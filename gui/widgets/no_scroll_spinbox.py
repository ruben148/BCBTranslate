from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QComboBox, QDoubleSpinBox, QSlider, QSpinBox

# Must match stylesheet: horizontal handle (16px) + groove margin; avoids clipping
_SLIDER_MIN_HEIGHT_HORIZONTAL = 26


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

    def sizeHint(self) -> QSize:
        sh = super().sizeHint()
        if self.orientation() == Qt.Orientation.Horizontal:
            return QSize(sh.width(), max(sh.height(), _SLIDER_MIN_HEIGHT_HORIZONTAL))
        return sh

    def minimumSizeHint(self) -> QSize:
        msh = super().minimumSizeHint()
        if self.orientation() == Qt.Orientation.Horizontal:
            return QSize(msh.width(), max(msh.height(), _SLIDER_MIN_HEIGHT_HORIZONTAL))
        return msh
