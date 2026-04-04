"""BCBTranslate — Real-time speech translation application."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from core.audio_router import AudioRouter
from core.config_manager import ConfigManager
from core.translation_pipeline import TranslationPipeline
from gui.main_window import MainWindow

_APP_ID = "BCBTranslate.BCBTranslate"


def _get_app_dir() -> Path:
    """Return the directory where the executable (or script) lives.
    Handles both normal Python execution and PyInstaller frozen bundles."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _get_icon_path() -> Path:
    """Resolve app icon for both dev and frozen (PyInstaller) mode."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "gui" / "resources" / "icons" / "app.png"
    return Path(__file__).parent / "gui" / "resources" / "icons" / "app.png"


def _setup_logging(level: str = "INFO") -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_ID)

    # Ensure .env is loaded from the application directory (matters for installed builds)
    app_dir = _get_app_dir()
    env_file = app_dir / ".env"
    load_dotenv(env_file if env_file.exists() else None)

    app = QApplication(sys.argv)
    app.setApplicationName("BCBTranslate")
    app.setOrganizationName("BCBTranslate")
    icon_path = _get_icon_path()
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    config_manager = ConfigManager()
    _setup_logging(config_manager.config.log_level)

    logger = logging.getLogger("main")
    logger.info("BCBTranslate starting...")

    if not config_manager.has_azure_credentials():
        QMessageBox.warning(
            None,
            "Azure Credentials Missing",
            "Azure Speech credentials were not found.\n\n"
            f"Please set the environment variables:\n"
            f"  • {config_manager.config.speech_key_env_var}\n"
            f"  • {config_manager.config.speech_region_env_var}\n\n"
            "You can create a .env file in the application directory,\n"
            "or set them as system environment variables.\n\n"
            "The application will start, but translation will not work\n"
            "until valid credentials are provided.",
        )

    audio_router = AudioRouter()
    pipeline = TranslationPipeline(config_manager, audio_router)
    window = MainWindow(config_manager, audio_router, pipeline)

    logger.info("BCBTranslate ready")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
