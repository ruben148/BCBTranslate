"""OTA update checker and installer launcher.

Checks the GitHub Releases API for a newer version on startup.  When an
update is found the user is prompted to download the Inno Setup installer
and run it in silent mode, which upgrades in-place without losing
configuration or credentials.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from version import APP_VERSION

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
USER_AGENT = "BCBTranslate-Updater"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse '1.2.3' or 'v1.2.3' into a comparable tuple."""
    return tuple(int(x) for x in v.lstrip("v").split("."))


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class UpdateInfo:
    version: str
    download_url: str
    release_notes: str
    file_size: int
    html_url: str


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class _CheckWorker(QThread):
    """Fetches the latest release from GitHub in a background thread."""

    finished = pyqtSignal(object)  # UpdateInfo | None
    error = pyqtSignal(str)

    def __init__(self, github_repo: str, current_version: str, parent=None):
        super().__init__(parent)
        self._repo = github_repo
        self._current = current_version

    def run(self) -> None:
        try:
            url = f"{GITHUB_API}/repos/{self._repo}/releases/latest"
            req = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            tag = data.get("tag_name", "")
            if not tag:
                self.finished.emit(None)
                return

            if _parse_version(tag) <= _parse_version(self._current):
                self.finished.emit(None)
                return

            download_url, file_size = self._find_installer_asset(data)
            if not download_url:
                logger.warning("Release %s has no installer asset", tag)
                self.finished.emit(None)
                return

            self.finished.emit(UpdateInfo(
                version=tag.lstrip("v"),
                download_url=download_url,
                release_notes=data.get("body", "") or "",
                file_size=file_size,
                html_url=data.get("html_url", ""),
            ))

        except Exception as exc:
            logger.debug("Update check failed: %s", exc)
            self.error.emit(str(exc))

    @staticmethod
    def _find_installer_asset(release: dict) -> tuple[str, int]:
        """Return (download_url, size) for the installer .exe asset."""
        for asset in release.get("assets", []):
            name = asset.get("name", "").lower()
            if name.endswith(".exe") and "setup" in name:
                return asset["browser_download_url"], asset.get("size", 0)

        for asset in release.get("assets", []):
            if asset.get("name", "").lower().endswith(".exe"):
                return asset["browser_download_url"], asset.get("size", 0)

        return "", 0


class _DownloadWorker(QThread):
    """Downloads the installer to a temp file with progress reporting."""

    progress = pyqtSignal(int, int)   # (bytes_downloaded, total_bytes)
    finished = pyqtSignal(str)        # path to downloaded file
    error = pyqtSignal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        tmp_path = ""
        try:
            req = urllib.request.Request(self._url, headers={
                "User-Agent": USER_AGENT,
            })
            with urllib.request.urlopen(req, timeout=300) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                fd, tmp_path = tempfile.mkstemp(
                    suffix=".exe", prefix="BCBTranslate_Update_",
                )
                downloaded = 0
                with os.fdopen(fd, "wb") as f:
                    while True:
                        if self._cancelled:
                            break
                        chunk = resp.read(65_536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(downloaded, total)

            if self._cancelled:
                self._cleanup(tmp_path)
                return

            self.finished.emit(tmp_path)

        except Exception as exc:
            self._cleanup(tmp_path)
            self.error.emit(str(exc))

    @staticmethod
    def _cleanup(path: str) -> None:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class UpdateChecker(QObject):
    """Kick off a background check and emit a signal if an update exists."""

    update_available = pyqtSignal(object)   # UpdateInfo
    no_update = pyqtSignal()
    check_error = pyqtSignal(str)

    def __init__(self, github_repo: str, parent: QObject | None = None):
        super().__init__(parent)
        self._repo = github_repo
        self._worker: _CheckWorker | None = None

    def check(self) -> None:
        if not self._repo:
            logger.info("No GitHub repo configured — skipping update check")
            return

        self._worker = _CheckWorker(self._repo, APP_VERSION, self)
        self._worker.finished.connect(self._on_check_done)
        self._worker.error.connect(self._on_check_error)
        self._worker.start()

    def _on_check_done(self, info: UpdateInfo | None) -> None:
        if info:
            logger.info("Update available: %s -> %s", APP_VERSION, info.version)
            self.update_available.emit(info)
        else:
            logger.debug("No update available (current: %s)", APP_VERSION)
            self.no_update.emit()

    def _on_check_error(self, msg: str) -> None:
        logger.debug("Update check error: %s", msg)
        self.check_error.emit(msg)


class UpdateDownloadDialog(QDialog):
    """Modal dialog that downloads the installer and shows a progress bar."""

    def __init__(self, info: UpdateInfo, parent: QWidget | None = None):
        super().__init__(parent)
        self._info = info
        self._worker: _DownloadWorker | None = None
        self._installer_path: str | None = None

        self.setWindowTitle("Downloading Update")
        self.setFixedSize(440, 160)
        self.setModal(True)

        layout = QVBoxLayout(self)

        self._label = QLabel(f"Downloading BCBTranslate {info.version} …")
        layout.addWidget(self._label)

        self._progress = QProgressBar()
        self._progress.setMaximum(info.file_size if info.file_size > 0 else 0)
        layout.addWidget(self._progress)

        self._size_label = QLabel("")
        layout.addWidget(self._size_label)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.rejected.connect(self._cancel)
        layout.addWidget(self._buttons)

        self._start_download()

    def _start_download(self) -> None:
        self._worker = _DownloadWorker(self._info.download_url, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_download_done)
        self._worker.error.connect(self._on_download_error)
        self._worker.start()

    def _on_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(downloaded)
            mb_done = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            self._size_label.setText(f"{mb_done:.1f} / {mb_total:.1f} MB")
        else:
            mb_done = downloaded / (1024 * 1024)
            self._size_label.setText(f"{mb_done:.1f} MB downloaded")

    def _on_download_done(self, path: str) -> None:
        self._installer_path = path
        self._label.setText("Download complete!")
        self._progress.setValue(self._progress.maximum() or 100)
        self.accept()

    def _on_download_error(self, msg: str) -> None:
        self._label.setText(f"Download failed: {msg}")
        self._buttons.clear()
        self._buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self._buttons.rejected.connect(self.reject)

    def _cancel(self) -> None:
        if self._worker:
            self._worker.cancel()
        self.reject()

    @property
    def installer_path(self) -> str | None:
        return self._installer_path


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def prompt_and_install(info: UpdateInfo, parent: QWidget | None = None) -> bool:
    """Show update confirmation, download, and launch the installer.

    Returns True if the application should exit (installer is launching).
    """
    size_str = ""
    if info.file_size > 0:
        size_str = f"  ({info.file_size / (1024 * 1024):.1f} MB)"

    notes = info.release_notes
    if len(notes) > 500:
        notes = notes[:500] + "…"
    if not notes:
        notes = "No release notes."

    reply = QMessageBox.question(
        parent,
        "Update Available",
        f"A new version of BCBTranslate is available!\n\n"
        f"Current version:  {APP_VERSION}\n"
        f"New version:  {info.version}{size_str}\n\n"
        f"Release notes:\n{notes}\n\n"
        f"Download and install now?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    )

    if reply != QMessageBox.StandardButton.Yes:
        return False

    dlg = UpdateDownloadDialog(info, parent)
    if dlg.exec() and dlg.installer_path:
        _launch_installer(dlg.installer_path)
        return True

    return False


def _launch_installer(path: str) -> None:
    """Launch the Inno Setup installer in silent mode and let it upgrade us."""
    logger.info("Launching installer: %s", path)
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    subprocess.Popen(
        [path, "/SILENT", "/CLOSEAPPLICATIONS"],
        **kwargs,
    )
