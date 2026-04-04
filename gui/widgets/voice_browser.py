from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from core.azure_wrapper import AzureTranslationService

logger = logging.getLogger(__name__)


class VoiceBrowser(QWidget):
    """Browse and preview Azure TTS voices with locale filter."""

    voice_selected = pyqtSignal(str)  # voice short name

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._azure: AzureTranslationService | None = None
        self._voices: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self._filter_input = QLineEdit()
        self._filter_input.setPlaceholderText("e.g. en-US, Jenny, Neural...")
        self._filter_input.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_input, 1)

        self._load_btn = QPushButton("Load Voices")
        self._load_btn.clicked.connect(self._load_voices)
        filter_row.addWidget(self._load_btn)
        layout.addLayout(filter_row)

        # Voice combo
        self._combo = QComboBox()
        self._combo.currentIndexChanged.connect(self._on_selection_changed)
        layout.addWidget(self._combo)

        # Info + preview
        info_row = QHBoxLayout()
        self._info_label = QLabel("")
        self._info_label.setWordWrap(True)
        info_row.addWidget(self._info_label, 1)

        self._preview_btn = QPushButton("Preview")
        self._preview_btn.setEnabled(False)
        self._preview_btn.clicked.connect(self._preview)
        info_row.addWidget(self._preview_btn)
        layout.addLayout(info_row)

    def set_azure_service(self, azure: AzureTranslationService) -> None:
        self._azure = azure

    def set_current_voice(self, voice_name: str) -> None:
        for i in range(self._combo.count()):
            if self._combo.itemData(i) == voice_name:
                self._combo.setCurrentIndex(i)
                return

    def _load_voices(self) -> None:
        if not self._azure:
            return
        self._load_btn.setText("Loading...")
        self._load_btn.setEnabled(False)

        try:
            self._voices = self._azure.list_voices()
            self._populate_combo(self._voices)
        except Exception:
            logger.exception("Failed to load voices")
        finally:
            self._load_btn.setText("Load Voices")
            self._load_btn.setEnabled(True)

    def _populate_combo(self, voices: list) -> None:
        self._combo.blockSignals(True)
        self._combo.clear()
        for v in voices:
            display = f"{v.short_name} — {v.local_name}"
            self._combo.addItem(display, v.short_name)
        self._combo.blockSignals(False)
        if self._combo.count() > 0:
            self._combo.setCurrentIndex(0)
            self._on_selection_changed(0)

    def _apply_filter(self, text: str) -> None:
        text_lower = text.lower()
        filtered = [
            v for v in self._voices
            if text_lower in v.short_name.lower()
            or text_lower in getattr(v, "local_name", "").lower()
        ]
        self._populate_combo(filtered)

    def _on_selection_changed(self, index: int) -> None:
        voice_name = self._combo.currentData()
        if voice_name:
            self._preview_btn.setEnabled(True)
            voice_obj = next(
                (v for v in self._voices if v.short_name == voice_name), None
            )
            if voice_obj:
                gender = getattr(voice_obj, "gender", "")
                locale = getattr(voice_obj, "locale", "")
                self._info_label.setText(
                    f"Locale: {locale}  |  Gender: {gender}"
                )
            self.voice_selected.emit(voice_name)
        else:
            self._preview_btn.setEnabled(False)
            self._info_label.setText("")

    def _preview(self) -> None:
        voice_name = self._combo.currentData()
        if not voice_name or not self._azure:
            return
        try:
            from utils.ssml_builder import SSMLBuilder
            ssml = SSMLBuilder.build(
                "Hello, this is a preview of the selected voice.",
                voice=voice_name,
            )
            self._azure.synthesize_ssml(ssml)
        except Exception:
            logger.exception("Voice preview failed")
