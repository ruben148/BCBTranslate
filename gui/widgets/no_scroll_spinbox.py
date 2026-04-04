from PyQt6.QtWidgets import QDoubleSpinBox, QSpinBox


class NoScrollSpinBox(QSpinBox):
    """QSpinBox that ignores scroll-wheel events unless the widget has focus.

    This prevents accidental value changes while scrolling a parent scroll area.
    """

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoScrollDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that ignores scroll-wheel events unless the widget has focus."""

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()
