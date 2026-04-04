from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from core.audio_router import AudioRouter
from core.config_manager import ConfigManager
from core.models import DeviceDirection, TranslationMetrics
from core.translation_pipeline import TranslationPipeline
from core.updater import UpdateChecker, UpdateInfo, prompt_and_install
from gui.settings_dialog import SettingsDialog
from gui.tray import TrayIcon
from gui.widgets.device_selector import DeviceSelector
from gui.widgets.lag_indicator import LagIndicator
from gui.widgets.log_panel import LogPanel
from gui.widgets.vu_meter import VUMeter
from utils.hotkey_manager import HotkeyManager
from version import GITHUB_REPO

logger = logging.getLogger(__name__)


def _resource_dir() -> Path:
    """Resolve the styles directory for both dev and frozen (PyInstaller) mode."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "gui" / "resources" / "styles"
    return Path(__file__).parent / "resources" / "styles"


STYLE_DIR = _resource_dir()


class MainWindow(QMainWindow):
    def __init__(
        self,
        config_manager: ConfigManager,
        audio_router: AudioRouter,
        pipeline: TranslationPipeline,
    ):
        super().__init__()
        self._cfg = config_manager
        self._audio_router = audio_router
        self._pipeline = pipeline

        self.setWindowTitle("BCBTranslate")
        self.setMinimumSize(680, 720)
        self.resize(720, 800)

        self._audio_router.gain = self._cfg.config.input_gain

        self._build_ui()
        self._connect_signals()
        self._apply_theme()
        self._restore_device_selections()

        # System tray
        self._tray = TrayIcon(self)
        self._tray.show()

        # Metrics polling timer
        self._metrics_timer = QTimer(self)
        self._metrics_timer.timeout.connect(self._poll_metrics)
        self._metrics_timer.start(500)

        # VU meter updates from audio router
        self._start_vu_monitor()

        # Global hotkey
        self._hotkey = HotkeyManager(self._cfg.config.hotkey_start_stop, self)
        self._hotkey.triggered.connect(self.toggle_translation)
        self._hotkey.start()

        # Window flags
        if self._cfg.config.always_on_top:
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        if self._cfg.config.start_minimized:
            self.hide()
        else:
            self.show()

        # OTA update check (non-blocking)
        self._update_checker: UpdateChecker | None = None
        if self._cfg.config.auto_check_updates and GITHUB_REPO:
            self._check_for_updates()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # ── Audio section ────────────────────────────────────────────────
        audio_group = QGroupBox("Audio")
        audio_layout = QVBoxLayout(audio_group)

        # Input device
        in_row = QHBoxLayout()
        in_row.addWidget(QLabel("Input:"))
        self._input_selector = DeviceSelector(
            self._audio_router, DeviceDirection.INPUT
        )
        self._input_selector.device_changed.connect(self._on_input_device_changed)
        in_row.addWidget(self._input_selector, 1)
        audio_layout.addLayout(in_row)

        # VU meter
        self._vu_meter = VUMeter()
        audio_layout.addWidget(self._vu_meter)
        if not self._cfg.config.show_vu_meter:
            self._vu_meter.hide()

        # Input gain slider
        gain_row = QHBoxLayout()
        gain_row.addWidget(QLabel("Gain:"))
        self._gain_slider = QSlider(Qt.Orientation.Horizontal)
        self._gain_slider.setRange(0, 50)  # 0.0× – 5.0×
        self._gain_slider.setValue(int(self._cfg.config.input_gain * 10))
        self._gain_slider.setTickInterval(5)
        self._gain_slider.valueChanged.connect(self._on_gain_changed)
        gain_row.addWidget(self._gain_slider, 1)
        self._gain_label = QLabel(f"{self._cfg.config.input_gain:.1f}×")
        self._gain_label.setFixedWidth(45)
        gain_row.addWidget(self._gain_label)
        audio_layout.addLayout(gain_row)

        # Output device
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output:"))
        self._output_selector = DeviceSelector(
            self._audio_router, DeviceDirection.OUTPUT
        )
        self._output_selector.device_changed.connect(self._on_output_device_changed)
        out_row.addWidget(self._output_selector, 1)
        audio_layout.addLayout(out_row)

        # Secondary output
        sec_row = QHBoxLayout()
        sec_row.addWidget(QLabel("2nd Out:"))
        self._secondary_selector = DeviceSelector(
            self._audio_router, DeviceDirection.OUTPUT
        )
        self._secondary_selector.device_changed.connect(
            self._on_secondary_device_changed
        )
        sec_row.addWidget(self._secondary_selector, 1)
        audio_layout.addLayout(sec_row)

        root.addWidget(audio_group)

        # ── Segmentation section ─────────────────────────────────────────
        seg_group = QGroupBox("Segmentation")
        seg_layout = QVBoxLayout(seg_group)

        # Silence timeout
        silence_row = QHBoxLayout()
        silence_row.addWidget(QLabel("Silence:"))
        self._seg_timeout_spin = QSpinBox()
        self._seg_timeout_spin.setRange(100, 5000)
        self._seg_timeout_spin.setSingleStep(100)
        self._seg_timeout_spin.setSuffix(" ms")
        self._seg_timeout_spin.setToolTip(
            "How long a silence gap (in ms) must last before Azure finalises\n"
            "the current utterance and starts a new one.\n\n"
            "Lower → more frequent, shorter utterances.\n"
            "Higher → fewer, longer utterances."
        )
        self._seg_timeout_spin.setValue(self._cfg.config.segmentation_silence_timeout_ms)
        self._seg_timeout_spin.valueChanged.connect(self._on_seg_timeout_changed)
        silence_row.addWidget(self._seg_timeout_spin, 1)
        seg_layout.addLayout(silence_row)

        # Auto-adjust checkbox
        self._auto_seg_cb = QCheckBox("Auto-adjust segmentation timeout")
        self._auto_seg_cb.setToolTip(
            "Automatically tune the segmentation silence timeout based on\n"
            "observed utterance durations so they stay within the target range."
        )
        self._auto_seg_cb.setChecked(self._cfg.config.auto_segmentation_enabled)
        self._auto_seg_cb.toggled.connect(self._on_auto_seg_toggled)
        seg_layout.addWidget(self._auto_seg_cb)

        # Target min / max row
        thresh_row = QHBoxLayout()
        thresh_row.addWidget(QLabel("Target min:"))
        self._auto_seg_min_spin = QDoubleSpinBox()
        self._auto_seg_min_spin.setRange(1.0, 30.0)
        self._auto_seg_min_spin.setSingleStep(1.0)
        self._auto_seg_min_spin.setDecimals(1)
        self._auto_seg_min_spin.setSuffix(" s")
        self._auto_seg_min_spin.setValue(self._cfg.config.auto_seg_target_min_s)
        self._auto_seg_min_spin.setEnabled(self._cfg.config.auto_segmentation_enabled)
        self._auto_seg_min_spin.valueChanged.connect(self._on_auto_seg_min_changed)
        thresh_row.addWidget(self._auto_seg_min_spin)

        thresh_row.addSpacing(10)
        thresh_row.addWidget(QLabel("Target max:"))
        self._auto_seg_max_spin = QDoubleSpinBox()
        self._auto_seg_max_spin.setRange(5.0, 55.0)
        self._auto_seg_max_spin.setSingleStep(1.0)
        self._auto_seg_max_spin.setDecimals(1)
        self._auto_seg_max_spin.setSuffix(" s")
        self._auto_seg_max_spin.setValue(self._cfg.config.auto_seg_target_max_s)
        self._auto_seg_max_spin.setEnabled(self._cfg.config.auto_segmentation_enabled)
        self._auto_seg_max_spin.valueChanged.connect(self._on_auto_seg_max_changed)
        thresh_row.addWidget(self._auto_seg_max_spin)
        seg_layout.addLayout(thresh_row)

        root.addWidget(seg_group)

        # ── Translation section ──────────────────────────────────────────
        trans_group = QGroupBox("Translation")
        trans_layout = QHBoxLayout(trans_group)

        trans_layout.addWidget(QLabel("From:"))
        self._source_label = QLabel(self._cfg.config.source_language)
        self._source_label.setStyleSheet("font-weight: bold;")
        trans_layout.addWidget(self._source_label)

        trans_layout.addWidget(QLabel("→"))

        trans_layout.addWidget(QLabel("To:"))
        self._target_label = QLabel(self._cfg.config.target_language)
        self._target_label.setStyleSheet("font-weight: bold;")
        trans_layout.addWidget(self._target_label)

        trans_layout.addStretch()

        trans_layout.addWidget(QLabel("Voice:"))
        self._voice_label = QLabel(self._cfg.config.voice_name)
        self._voice_label.setStyleSheet("font-weight: bold;")
        trans_layout.addWidget(self._voice_label)

        root.addWidget(trans_group)

        # ── Voice Tuning section ─────────────────────────────────────────
        tuning_group = QGroupBox("Voice Tuning")
        tuning_layout = QVBoxLayout(tuning_group)

        # Speed slider
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Speed:"))
        self._speed_slider = QSlider(Qt.Orientation.Horizontal)
        self._speed_slider.setRange(50, 200)  # 0.5× – 2.0×
        self._speed_slider.setValue(int(self._cfg.config.speaking_rate * 100))
        self._speed_slider.setTickInterval(25)
        self._speed_slider.valueChanged.connect(self._on_speed_changed)
        speed_row.addWidget(self._speed_slider, 1)
        self._speed_label = QLabel(f"{self._cfg.config.speaking_rate:.1f}×")
        self._speed_label.setFixedWidth(45)
        speed_row.addWidget(self._speed_label)
        tuning_layout.addLayout(speed_row)

        # Pitch slider
        pitch_row = QHBoxLayout()
        pitch_row.addWidget(QLabel("Pitch:"))
        self._pitch_slider = QSlider(Qt.Orientation.Horizontal)
        self._pitch_slider.setRange(-50, 50)
        self._pitch_slider.setValue(self._parse_pitch(self._cfg.config.pitch))
        self._pitch_slider.setTickInterval(10)
        self._pitch_slider.valueChanged.connect(self._on_pitch_changed)
        pitch_row.addWidget(self._pitch_slider, 1)
        self._pitch_label = QLabel(self._cfg.config.pitch)
        self._pitch_label.setFixedWidth(45)
        pitch_row.addWidget(self._pitch_label)
        tuning_layout.addLayout(pitch_row)

        root.addWidget(tuning_group)

        # ── Status section ───────────────────────────────────────────────
        status_group = QGroupBox("Status")
        status_layout = QHBoxLayout(status_group)

        self._lag_indicator = LagIndicator()
        status_layout.addWidget(self._lag_indicator)

        status_layout.addSpacing(16)

        self._queue_label = QLabel("Queue: 0")
        status_layout.addWidget(self._queue_label)

        self._session_label = QLabel("Session: 00:00:00")
        status_layout.addWidget(self._session_label)

        self._rate_label = QLabel("")
        status_layout.addWidget(self._rate_label)

        status_layout.addStretch()

        # Start / Stop button
        self._start_btn = QPushButton("START")
        self._start_btn.setObjectName("startButton")
        self._start_btn.setFixedWidth(140)
        self._start_btn.clicked.connect(self.toggle_translation)
        status_layout.addWidget(self._start_btn)

        # Settings button
        settings_btn = QPushButton("Settings")
        settings_btn.clicked.connect(self.open_settings)
        status_layout.addWidget(settings_btn)

        root.addWidget(status_group)

        # ── Log panel ────────────────────────────────────────────────────
        log_group = QGroupBox("Live Log")
        log_layout = QVBoxLayout(log_group)
        self._log_panel = LogPanel()
        log_layout.addWidget(self._log_panel)
        root.addWidget(log_group, 1)  # stretch factor

        # ── Status bar ───────────────────────────────────────────────────
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._seg_label = QLabel("")
        self._status_bar.addPermanentWidget(self._seg_label)
        self._connection_label = QLabel("Disconnected")
        self._status_bar.addPermanentWidget(self._connection_label)

    # -- signal wiring -----------------------------------------------------

    def _connect_signals(self) -> None:
        p = self._pipeline
        p.utterance_complete.connect(self._on_utterance_complete)
        p.partial_result.connect(self._log_panel.add_partial)
        p.status_changed.connect(self._log_panel.add_status)
        p.error_occurred.connect(self._log_panel.add_error)
        p.connection_changed.connect(self._on_connection_changed)
        p.segmentation_updated.connect(self._on_segmentation_updated)

    # -- theme -------------------------------------------------------------

    def _apply_theme(self) -> None:
        theme = self._cfg.config.theme
        qss_file = STYLE_DIR / f"{theme}.qss"
        if qss_file.exists():
            self.setStyleSheet(qss_file.read_text(encoding="utf-8"))
        else:
            logger.warning("Theme file not found: %s", qss_file)

    def set_theme(self, theme: str) -> None:
        self._cfg.set("theme", theme)
        self._apply_theme()

    # -- device selection --------------------------------------------------

    def _restore_device_selections(self) -> None:
        cfg = self._cfg.config
        self._input_selector.select_by_name(cfg.input_device_name)
        self._output_selector.select_by_name(cfg.output_device_name)
        self._secondary_selector.select_by_name(cfg.secondary_output_device_name)

    def _on_input_device_changed(self, dev) -> None:
        name = dev.name if dev else None
        self._cfg.set("input_device_name", name)
        self._start_vu_monitor()

    def _on_output_device_changed(self, dev) -> None:
        name = dev.name if dev else None
        self._cfg.set("output_device_name", name)

    def _on_secondary_device_changed(self, dev) -> None:
        name = dev.name if dev else None
        self._cfg.set("secondary_output_device_name", name)

    # -- VU meter ----------------------------------------------------------

    def _start_vu_monitor(self) -> None:
        if not self._cfg.config.show_vu_meter:
            return
        dev = self._input_selector.selected_device()
        dev_id = dev.device_id if dev else None
        self._audio_router.start_vu_stream(dev_id, callback=self._vu_meter.set_level)

    # -- gain --------------------------------------------------------------

    def _on_gain_changed(self, value: int) -> None:
        gain = value / 10.0
        self._gain_label.setText(f"{gain:.1f}×")
        self._cfg.set("input_gain", gain)
        self._audio_router.gain = gain

    # -- segmentation controls -----------------------------------------------

    def _on_seg_timeout_changed(self, value: int) -> None:
        self._cfg.set("segmentation_silence_timeout_ms", value)

    def _on_auto_seg_toggled(self, checked: bool) -> None:
        self._cfg.set("auto_segmentation_enabled", checked)
        self._auto_seg_min_spin.setEnabled(checked)
        self._auto_seg_max_spin.setEnabled(checked)

    def _on_auto_seg_min_changed(self, value: float) -> None:
        self._cfg.set("auto_seg_target_min_s", value)

    def _on_auto_seg_max_changed(self, value: float) -> None:
        self._cfg.set("auto_seg_target_max_s", value)

    # -- speed / pitch -----------------------------------------------------

    def _on_speed_changed(self, value: int) -> None:
        rate = value / 100.0
        self._speed_label.setText(f"{rate:.1f}×")
        self._cfg.set("speaking_rate", rate)

    def _on_pitch_changed(self, value: int) -> None:
        pitch = f"{value:+d}%"
        self._pitch_label.setText(pitch)
        self._cfg.set("pitch", pitch)

    @staticmethod
    def _parse_pitch(pitch_str: str) -> int:
        try:
            return int(pitch_str.replace("%", "").replace("+", ""))
        except ValueError:
            return 0

    # -- translation control -----------------------------------------------

    def toggle_translation(self) -> None:
        if self._pipeline.is_running:
            self._pipeline.stop()
            self._start_btn.setText("START")
            self._start_btn.setProperty("running", False)
            self._tray.set_running(False)
        else:
            self._pipeline.start()
            self._start_btn.setText("STOP")
            self._start_btn.setProperty("running", True)
            self._tray.set_running(True)

        # Force style update for the dynamic property
        self._start_btn.style().unpolish(self._start_btn)
        self._start_btn.style().polish(self._start_btn)

    # -- metrics polling ---------------------------------------------------

    def _poll_metrics(self) -> None:
        if not self._pipeline.is_running:
            return
        metrics = self._pipeline.monitor.snapshot()
        self._update_metrics_display(metrics)

    def _update_metrics_display(self, m: TranslationMetrics) -> None:
        self._lag_indicator.set_lag(m.current_lag_ms)
        self._queue_label.setText(f"Queue: {m.queue_depth}")

        h, rem = divmod(int(m.session_duration_s), 3600)
        mi, s = divmod(rem, 60)
        self._session_label.setText(f"Session: {h:02d}:{mi:02d}:{s:02d}")

        if m.effective_rate != self._cfg.config.speaking_rate:
            self._rate_label.setText(f"Adaptive: {m.effective_rate:.1f}×")
        else:
            self._rate_label.setText("")

        self._tray.update_lag(m.current_lag_ms)

    # -- callbacks ---------------------------------------------------------

    def _on_utterance_complete(self, source: str, translated: str, lag_ms: int) -> None:
        self._log_panel.add_translation(source, translated, lag_ms)

    def _on_segmentation_updated(self, timeout_ms: int, avg_duration: float) -> None:
        self._seg_label.setText(f"Seg: {timeout_ms} ms")
        self._seg_label.setToolTip(
            f"Auto-segmentation active\n"
            f"Current timeout: {timeout_ms} ms\n"
            f"Avg utterance: {avg_duration:.1f} s"
        )
        self._log_panel.add_status(
            f"Auto-segmentation: timeout \u2192 {timeout_ms} ms "
            f"(avg utterance {avg_duration:.1f}s)"
        )
        # Keep the main-window spin box in sync with the auto-adjusted value
        self._seg_timeout_spin.blockSignals(True)
        self._seg_timeout_spin.setValue(timeout_ms)
        self._seg_timeout_spin.blockSignals(False)

    def _on_connection_changed(self, connected: bool) -> None:
        if connected:
            self._connection_label.setText("Connected to Azure")
            self._connection_label.setStyleSheet("color: #4caf50;")
        else:
            self._connection_label.setText("Disconnected")
            self._connection_label.setStyleSheet("color: #888;")

    # -- settings dialog ---------------------------------------------------

    def open_settings(self) -> None:
        dlg = SettingsDialog(self._cfg, self._audio_router, self._pipeline, parent=self)
        if dlg.exec():
            self._apply_theme()
            self._source_label.setText(self._cfg.config.source_language)
            self._target_label.setText(self._cfg.config.target_language)
            self._voice_label.setText(self._cfg.config.voice_name)
            self._speed_slider.setValue(int(self._cfg.config.speaking_rate * 100))
            self._pitch_slider.setValue(self._parse_pitch(self._cfg.config.pitch))
            self._gain_slider.setValue(int(self._cfg.config.input_gain * 10))
            self._audio_router.gain = self._cfg.config.input_gain

            # Sync segmentation controls
            self._seg_timeout_spin.setValue(
                self._cfg.config.segmentation_silence_timeout_ms
            )
            self._auto_seg_cb.setChecked(self._cfg.config.auto_segmentation_enabled)
            self._auto_seg_min_spin.setValue(self._cfg.config.auto_seg_target_min_s)
            self._auto_seg_max_spin.setValue(self._cfg.config.auto_seg_target_max_s)

            # Update hotkey
            self._hotkey.update_hotkey(self._cfg.config.hotkey_start_stop)

            # Update VU visibility
            self._vu_meter.setVisible(self._cfg.config.show_vu_meter)

            # Update always-on-top
            if self._cfg.config.always_on_top:
                self.setWindowFlags(
                    self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
                )
            else:
                self.setWindowFlags(
                    self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint
                )
            self.show()

    # -- OTA updates -------------------------------------------------------

    def _check_for_updates(self) -> None:
        self._update_checker = UpdateChecker(GITHUB_REPO, self)
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.check()

    def _on_update_available(self, info: UpdateInfo) -> None:
        should_exit = prompt_and_install(info, parent=self)
        if should_exit:
            self.close()

    def check_for_updates_manual(self) -> None:
        """Triggered from Settings dialog — always checks, shows 'no update' feedback."""
        checker = UpdateChecker(GITHUB_REPO, self)
        checker.update_available.connect(self._on_update_available)
        checker.no_update.connect(self._on_no_update)
        checker.check_error.connect(self._on_update_check_error)
        self._update_checker = checker

    def _on_no_update(self) -> None:
        from version import APP_VERSION
        QMessageBox.information(
            self, "No Update Available",
            f"You are running the latest version ({APP_VERSION}).",
        )

    def _on_update_check_error(self, msg: str) -> None:
        QMessageBox.warning(
            self, "Update Check Failed",
            f"Could not check for updates:\n\n{msg}",
        )

    # -- window lifecycle --------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._pipeline.is_running:
            self._pipeline.stop()
        self._audio_router.shutdown()
        self._hotkey.stop()
        self._tray.hide()
        self._cfg.save()
        super().closeEvent(event)
