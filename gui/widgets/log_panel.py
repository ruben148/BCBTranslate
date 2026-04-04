from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QTextEdit, QVBoxLayout, QWidget


class LogPanel(QWidget):
    """Scrollable, color-coded translation log."""

    MAX_BLOCKS = 2000  # max lines before pruning

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._text.setFont(self._text.font())
        layout.addWidget(self._text)

        self._has_partial = False

    @pyqtSlot(str, str, int)
    def add_translation(self, source: str, translated: str, lag_ms: int = 0) -> None:
        self._remove_partial()
        ts = datetime.now().strftime("%H:%M:%S")
        lag_str = f"  ({lag_ms / 1000:.1f}s)" if lag_ms > 0 else ""

        self._append(f"[{ts}]  RO: {source}", QColor(180, 180, 180))
        self._append(f"[{ts}]  EN: {translated}{lag_str}", QColor(100, 200, 255))
        self._append("", None)

    @pyqtSlot(str)
    def add_partial(self, text: str) -> None:
        self._remove_partial()
        ts = datetime.now().strftime("%H:%M:%S")
        self._append(f"[{ts}]  ... {text}", QColor(120, 120, 120))
        self._has_partial = True

    @pyqtSlot(str)
    def add_status(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._append(f"[{ts}]  ⓘ {message}", QColor(255, 235, 59))

    @pyqtSlot(str)
    def add_error(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._append(f"[{ts}]  ✖ {message}", QColor(244, 67, 54))

    def clear(self) -> None:
        self._text.clear()
        self._has_partial = False

    def _remove_partial(self) -> None:
        """Remove the last partial line so it can be replaced."""
        if not self._has_partial:
            return
        self._has_partial = False
        doc = self._text.document()
        if doc.blockCount() < 2:
            self._text.clear()
            return
        cursor = QTextCursor(doc.lastBlock())
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        cursor.removeSelectedText()
        cursor.deletePreviousChar()

    def _append(self, text: str, color: QColor | None) -> None:
        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        if color:
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            cursor.insertText(text + "\n", fmt)
        else:
            cursor.insertText(text + "\n")

        # Auto-prune old content
        if self._text.document().blockCount() > self.MAX_BLOCKS:
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(
                QTextCursor.MoveOperation.Down,
                QTextCursor.MoveMode.KeepAnchor,
                500,
            )
            cursor.removeSelectedText()

        self._text.setTextCursor(cursor)
        self._text.ensureCursorVisible()
