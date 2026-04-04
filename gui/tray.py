from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtGui import QAction, QColor, QIcon, QPixmap, QPainter
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

if TYPE_CHECKING:
    from gui.main_window import MainWindow

logger = logging.getLogger(__name__)


def _create_color_icon(color: QColor, size: int = 64) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color)
    painter.setPen(color.darker(120))
    margin = size // 8
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    painter.end()
    return QIcon(pixmap)


class TrayIcon:
    """System tray icon with status color and context menu."""

    COLOR_GREEN = QColor(76, 175, 80)
    COLOR_YELLOW = QColor(255, 235, 59)
    COLOR_RED = QColor(244, 67, 54)
    COLOR_GRAY = QColor(128, 128, 128)

    def __init__(self, main_window: MainWindow):
        self._window = main_window
        self._tray = QSystemTrayIcon(main_window)
        self._build_menu()
        self.set_status_color(self.COLOR_GRAY)
        self._tray.activated.connect(self._on_activated)

    def _build_menu(self) -> None:
        menu = QMenu()

        self._toggle_action = QAction("Start", menu)
        self._toggle_action.triggered.connect(self._window.toggle_translation)
        menu.addAction(self._toggle_action)

        menu.addSeparator()

        show_action = QAction("Show", menu)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)

        settings_action = QAction("Settings", menu)
        settings_action.triggered.connect(self._window.open_settings)
        menu.addAction(settings_action)

        menu.addSeparator()

        exit_action = QAction("Exit", menu)
        exit_action.triggered.connect(self._window.close)
        menu.addAction(exit_action)

        self._tray.setContextMenu(menu)

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def set_status_color(self, color: QColor) -> None:
        icon = _create_color_icon(color)
        self._tray.setIcon(icon)

    def set_running(self, running: bool) -> None:
        self._toggle_action.setText("Stop" if running else "Start")
        if not running:
            self.set_status_color(self.COLOR_GRAY)

    def update_lag(self, lag_ms: int) -> None:
        if lag_ms <= 2000:
            self.set_status_color(self.COLOR_GREEN)
        elif lag_ms <= 4000:
            self.set_status_color(self.COLOR_YELLOW)
        else:
            self.set_status_color(self.COLOR_RED)

        secs = lag_ms / 1000
        self._tray.setToolTip(f"BCBTranslate — Lag: {secs:.1f}s")

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self) -> None:
        self._window.showNormal()
        self._window.activateWindow()
        self._window.raise_()
