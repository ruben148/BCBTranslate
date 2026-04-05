"""Modal dialog for WebRTC WHIP URL, token, and backend selection."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.config_manager import ConfigManager
from gui.widgets.no_scroll_spinbox import NoScrollComboBox


class WebRTCStreamSettingsDialog(QDialog):
    """Separate window with WHIP URL, bearer token, and encoder backend."""

    def __init__(self, config_manager: ConfigManager, parent=None) -> None:
        super().__init__(parent)
        self._cfg = config_manager
        self.setWindowTitle("WebRTC stream settings")
        self.setModal(True)
        self.resize(520, 220)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://server.example/whip/live")
        self._url_input.textChanged.connect(
            lambda t: self._cfg.set("webrtc_whip_url", t)
        )
        form.addRow(QLabel("WHIP URL:"), self._url_input)

        token_row = QHBoxLayout()
        self._token_input = QLineEdit()
        self._token_input.setPlaceholderText("Bearer token (optional)")
        self._token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_input.textChanged.connect(
            lambda t: self._cfg.set("webrtc_bearer_token", t)
        )
        token_row.addWidget(self._token_input, 1)
        self._token_toggle = QPushButton("\U0001f441")
        self._token_toggle.setFixedWidth(32)
        self._token_toggle.setToolTip("Show / hide token")
        self._token_toggle.setCheckable(True)
        self._token_toggle.toggled.connect(self._on_token_visibility)
        token_row.addWidget(self._token_toggle)
        token_wrap = QWidget()
        token_wrap.setLayout(token_row)
        form.addRow(QLabel("Token:"), token_wrap)

        self._backend_combo = NoScrollComboBox()
        self._backend_combo.addItem("FFmpeg (low-latency)", "ffmpeg")
        self._backend_combo.addItem("aiortc (Python WebRTC)", "aiortc")
        self._backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        form.addRow(QLabel("Backend:"), self._backend_combo)

        root.addLayout(form)

        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn)

        self._load_from_config()

    def _load_from_config(self) -> None:
        cfg = self._cfg.config
        self._url_input.setText(cfg.webrtc_whip_url)
        self._token_input.setText(cfg.webrtc_bearer_token)
        idx = self._backend_combo.findData(cfg.webrtc_backend)
        if idx >= 0:
            self._backend_combo.setCurrentIndex(idx)

    def _on_token_visibility(self, show: bool) -> None:
        self._token_input.setEchoMode(
            QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        )

    def _on_backend_changed(self, _idx: int) -> None:
        backend = self._backend_combo.currentData()
        if backend:
            self._cfg.set("webrtc_backend", backend)

    def set_fields_enabled(self, enabled: bool) -> None:
        self._url_input.setEnabled(enabled)
        self._token_input.setEnabled(enabled)
        self._token_toggle.setEnabled(enabled)
        self._backend_combo.setEnabled(enabled)
