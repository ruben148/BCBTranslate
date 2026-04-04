from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QColor, QLinearGradient, QPainter
from PyQt6.QtWidgets import QWidget


class VUMeter(QWidget):
    """Horizontal audio level meter with green-yellow-red gradient."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._level: float = 0.0
        self._peak: float = 0.0
        self._peak_decay = 0.02
        self.setMinimumHeight(18)
        self.setMaximumHeight(22)

        self._decay_timer = QTimer(self)
        self._decay_timer.timeout.connect(self._decay_peak)
        self._decay_timer.start(50)

    @pyqtSlot(float)
    def set_level(self, rms: float) -> None:
        self._level = min(1.0, rms * 5.0)  # amplify for visibility
        if self._level > self._peak:
            self._peak = self._level
        self.update()

    def _decay_peak(self) -> None:
        if self._peak > self._level:
            self._peak = max(self._level, self._peak - self._peak_decay)
            self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        radius = h // 2

        # Background
        p.setBrush(QColor(40, 40, 40))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, w, h, radius, radius)

        # Level bar with gradient
        bar_w = int(w * self._level)
        if bar_w > 0:
            grad = QLinearGradient(0, 0, w, 0)
            grad.setColorAt(0.0, QColor(76, 175, 80))   # green
            grad.setColorAt(0.6, QColor(255, 235, 59))   # yellow
            grad.setColorAt(1.0, QColor(244, 67, 54))    # red
            p.setBrush(grad)
            p.drawRoundedRect(0, 0, bar_w, h, radius, radius)

        # Peak indicator
        peak_x = int(w * self._peak)
        if peak_x > 2:
            p.setPen(QColor(255, 255, 255, 180))
            p.drawLine(peak_x, 2, peak_x, h - 2)

        p.end()
