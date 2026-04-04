from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QPushButton, QWidget

from core.audio_router import AudioRouter
from core.models import AudioDevice, DeviceDirection


class DeviceSelector(QWidget):
    """Combo box for selecting an audio device, with a refresh button."""

    device_changed = pyqtSignal(object)  # AudioDevice | None

    def __init__(
        self,
        audio_router: AudioRouter,
        direction: DeviceDirection,
        label_text: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._router = audio_router
        self._direction = direction
        self._devices: list[AudioDevice] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._combo = QComboBox()
        self._combo.setMinimumWidth(300)
        self._combo.currentIndexChanged.connect(self._on_index_changed)
        layout.addWidget(self._combo, 1)

        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setFixedWidth(32)
        self._refresh_btn.setToolTip("Refresh device list")
        self._refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(self._refresh_btn)

        self.refresh()

    def refresh(self) -> None:
        self._combo.blockSignals(True)
        current_text = self._combo.currentText()
        self._combo.clear()

        if self._direction == DeviceDirection.INPUT:
            self._devices = self._router.list_input_devices()
        else:
            self._devices = self._router.list_output_devices()

        self._combo.addItem("(System Default)", None)
        for dev in self._devices:
            self._combo.addItem(dev.display_name(), dev.device_id)

        # Try to re-select previous device
        restored = False
        for i in range(self._combo.count()):
            if self._combo.itemText(i) == current_text:
                self._combo.setCurrentIndex(i)
                restored = True
                break

        if not restored:
            self._combo.setCurrentIndex(0)

        self._combo.blockSignals(False)

    def select_by_name(self, name: str | None) -> None:
        if not name:
            self._combo.setCurrentIndex(0)
            return
        for i in range(self._combo.count()):
            text = self._combo.itemText(i)
            if name in text or text in name:
                self._combo.setCurrentIndex(i)
                return
        self._combo.setCurrentIndex(0)

    def selected_device(self) -> AudioDevice | None:
        idx = self._combo.currentIndex()
        if idx <= 0:
            return None
        dev_idx = idx - 1  # offset for "(System Default)"
        if 0 <= dev_idx < len(self._devices):
            return self._devices[dev_idx]
        return None

    def selected_device_name(self) -> str | None:
        dev = self.selected_device()
        return dev.name if dev else None

    def _on_index_changed(self, index: int) -> None:
        self.device_changed.emit(self.selected_device())
