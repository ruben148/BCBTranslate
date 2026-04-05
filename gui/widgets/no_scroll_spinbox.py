from PyQt6.QtWidgets import QComboBox, QDoubleSpinBox, QSlider, QSpinBox


class NoScrollComboBox(QComboBox):
    """QComboBox that never changes selection via the scroll wheel."""

    def wheelEvent(self, event):
        event.ignore()


class NoScrollSpinBox(QSpinBox):
    """QSpinBox that never changes value via the scroll wheel."""

    def wheelEvent(self, event):
        event.ignore()


class NoScrollDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that never changes value via the scroll wheel."""

    def wheelEvent(self, event):
        event.ignore()


class NoScrollSlider(QSlider):
    """QSlider that never changes value via the scroll wheel."""

    def wheelEvent(self, event):
        event.ignore()
