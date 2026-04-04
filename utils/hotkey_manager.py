from __future__ import annotations

import logging
import threading
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)


def _parse_hotkey(hotkey_str: str) -> tuple[set[str], str | None]:
    """Parse 'Ctrl+Shift+T' into (modifier_set, key).

    Returns a set of modifier names and the main key, both lowered.
    """
    parts = [p.strip().lower() for p in hotkey_str.split("+")]
    if not parts:
        return set(), None
    key = parts[-1]
    modifiers = set(parts[:-1])
    return modifiers, key


class HotkeyManager(QObject):
    """Global hotkey listener using pynput, emitting Qt signals."""

    triggered = pyqtSignal()

    def __init__(self, hotkey_str: str = "ctrl+shift+t", parent: QObject | None = None):
        super().__init__(parent)
        self._hotkey_str = hotkey_str
        self._modifiers, self._key = _parse_hotkey(hotkey_str)
        self._pressed_modifiers: set[str] = set()
        self._listener: object | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._listener is not None:
            return
        try:
            from pynput import keyboard

            def on_press(key):
                try:
                    name = key.char.lower() if hasattr(key, "char") and key.char else ""
                except AttributeError:
                    name = ""

                if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                    self._pressed_modifiers.add("ctrl")
                elif key == keyboard.Key.shift or key == keyboard.Key.shift_r:
                    self._pressed_modifiers.add("shift")
                elif key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                    self._pressed_modifiers.add("alt")

                if name == self._key and self._modifiers.issubset(self._pressed_modifiers):
                    self.triggered.emit()

            def on_release(key):
                if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                    self._pressed_modifiers.discard("ctrl")
                elif key == keyboard.Key.shift or key == keyboard.Key.shift_r:
                    self._pressed_modifiers.discard("shift")
                elif key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                    self._pressed_modifiers.discard("alt")

            self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self._listener.daemon = True
            self._listener.start()
            logger.info("Global hotkey registered: %s", self._hotkey_str)
        except ImportError:
            logger.warning("pynput not installed — global hotkeys disabled")
        except Exception:
            logger.exception("Failed to register global hotkey")

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def update_hotkey(self, hotkey_str: str) -> None:
        self.stop()
        self._hotkey_str = hotkey_str
        self._modifiers, self._key = _parse_hotkey(hotkey_str)
        self._pressed_modifiers.clear()
        self.start()
