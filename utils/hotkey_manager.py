from __future__ import annotations

import logging
import re

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

# User-facing modifier names -> pynput HotKey.parse tokens
_MODIFIER_TO_SPEC: dict[str, str] = {
    "ctrl": "<ctrl>",
    "control": "<ctrl>",
    "shift": "<shift>",
    "alt": "<alt>",
    "option": "<alt>",
    "win": "<cmd>",
    "windows": "<cmd>",
    "super": "<cmd>",
    "cmd": "<cmd>",
    "meta": "<cmd>",
    "command": "<cmd>",
}


def _format_main_key(key: str) -> str:
    """Last segment of a combo: single char, F-key, or <name> for Key.*."""
    k = key.strip().lower()
    if not k:
        raise ValueError("empty key")
    if len(k) == 1:
        return k
    if re.fullmatch(r"f\d+", k):
        return f"<{k}>"
    return f"<{k}>"


def user_hotkey_to_pynput_spec(hotkey_str: str) -> str:
    """Turn 'Ctrl+Shift+T' into '<ctrl>+<shift>+t' for pynput.keyboard.HotKey.parse."""
    parts = [p.strip() for p in hotkey_str.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mod_parts, main = parts
    spec_mods: list[str] = []
    for raw in mod_parts:
        m = raw.lower()
        if m not in _MODIFIER_TO_SPEC:
            raise ValueError(f"unknown modifier: {raw!r}")
        spec_mods.append(_MODIFIER_TO_SPEC[m])
    key_token = _format_main_key(main)
    return "+".join([*spec_mods, key_token]) if spec_mods else key_token


class HotkeyManager(QObject):
    """Global hotkey listener using pynput, emitting Qt signals."""

    triggered = pyqtSignal()

    def __init__(self, hotkey_str: str = "Ctrl+Shift+T", parent: QObject | None = None):
        super().__init__(parent)
        self._hotkey_str = hotkey_str
        self._listener: object | None = None

    def _emit_trigger(self) -> None:
        self.triggered.emit()

    def start(self) -> None:
        if self._listener is not None:
            return
        try:
            from pynput import keyboard
            from pynput.keyboard import HotKey

            try:
                spec = user_hotkey_to_pynput_spec(self._hotkey_str)
                HotKey.parse(spec)
            except ValueError:
                logger.warning(
                    "Invalid hotkey %r — using Ctrl+Shift+T", self._hotkey_str
                )
                spec = user_hotkey_to_pynput_spec("Ctrl+Shift+T")

            self._listener = keyboard.GlobalHotKeys(
                {spec: self._emit_trigger},
                daemon=True,
            )
            self._listener.start()
            logger.info("Global hotkey registered: %s (%s)", self._hotkey_str, spec)
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
        self.start()
