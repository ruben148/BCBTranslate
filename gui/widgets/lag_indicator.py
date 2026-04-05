from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

# (max_lag_ms, dot_count, color)
LAG_LEVELS = [
    (1000, 1, QColor(76, 175, 80)),     # green — excellent
    (2000, 2, QColor(76, 175, 80)),     # green — good
    (4000, 3, QColor(255, 235, 59)),    # yellow — noticeable
    (6000, 4, QColor(255, 152, 0)),     # orange — warning
    (999999, 5, QColor(244, 67, 54)),   # red — critical
]


class _DotWidget(QWidget):
    """Renders the colored dots."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._active = 0
        self._total = 5
        self._color = QColor(76, 175, 80)
        self.setFixedSize(72, 16)

    def set_state(self, active: int, color: QColor) -> None:
        self._active = active
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dot_r = 5
        spacing = 12
        x_start = 3
        y_center = self.height() // 2

        for i in range(self._total):
            x = x_start + i * spacing
            if i < self._active:
                p.setBrush(self._color)
            else:
                p.setBrush(QColor(80, 80, 80))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(x, y_center - dot_r // 2, dot_r, dot_r)
        p.end()


class LagIndicator(QWidget):
    """Shows lag as colored dots + numeric label."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._label = QLabel("Lag:")
        self._label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._label)

        self._dots = _DotWidget(self)
        layout.addWidget(self._dots)

        self._value = QLabel("—")
        layout.addWidget(self._value)

    def set_lag(self, lag_ms: int) -> None:
        for max_ms, dots, color in LAG_LEVELS:
            if lag_ms <= max_ms:
                self._dots.set_state(dots, color)
                break

        if lag_ms == 0:
            self._value.setText("—")
            self._value.setStyleSheet("")
        else:
            secs = lag_ms / 1000
            self._value.setText(f"{secs:.1f}s")
            color = LAG_LEVELS[-1][2]
            for max_ms, _, c in LAG_LEVELS:
                if lag_ms <= max_ms:
                    color = c
                    break
            self._value.setStyleSheet(f"color: {color.name()};")
