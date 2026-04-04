from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, fields
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from core.models import AppConfig

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("APPDATA", ".")) / "BCBTranslate"
CONFIG_FILE = CONFIG_DIR / "bcbtranslate_config.json"
CONFIG_VERSION = 1


class ConfigManager(QObject):
    """Load, validate, and persist application configuration."""

    changed = pyqtSignal(str, object)  # (field_name, new_value)

    def __init__(self, path: Path | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._path = path or CONFIG_FILE
        self._config = AppConfig()
        self.load()

    # -- public api --------------------------------------------------------

    @property
    def config(self) -> AppConfig:
        return self._config

    def get(self, key: str, default=None):
        return getattr(self._config, key, default)

    def set(self, key: str, value) -> None:
        if not hasattr(self._config, key):
            logger.warning("Unknown config key: %s", key)
            return
        old = getattr(self._config, key)
        if old == value:
            return
        setattr(self._config, key, value)
        self.changed.emit(key, value)

    def update(self, **kwargs) -> None:
        for k, v in kwargs.items():
            self.set(k, v)

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self._path.exists():
            logger.info("No config file found — using defaults")
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read config — using defaults")
            return

        stored_version = raw.pop("_version", 0)
        if stored_version < CONFIG_VERSION:
            raw = self._migrate(raw, stored_version)

        valid_keys = {f.name for f in fields(AppConfig)}
        for key, value in raw.items():
            if key in valid_keys:
                setattr(self._config, key, value)

        logger.info("Config loaded from %s", self._path)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self._config)
        data["_version"] = CONFIG_VERSION

        # Atomic write: write to temp file then rename
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, str(self._path))
            logger.info("Config saved to %s", self._path)
        except OSError:
            logger.exception("Failed to save config")

    def reset_to_defaults(self) -> None:
        self._config = AppConfig()
        self.save()

    # -- azure keys --------------------------------------------------------

    def azure_speech_key(self) -> str | None:
        return os.environ.get(self._config.speech_key_env_var)

    def azure_speech_region(self) -> str | None:
        return os.environ.get(self._config.speech_region_env_var)

    def has_azure_credentials(self) -> bool:
        return bool(self.azure_speech_key() and self.azure_speech_region())

    # -- migration ---------------------------------------------------------

    @staticmethod
    def _migrate(data: dict, from_version: int) -> dict:
        logger.info("Migrating config from v%d to v%d", from_version, CONFIG_VERSION)
        return data
