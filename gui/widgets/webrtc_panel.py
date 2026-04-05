"""Collapsible WebRTC streaming panel for the main window."""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.audio_router import AudioRouter
from core.config_manager import ConfigManager
from core.models import DeviceDirection
from core.webrtc_streamer import HAS_ANY_BACKEND, HAS_FFMPEG_WHIP, WebRTCStreamer
from gui.widgets.no_scroll_spinbox import NoScrollComboBox, NoScrollSlider
from gui.widgets.vu_meter import VUMeter


class _StreamLog(QWidget):
    """Lightweight color-coded log for WebRTC events."""

    MAX_BLOCKS = 500

    _COLORS = {
        "info": QColor(180, 180, 180),
        "success": QColor(76, 175, 80),
        "warning": QColor(255, 235, 59),
        "error": QColor(244, 67, 54),
    }
    _ICONS = {
        "info": "\u24d8",      # ⓘ
        "success": "\u2713",   # ✓
        "warning": "\u26a0",   # ⚠
        "error": "\u2716",     # ✖
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self._text)

    @pyqtSlot(str, str)
    def add_message(self, text: str, level: str = "info") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        color = self._COLORS.get(level, self._COLORS["info"])
        icon = self._ICONS.get(level, self._ICONS["info"])

        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(color)
        cursor.insertText(f"[{ts}]  {icon} {text}\n", fmt)

        if self._text.document().blockCount() > self.MAX_BLOCKS:
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(
                QTextCursor.MoveOperation.Down,
                QTextCursor.MoveMode.KeepAnchor,
                200,
            )
            cursor.removeSelectedText()

        self._text.setTextCursor(cursor)
        self._text.ensureCursorVisible()

    def clear(self) -> None:
        self._text.clear()


class WebRTCPanel(QWidget):
    """Collapsible panel for WebRTC WHIP audio streaming.

    Sits on the main window and can be expanded/collapsed with a single
    click.  Contains URL/token inputs, audio-source selector, start/stop
    button, and a dedicated stream log.
    """

    def __init__(
        self,
        streamer: WebRTCStreamer,
        config_manager: ConfigManager,
        audio_router: AudioRouter,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._streamer = streamer
        self._cfg = config_manager
        self._audio_router = audio_router

        self._build_ui()
        self._connect_signals()
        self._restore_state()

    # -- ui ----------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Toggle header (expand/collapse URL, token, source, backend, log)
        self._toggle_btn = QPushButton("\u25b6  WebRTC Stream")
        self._toggle_btn.setObjectName("webrtcToggle")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        root.addWidget(self._toggle_btn)

        # Always visible: start/stop, stream meter, gain, status
        self._summary = QWidget()
        self._summary.setObjectName("webrtcSummary")
        sl = QHBoxLayout(self._summary)
        sl.setContentsMargins(12, 8, 12, 8)
        sl.setSpacing(10)

        self._stream_btn = QPushButton("START STREAM")
        self._stream_btn.setObjectName("webrtcStreamButton")
        self._stream_btn.setFixedWidth(160)
        self._stream_btn.clicked.connect(self._toggle_stream)
        sl.addWidget(self._stream_btn)

        self._stream_meter = VUMeter()
        self._stream_meter.setMinimumHeight(12)
        self._stream_meter.setMaximumHeight(16)
        self._stream_meter.setMinimumWidth(100)
        sl.addWidget(self._stream_meter, 1)

        self._stream_gain_slider = NoScrollSlider(Qt.Orientation.Horizontal)
        self._stream_gain_slider.setRange(0, 50)
        self._stream_gain_slider.setFixedWidth(120)
        self._stream_gain_slider.setToolTip(
            "Gain for the WebRTC stream only (not local output or translation input)"
        )
        self._stream_gain_slider.valueChanged.connect(self._on_stream_gain_slider)
        sl.addWidget(self._stream_gain_slider)
        self._stream_gain_label = QLabel("1.0×")
        self._stream_gain_label.setFixedWidth(36)
        self._stream_gain_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        sl.addWidget(self._stream_gain_label)

        sl.addSpacing(8)
        self._status_label = QLabel("Idle")
        self._status_label.setObjectName("webrtcStatus")
        sl.addWidget(self._status_label)

        root.addWidget(self._summary)

        # Collapsible details (WHIP, token, routing, log)
        self._content = QWidget()
        self._content.setObjectName("webrtcContent")
        self._content.setVisible(False)
        cl = QVBoxLayout(self._content)
        cl.setContentsMargins(12, 4, 12, 10)
        cl.setSpacing(8)

        # WHIP URL
        url_row = QHBoxLayout()
        lbl = QLabel("WHIP URL:")
        lbl.setFixedWidth(72)
        url_row.addWidget(lbl)
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://server.example/whip/live")
        self._url_input.textChanged.connect(
            lambda t: self._cfg.set("webrtc_whip_url", t)
        )
        url_row.addWidget(self._url_input, 1)
        cl.addLayout(url_row)

        # Bearer token
        token_row = QHBoxLayout()
        lbl2 = QLabel("Token:")
        lbl2.setFixedWidth(72)
        token_row.addWidget(lbl2)
        self._token_input = QLineEdit()
        self._token_input.setPlaceholderText("Bearer token (optional)")
        self._token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_input.textChanged.connect(
            lambda t: self._cfg.set("webrtc_bearer_token", t)
        )
        token_row.addWidget(self._token_input, 1)

        self._token_toggle = QPushButton("\U0001f441")  # 👁
        self._token_toggle.setFixedWidth(32)
        self._token_toggle.setToolTip("Show / hide token")
        self._token_toggle.setCheckable(True)
        self._token_toggle.toggled.connect(self._toggle_token_visibility)
        token_row.addWidget(self._token_toggle)
        cl.addLayout(token_row)

        # Audio source
        source_row = QHBoxLayout()
        lbl3 = QLabel("Audio:")
        lbl3.setFixedWidth(72)
        source_row.addWidget(lbl3)
        self._source_combo = NoScrollComboBox()
        self._source_combo.addItem("Original Audio (mic input)", "original")
        self._source_combo.addItem("Translated Audio (TTS output)", "translated")
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        source_row.addWidget(self._source_combo, 1)
        cl.addLayout(source_row)

        # Backend selection
        backend_row = QHBoxLayout()
        lbl4 = QLabel("Backend:")
        lbl4.setFixedWidth(72)
        backend_row.addWidget(lbl4)
        self._backend_combo = NoScrollComboBox()
        self._backend_combo.addItem("FFmpeg (low-latency)", "ffmpeg")
        self._backend_combo.addItem("aiortc (Python WebRTC)", "aiortc")
        self._backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        backend_row.addWidget(self._backend_combo, 1)
        cl.addLayout(backend_row)

        # Stream log
        self._log = _StreamLog()
        self._log.setFixedHeight(150)
        cl.addWidget(self._log)

        root.addWidget(self._content)

        if not HAS_ANY_BACKEND:
            self._stream_btn.setEnabled(False)
            self._stream_btn.setToolTip(
                "No WebRTC backend available.\n"
                "Install FFmpeg 6.1+ (recommended) or: pip install aiortc"
            )
            self._log.add_message(
                "No WebRTC backend found. Install FFmpeg 6.1+ with WHIP "
                "support (recommended) or: pip install aiortc",
                "warning",
            )
        elif HAS_FFMPEG_WHIP:
            self._log.add_message(
                "FFmpeg backend ready (low-latency)", "success",
            )

    # -- signals -----------------------------------------------------------

    def _connect_signals(self) -> None:
        self._toggle_btn.toggled.connect(self._on_toggle)
        self._streamer.log_message.connect(self._log.add_message)
        self._streamer.state_changed.connect(self._on_state_changed)
        self._streamer.stream_level_changed.connect(self._stream_meter.set_level)

    # -- state persistence -------------------------------------------------

    def _restore_state(self) -> None:
        cfg = self._cfg.config
        self._url_input.setText(cfg.webrtc_whip_url)
        self._token_input.setText(cfg.webrtc_bearer_token)

        idx = self._source_combo.findData(cfg.webrtc_audio_source)
        if idx >= 0:
            self._source_combo.setCurrentIndex(idx)

        bidx = self._backend_combo.findData(cfg.webrtc_backend)
        if bidx >= 0:
            self._backend_combo.setCurrentIndex(bidx)

        exp = cfg.webrtc_panel_expanded
        self._toggle_btn.blockSignals(True)
        self._toggle_btn.setChecked(exp)
        self._content.setVisible(exp)
        self._toggle_btn.blockSignals(False)
        arrow = "\u25bc" if exp else "\u25b6"
        self._toggle_btn.setText(f"{arrow}  WebRTC Stream")
        self._sync_webrtc_summary_chrome(exp)

        g = max(0.0, min(5.0, cfg.webrtc_stream_gain))
        self._stream_gain_slider.setValue(int(round(g * 10)))
        self._stream_gain_label.setText(f"{g:.1f}×")
        self._streamer.set_stream_gain(g)

    # -- slots -------------------------------------------------------------

    def _sync_webrtc_summary_chrome(self, details_open: bool) -> None:
        self._summary.setProperty("detailsOpen", details_open)
        self._summary.style().unpolish(self._summary)
        self._summary.style().polish(self._summary)

    def _on_toggle(self, expanded: bool) -> None:
        self._content.setVisible(expanded)
        self._sync_webrtc_summary_chrome(expanded)
        arrow = "\u25bc" if expanded else "\u25b6"
        self._toggle_btn.setText(f"{arrow}  WebRTC Stream")
        self._cfg.set("webrtc_panel_expanded", expanded)

    def _toggle_token_visibility(self, show: bool) -> None:
        self._token_input.setEchoMode(
            QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        )

    def _on_source_changed(self, _idx: int) -> None:
        source = self._source_combo.currentData()
        if source:
            self._cfg.set("webrtc_audio_source", source)

    def _on_backend_changed(self, _idx: int) -> None:
        backend = self._backend_combo.currentData()
        if backend:
            self._cfg.set("webrtc_backend", backend)

    def _on_stream_gain_slider(self, value: int) -> None:
        gain = max(0.0, min(5.0, value / 10.0))
        self._stream_gain_label.setText(f"{gain:.1f}×")
        self._cfg.set("webrtc_stream_gain", gain)
        self._streamer.set_stream_gain(gain)

    def _toggle_stream(self) -> None:
        if self._streamer.state in ("streaming", "connecting"):
            self._streamer.stop()
        else:
            source = self._source_combo.currentData() or "original"

            dev = self._audio_router.find_device_by_name(
                self._cfg.config.input_device_name, DeviceDirection.INPUT
            )
            device_id = dev.device_id if dev else None

            self._streamer.start(
                whip_url=self._url_input.text(),
                bearer_token=self._token_input.text(),
                audio_source=source,
                input_device_id=device_id,
                sample_rate=self._cfg.config.sample_rate,
                stream_gain=self._cfg.config.webrtc_stream_gain,
                preferred_backend=self._backend_combo.currentData() or "ffmpeg",
            )

    @pyqtSlot(str)
    def _on_state_changed(self, state: str) -> None:
        labels = {
            "idle": "Idle",
            "connecting": "Connecting…",
            "streaming": "Streaming",
            "stopping": "Stopping…",
            "error": "Error",
        }
        self._status_label.setText(labels.get(state, state))

        is_active = state in ("streaming", "connecting")
        self._stream_btn.setText("STOP STREAM" if is_active else "START STREAM")
        self._stream_btn.setProperty("streaming", is_active)
        self._stream_btn.style().unpolish(self._stream_btn)
        self._stream_btn.style().polish(self._stream_btn)

        self._url_input.setEnabled(not is_active)
        self._token_input.setEnabled(not is_active)
        self._source_combo.setEnabled(not is_active)
        self._backend_combo.setEnabled(not is_active)
