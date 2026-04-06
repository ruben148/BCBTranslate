from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.audio_router import AudioRouter
from core.azure_wrapper import AzureTranslationService
from core.config_manager import ConfigManager
from gui.widgets.no_scroll_spinbox import NoScrollComboBox
from gui.widgets.voice_browser import VoiceBrowser
from version import APP_VERSION, GITHUB_REPO

if TYPE_CHECKING:
    from core.translation_pipeline import TranslationPipeline

logger = logging.getLogger(__name__)

SOURCE_LANGUAGES = [
    ("ro-RO", "Romanian"),
    ("en-US", "English (US)"),
    ("en-GB", "English (UK)"),
    ("de-DE", "German"),
    ("fr-FR", "French"),
    ("es-ES", "Spanish"),
    ("it-IT", "Italian"),
    ("pt-BR", "Portuguese (BR)"),
    ("ru-RU", "Russian"),
    ("uk-UA", "Ukrainian"),
    ("pl-PL", "Polish"),
    ("hu-HU", "Hungarian"),
    ("nl-NL", "Dutch"),
    ("ja-JP", "Japanese"),
    ("zh-CN", "Chinese (Simplified)"),
    ("ko-KR", "Korean"),
    ("ar-SA", "Arabic"),
]

TARGET_LANGUAGES = [
    ("en", "English"),
    ("ro", "Romanian"),
    ("de", "German"),
    ("fr", "French"),
    ("es", "Spanish"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("ru", "Russian"),
    ("uk", "Ukrainian"),
    ("pl", "Polish"),
    ("hu", "Hungarian"),
    ("nl", "Dutch"),
    ("ja", "Japanese"),
    ("zh-Hans", "Chinese (Simplified)"),
    ("ko", "Korean"),
    ("ar", "Arabic"),
]

PROFANITY_OPTIONS = [
    ("masked", "Masked (****)"),
    ("removed", "Removed"),
    ("raw", "Raw (no filter)"),
]


class SettingsDialog(QDialog):
    def __init__(
        self,
        config_manager: ConfigManager,
        audio_router: AudioRouter,
        pipeline: TranslationPipeline,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._cfg = config_manager
        self._audio_router = audio_router
        self._pipeline = pipeline
        self._ephemeral_voice_azure: AzureTranslationService | None = None

        self.setWindowTitle("Settings — BCBTranslate")
        self.setMinimumSize(560, 520)
        self.resize(600, 580)

        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_azure_tab(), "Azure")
        tabs.addTab(self._build_translation_tab(), "Translation")
        tabs.addTab(self._build_voice_tab(), "Voice")
        tabs.addTab(self._build_behavior_tab(), "Behavior")
        tabs.addTab(self._build_logging_tab(), "Logging")
        tabs.addTab(self._build_interface_tab(), "Interface")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def done(self, result: int) -> None:
        if self._ephemeral_voice_azure is not None:
            try:
                self._ephemeral_voice_azure.shutdown()
            except Exception:
                logger.exception(
                    "Failed to shut down ephemeral Azure client for voice browser"
                )
            self._ephemeral_voice_azure = None
        super().done(result)

    # -- Azure tab ---------------------------------------------------------

    def _build_azure_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Azure Speech credentials are read from environment variables.\n"
            "Set them in your .env file or system environment."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()

        self._key_var_edit = QLineEdit()
        form.addRow("Key env var:", self._key_var_edit)

        self._region_var_edit = QLineEdit()
        form.addRow("Region env var:", self._region_var_edit)

        layout.addLayout(form)

        # Connection test
        test_row = QHBoxLayout()
        self._test_btn = QPushButton("Test Connection")
        self._test_btn.clicked.connect(self._test_connection)
        test_row.addWidget(self._test_btn)
        self._test_result = QLabel("")
        test_row.addWidget(self._test_result, 1)
        layout.addLayout(test_row)

        # Status
        has_creds = self._cfg.has_azure_credentials()
        status = "Credentials found" if has_creds else "Credentials NOT found"
        color = "#4caf50" if has_creds else "#f44336"
        status_label = QLabel(status)
        status_label.setStyleSheet(f"color: {color}; font-weight: bold;")
        layout.addWidget(status_label)

        layout.addStretch()
        return w

    # -- Translation tab ---------------------------------------------------

    def _build_translation_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        form = QFormLayout()

        self._mode_combo = NoScrollComboBox()
        self._mode_combo.addItem("Standard", "standard")
        self._mode_combo.addItem("Live Interpreter", "interpreter")
        self._mode_combo.setToolTip(
            "Standard: app-side TTS with speed/pitch control.\n"
            "Live Interpreter: server-side TTS via v2 endpoint\n"
            "(auto language detection, lower latency, no speed/pitch)."
        )
        self._mode_combo.currentIndexChanged.connect(
            self._on_mode_changed_settings
        )
        form.addRow("Translation mode:", self._mode_combo)

        self._source_combo = QComboBox()
        for code, name in SOURCE_LANGUAGES:
            self._source_combo.addItem(f"{name} ({code})", code)
        form.addRow("Source language:", self._source_combo)

        self._target_combo = QComboBox()
        for code, name in TARGET_LANGUAGES:
            self._target_combo.addItem(f"{name} ({code})", code)
        form.addRow("Target language:", self._target_combo)

        self._profanity_combo = QComboBox()
        for code, name in PROFANITY_OPTIONS:
            self._profanity_combo.addItem(name, code)
        form.addRow("Profanity filter:", self._profanity_combo)

        self._noise_cb = QCheckBox("Enable noise suppression on input")
        form.addRow("", self._noise_cb)

        layout.addLayout(form)

        # Audio input group
        audio_group = QGroupBox("Audio Input")
        aform = QFormLayout(audio_group)

        self._gain_spin = QDoubleSpinBox()
        self._gain_spin.setRange(0.0, 5.0)
        self._gain_spin.setSingleStep(0.1)
        self._gain_spin.setDecimals(1)
        self._gain_spin.setSuffix("×")
        aform.addRow("Input gain:", self._gain_spin)

        layout.addWidget(audio_group)

        self._seg_settings_group = QGroupBox("Segmentation (Standard only)")
        seg_form = QFormLayout(self._seg_settings_group)

        self._default_seg_cb = QCheckBox(
            "Use Azure default segmentation (semantic always on)"
        )
        self._default_seg_cb.setToolTip(
            "When enabled, custom silence timeout and auto-adjust are not sent to\n"
            "Azure — the service uses its defaults. Semantic segmentation stays on."
        )
        self._default_seg_cb.toggled.connect(self._on_default_seg_toggled_settings)
        seg_form.addRow("", self._default_seg_cb)

        self._seg_timeout_spin = QSpinBox()
        self._seg_timeout_spin.setRange(100, 5000)
        self._seg_timeout_spin.setSingleStep(100)
        self._seg_timeout_spin.setSuffix(" ms")
        self._seg_timeout_spin.setToolTip(
            "How long a silence gap (in ms) must last before Azure finalises\n"
            "the current utterance and starts a new one.\n\n"
            "Lower values → more frequent, shorter utterances (good for\n"
            "speakers who rarely pause).\n"
            "Higher values → fewer, longer utterances."
        )
        seg_form.addRow("Segmentation silence:", self._seg_timeout_spin)

        self._auto_seg_cb = QCheckBox("Auto-adjust segmentation timeout")
        self._auto_seg_cb.setToolTip(
            "Automatically tune the segmentation silence timeout based on\n"
            "observed utterance durations so they stay within the target range.\n\n"
            "The algorithm adjusts the API's silence threshold — it never\n"
            "cuts audio on the application side."
        )
        self._auto_seg_cb.toggled.connect(self._on_auto_seg_toggled)
        seg_form.addRow("", self._auto_seg_cb)

        self._auto_seg_min_spin = QDoubleSpinBox()
        self._auto_seg_min_spin.setRange(1.0, 30.0)
        self._auto_seg_min_spin.setSingleStep(1.0)
        self._auto_seg_min_spin.setDecimals(1)
        self._auto_seg_min_spin.setSuffix(" s")
        seg_form.addRow("Target min duration:", self._auto_seg_min_spin)

        self._auto_seg_max_spin = QDoubleSpinBox()
        self._auto_seg_max_spin.setRange(5.0, 55.0)
        self._auto_seg_max_spin.setSingleStep(1.0)
        self._auto_seg_max_spin.setDecimals(1)
        self._auto_seg_max_spin.setSuffix(" s")
        seg_form.addRow("Target max duration:", self._auto_seg_max_spin)

        layout.addWidget(self._seg_settings_group)
        layout.addStretch()
        return w

    # -- Voice tab ---------------------------------------------------------

    def _build_voice_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        self._voice_browser = VoiceBrowser()
        azure: AzureTranslationService | None = self._pipeline._azure
        if azure is None and self._cfg.has_azure_credentials():
            key = self._cfg.azure_speech_key()
            region = self._cfg.azure_speech_region()
            if key and region:
                self._ephemeral_voice_azure = AzureTranslationService(
                    key,
                    region,
                    self._cfg.config,
                    self._audio_router,
                    parent=self,
                )
                azure = self._ephemeral_voice_azure
        if azure is not None:
            self._voice_browser.set_azure_service(azure)
        layout.addWidget(self._voice_browser)

        self._voice_tuning_group = QGroupBox("Voice Tuning (Standard only)")
        vt_form = QFormLayout(self._voice_tuning_group)

        self._rate_spin = QDoubleSpinBox()
        self._rate_spin.setRange(0.5, 2.0)
        self._rate_spin.setSingleStep(0.1)
        self._rate_spin.setDecimals(1)
        vt_form.addRow("Speaking rate:", self._rate_spin)

        self._pitch_edit = QLineEdit()
        self._pitch_edit.setPlaceholderText("+0%")
        vt_form.addRow("Pitch:", self._pitch_edit)

        self._volume_spin = QSpinBox()
        self._volume_spin.setRange(0, 100)
        vt_form.addRow("TTS volume:", self._volume_spin)

        layout.addWidget(self._voice_tuning_group)
        layout.addStretch()
        return w

    # -- Behavior tab ------------------------------------------------------

    def _build_behavior_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        queue_group = QGroupBox("TTS Queue")
        qform = QFormLayout(queue_group)

        self._warn_thresh_spin = QSpinBox()
        self._warn_thresh_spin.setRange(1, 20)
        qform.addRow("Warning threshold:", self._warn_thresh_spin)

        self._max_queue_spin = QSpinBox()
        self._max_queue_spin.setRange(2, 50)
        qform.addRow("Max queue size:", self._max_queue_spin)

        self._drop_oldest_cb = QCheckBox("Drop oldest utterance on overflow")
        qform.addRow("", self._drop_oldest_cb)

        self._adaptive_cb = QCheckBox("Enable adaptive speaking rate")
        qform.addRow("", self._adaptive_cb)

        layout.addWidget(queue_group)

        reconnect_group = QGroupBox("Reconnection")
        rform = QFormLayout(reconnect_group)

        self._reconnect_spin = QSpinBox()
        self._reconnect_spin.setRange(0, 20)
        rform.addRow("Max attempts:", self._reconnect_spin)

        self._reconnect_delay_spin = QSpinBox()
        self._reconnect_delay_spin.setRange(1, 30)
        self._reconnect_delay_spin.setSuffix(" s")
        rform.addRow("Base delay:", self._reconnect_delay_spin)

        layout.addWidget(reconnect_group)
        layout.addStretch()
        return w

    # -- Logging tab -------------------------------------------------------

    def _build_logging_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        form = QFormLayout()

        self._log_level_combo = QComboBox()
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self._log_level_combo.addItem(level)
        form.addRow("Log level:", self._log_level_combo)

        self._log_to_file_cb = QCheckBox("Log to file")
        form.addRow("", self._log_to_file_cb)

        self._log_dir_edit = QLineEdit()
        self._log_dir_edit.setPlaceholderText("Log directory...")
        form.addRow("Log directory:", self._log_dir_edit)

        self._transcript_cb = QCheckBox("Save translation transcripts")
        form.addRow("", self._transcript_cb)

        self._transcript_dir_edit = QLineEdit()
        self._transcript_dir_edit.setPlaceholderText("Transcript directory...")
        form.addRow("Transcript dir:", self._transcript_dir_edit)

        layout.addLayout(form)
        layout.addStretch()
        return w

    # -- Interface tab -----------------------------------------------------

    def _build_interface_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        form = QFormLayout()

        self._theme_combo = QComboBox()
        self._theme_combo.addItem("Dark", "dark")
        self._theme_combo.addItem("Light", "light")
        form.addRow("Theme:", self._theme_combo)

        self._aot_cb = QCheckBox("Always on top")
        form.addRow("", self._aot_cb)

        self._minimized_cb = QCheckBox("Start minimized to tray")
        form.addRow("", self._minimized_cb)

        self._vu_cb = QCheckBox("Show VU meter")
        form.addRow("", self._vu_cb)

        self._hotkey_edit = QLineEdit()
        self._hotkey_edit.setPlaceholderText("e.g. Ctrl+Shift+T")
        form.addRow("Start/Stop hotkey:", self._hotkey_edit)

        layout.addLayout(form)

        # Updates group
        update_group = QGroupBox("Updates")
        uform = QFormLayout(update_group)

        self._auto_update_cb = QCheckBox("Check for updates on startup")
        uform.addRow("", self._auto_update_cb)

        update_row = QHBoxLayout()
        self._check_update_btn = QPushButton("Check for Updates Now")
        self._check_update_btn.clicked.connect(self._on_check_updates_clicked)
        self._check_update_btn.setEnabled(bool(GITHUB_REPO))
        update_row.addWidget(self._check_update_btn)
        self._version_label = QLabel(f"Current version: {APP_VERSION}")
        update_row.addWidget(self._version_label)
        update_row.addStretch()
        uform.addRow(update_row)

        if not GITHUB_REPO:
            hint = QLabel("OTA updates are disabled (GITHUB_REPO not set in version.py)")
            hint.setStyleSheet("color: #888; font-style: italic;")
            uform.addRow(hint)

        layout.addWidget(update_group)

        layout.addStretch()
        return w

    # -- load / save -------------------------------------------------------

    def _load_values(self) -> None:
        cfg = self._cfg.config

        # Azure
        self._key_var_edit.setText(cfg.speech_key_env_var)
        self._region_var_edit.setText(cfg.speech_region_env_var)

        # Translation
        self._select_combo_data(self._mode_combo, cfg.translation_mode)
        self._select_combo_data(self._source_combo, cfg.source_language)
        self._select_combo_data(self._target_combo, cfg.target_language)
        self._select_combo_data(self._profanity_combo, cfg.profanity_filter)
        self._noise_cb.setChecked(cfg.noise_suppression)
        self._gain_spin.setValue(cfg.input_gain)
        self._default_seg_cb.setChecked(cfg.use_default_segmentation)
        self._seg_timeout_spin.setValue(cfg.segmentation_silence_timeout_ms)
        self._auto_seg_cb.setChecked(cfg.auto_segmentation_enabled)
        self._auto_seg_min_spin.setValue(cfg.auto_seg_target_min_s)
        self._auto_seg_max_spin.setValue(cfg.auto_seg_target_max_s)
        self._refresh_settings_mode_visibility()
        self._sync_default_seg_dependencies()

        # Voice
        self._voice_browser.set_current_voice(cfg.voice_name)
        self._rate_spin.setValue(cfg.speaking_rate)
        self._pitch_edit.setText(cfg.pitch)
        self._volume_spin.setValue(cfg.tts_volume)

        # Behavior
        self._warn_thresh_spin.setValue(cfg.tts_queue_warning_threshold)
        self._max_queue_spin.setValue(cfg.max_tts_queue_size)
        self._drop_oldest_cb.setChecked(cfg.drop_oldest_on_overflow)
        self._adaptive_cb.setChecked(cfg.adaptive_rate_enabled)
        self._reconnect_spin.setValue(cfg.reconnect_attempts)
        self._reconnect_delay_spin.setValue(cfg.reconnect_delay_seconds)

        # Logging
        self._select_combo_text(self._log_level_combo, cfg.log_level)
        self._log_to_file_cb.setChecked(cfg.log_to_file)
        self._log_dir_edit.setText(cfg.log_directory)
        self._transcript_cb.setChecked(cfg.save_transcripts)
        self._transcript_dir_edit.setText(cfg.transcript_directory)

        # Interface
        self._select_combo_data(self._theme_combo, cfg.theme)
        self._aot_cb.setChecked(cfg.always_on_top)
        self._minimized_cb.setChecked(cfg.start_minimized)
        self._vu_cb.setChecked(cfg.show_vu_meter)
        self._hotkey_edit.setText(cfg.hotkey_start_stop)
        self._auto_update_cb.setChecked(cfg.auto_check_updates)

    def _save(self) -> None:
        c = self._cfg.config
        seg_before = (
            c.use_default_segmentation,
            c.segmentation_silence_timeout_ms,
            c.auto_segmentation_enabled,
            c.auto_seg_target_min_s,
            c.auto_seg_target_max_s,
            c.noise_suppression,
        )

        self._cfg.update(
            # Azure
            speech_key_env_var=self._key_var_edit.text().strip(),
            speech_region_env_var=self._region_var_edit.text().strip(),
            # Translation
            translation_mode=self._mode_combo.currentData(),
            source_language=self._source_combo.currentData(),
            target_language=self._target_combo.currentData(),
            profanity_filter=self._profanity_combo.currentData(),
            noise_suppression=self._noise_cb.isChecked(),
            input_gain=self._gain_spin.value(),
            use_default_segmentation=self._default_seg_cb.isChecked(),
            segmentation_silence_timeout_ms=self._seg_timeout_spin.value(),
            auto_segmentation_enabled=self._auto_seg_cb.isChecked(),
            auto_seg_target_min_s=self._auto_seg_min_spin.value(),
            auto_seg_target_max_s=self._auto_seg_max_spin.value(),
            # Voice
            speaking_rate=self._rate_spin.value(),
            pitch=self._pitch_edit.text().strip() or "+0%",
            tts_volume=self._volume_spin.value(),
            # Behavior
            tts_queue_warning_threshold=self._warn_thresh_spin.value(),
            max_tts_queue_size=self._max_queue_spin.value(),
            drop_oldest_on_overflow=self._drop_oldest_cb.isChecked(),
            adaptive_rate_enabled=self._adaptive_cb.isChecked(),
            reconnect_attempts=self._reconnect_spin.value(),
            reconnect_delay_seconds=self._reconnect_delay_spin.value(),
            # Logging
            log_level=self._log_level_combo.currentText(),
            log_to_file=self._log_to_file_cb.isChecked(),
            log_directory=self._log_dir_edit.text().strip(),
            save_transcripts=self._transcript_cb.isChecked(),
            transcript_directory=self._transcript_dir_edit.text().strip(),
            # Interface
            theme=self._theme_combo.currentData(),
            always_on_top=self._aot_cb.isChecked(),
            start_minimized=self._minimized_cb.isChecked(),
            show_vu_meter=self._vu_cb.isChecked(),
            hotkey_start_stop=self._hotkey_edit.text().strip() or "Ctrl+Shift+T",
            auto_check_updates=self._auto_update_cb.isChecked(),
        )

        # Handle voice selection from browser
        voice_name = self._voice_browser._combo.currentData()
        if voice_name:
            self._cfg.set("voice_name", voice_name)

        self._cfg.save()

        seg_after = (
            self._cfg.config.use_default_segmentation,
            self._cfg.config.segmentation_silence_timeout_ms,
            self._cfg.config.auto_segmentation_enabled,
            self._cfg.config.auto_seg_target_min_s,
            self._cfg.config.auto_seg_target_max_s,
            self._cfg.config.noise_suppression,
        )
        if seg_before != seg_after and self._pipeline.is_running:
            self._pipeline.apply_segmentation_mode_change()

        self.accept()

    def _test_connection(self) -> None:
        self._test_btn.setEnabled(False)
        self._test_result.setText("Testing...")

        key = self._cfg.azure_speech_key()
        region = self._cfg.azure_speech_region()
        if not key or not region:
            self._test_result.setText("Credentials not found in environment")
            self._test_result.setStyleSheet("color: #f44336;")
            self._test_btn.setEnabled(True)
            return

        try:
            svc = AzureTranslationService(
                key, region, self._cfg.config, self._audio_router
            )
            ok, msg = svc.test_connection()
            if ok:
                self._test_result.setText("Connection successful!")
                self._test_result.setStyleSheet("color: #4caf50;")
            else:
                self._test_result.setText(msg)
                self._test_result.setStyleSheet("color: #f44336;")
        except Exception as exc:
            self._test_result.setText(str(exc))
            self._test_result.setStyleSheet("color: #f44336;")
        finally:
            self._test_btn.setEnabled(True)

    def _on_auto_seg_toggled(self, checked: bool) -> None:
        self._sync_default_seg_dependencies()

    def _on_default_seg_toggled_settings(self, checked: bool) -> None:
        self._sync_default_seg_dependencies()

    def _sync_default_seg_dependencies(self) -> None:
        custom = not self._default_seg_cb.isChecked()
        self._seg_timeout_spin.setEnabled(custom)
        self._auto_seg_cb.setEnabled(custom)
        auto_on = custom and self._auto_seg_cb.isChecked()
        self._auto_seg_min_spin.setEnabled(auto_on)
        self._auto_seg_max_spin.setEnabled(auto_on)

    def _on_mode_changed_settings(self, _index: int) -> None:
        self._refresh_settings_mode_visibility()

    def _refresh_settings_mode_visibility(self) -> None:
        is_standard = self._mode_combo.currentData() == "standard"
        self._seg_settings_group.setVisible(is_standard)
        self._voice_tuning_group.setVisible(is_standard)
        self._source_combo.setEnabled(is_standard)

    def _on_check_updates_clicked(self) -> None:
        main_win = self.parent()
        if main_win and hasattr(main_win, "check_for_updates_manual"):
            self.close()
            main_win.check_for_updates_manual()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _select_combo_data(combo: QComboBox, data) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                return

    @staticmethod
    def _select_combo_text(combo: QComboBox, text: str) -> None:
        idx = combo.findText(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)
