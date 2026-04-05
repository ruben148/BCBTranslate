"""WebRTC streaming panel: transport controls on the main window; WHIP settings in a dialog."""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.audio_router import AudioRouter
from core.config_manager import ConfigManager
from core.models import DeviceDirection
from core.webrtc_streamer import HAS_ANY_BACKEND, HAS_FFMPEG_WHIP, WebRTCStreamer
from gui.webrtc_settings_dialog import WebRTCStreamSettingsDialog
from gui.widgets.no_scroll_spinbox import NoScrollComboBox, NoScrollSlider
from gui.widgets.vu_meter import VUMeter


class _StreamLog(QWidget):
    """Lightweight color-coded log for WebRTC events."""

    MAX_BLOCKS = 500
    _LOG_FONT_PT = 10

    _COLORS = {
        "info": QColor(180, 180, 180),
        "success": QColor(76, 175, 80),
        "warning": QColor(255, 235, 59),
        "error": QColor(244, 67, 54),
    }
    _ICONS = {
        "info": "\u24d8",
        "success": "\u2713",
        "warning": "\u26a0",
        "error": "\u2716",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        _lf = QFont(self._text.font())
        _lf.setPointSize(self._LOG_FONT_PT)
        self._text.setFont(_lf)
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
    """WebRTC WHIP streaming: controls and log on the main window; URL/token/backend in a dialog."""

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

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        group = QGroupBox("WebRTC Stream")
        gl = QVBoxLayout(group)
        gl.setSpacing(8)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)

        self._stream_btn = QPushButton("START STREAM")
        self._stream_btn.setObjectName("webrtcStreamButton")
        self._stream_btn.setFixedWidth(160)
        self._stream_btn.clicked.connect(self._toggle_stream)
        ctrl.addWidget(self._stream_btn)

        self._stream_meter = VUMeter()
        self._stream_meter.setMinimumHeight(12)
        self._stream_meter.setMaximumHeight(16)
        self._stream_meter.setMinimumWidth(100)
        ctrl.addWidget(self._stream_meter, 1)

        self._stream_gain_slider = NoScrollSlider(Qt.Orientation.Horizontal)
        self._stream_gain_slider.setRange(0, 50)
        self._stream_gain_slider.setFixedWidth(120)
        self._stream_gain_slider.setToolTip(
            "Gain for the WebRTC stream only (not local output or translation input)"
        )
        self._stream_gain_slider.valueChanged.connect(self._on_stream_gain_slider)
        ctrl.addWidget(self._stream_gain_slider)
        self._stream_gain_label = QLabel("1.0×")
        self._stream_gain_label.setFixedWidth(36)
        self._stream_gain_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        ctrl.addWidget(self._stream_gain_label)

        ctrl.addSpacing(8)
        self._status_label = QLabel("Idle")
        self._status_label.setObjectName("webrtcStatus")
        ctrl.addWidget(self._status_label)

        gl.addLayout(ctrl)

        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Audio:"))
        self._source_combo = NoScrollComboBox()
        self._source_combo.addItem("Original Audio (mic input)", "original")
        self._source_combo.addItem("Translated Audio (TTS output)", "translated")
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        src_row.addWidget(self._source_combo, 1)

        self._settings_btn = QPushButton("Settings")
        self._settings_btn.setObjectName("webrtcSettingsButton")
        self._settings_btn.setToolTip(
            "WHIP URL, bearer token, and encoder backend"
        )
        self._settings_btn.clicked.connect(self._open_stream_settings)
        src_row.addWidget(self._settings_btn)

        gl.addLayout(src_row)

        self._log = _StreamLog()
        self._log.setFixedHeight(100)
        gl.addWidget(self._log)

        root.addWidget(group)

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

    def _connect_signals(self) -> None:
        self._streamer.log_message.connect(self._log.add_message)
        self._streamer.state_changed.connect(self._on_state_changed)
        self._streamer.stream_level_changed.connect(self._stream_meter.set_level)

    def _restore_state(self) -> None:
        cfg = self._cfg.config
        idx = self._source_combo.findData(cfg.webrtc_audio_source)
        if idx >= 0:
            self._source_combo.setCurrentIndex(idx)

        g = max(0.0, min(5.0, cfg.webrtc_stream_gain))
        self._stream_gain_slider.setValue(int(round(g * 10)))
        self._stream_gain_label.setText(f"{g:.1f}×")
        self._streamer.set_stream_gain(g)

    def _open_stream_settings(self) -> None:
        dlg = WebRTCStreamSettingsDialog(self._cfg, parent=self.window())
        is_live = self._streamer.state in ("streaming", "connecting")
        dlg.set_fields_enabled(not is_live)
        dlg.exec()

    def _on_source_changed(self, _idx: int) -> None:
        source = self._source_combo.currentData()
        if source:
            self._cfg.set("webrtc_audio_source", source)

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
            cfg = self._cfg.config

            self._streamer.start(
                whip_url=cfg.webrtc_whip_url,
                bearer_token=cfg.webrtc_bearer_token,
                audio_source=source,
                input_device_id=device_id,
                sample_rate=cfg.sample_rate,
                stream_gain=cfg.webrtc_stream_gain,
                preferred_backend=cfg.webrtc_backend or "ffmpeg",
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

        self._source_combo.setEnabled(not is_active)
        self._settings_btn.setEnabled(not is_active)
