from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


class TranscriptWriter:
    """Write timestamped translation transcripts to text files."""

    def __init__(
        self,
        directory: str,
        source_language: str = "ro-RO",
        target_language: str = "en",
        voice: str = "",
        rate: float = 1.0,
        pitch: str = "+0%",
    ):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now()
        filename = f"transcript_{now.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
        self._path = self._dir / filename
        self._start_time = now
        self._count = 0

        header = (
            f"=== BCBTranslate Session — {now.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            f"Source: {source_language} → {target_language}\n"
            f"Voice: {voice} | Rate: {rate:.1f}× | Pitch: {pitch}\n"
            f"{'=' * 50}\n\n"
        )
        self._path.write_text(header, encoding="utf-8")

    def write(
        self, source_text: str, translated_text: str, lag_ms: int = 0
    ) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        lag_str = f" (lag: {lag_ms / 1000:.1f}s)" if lag_ms > 0 else ""
        entry = (
            f"[{ts}] RO: {source_text}\n"
            f"[{ts}] EN: {translated_text}{lag_str}\n\n"
        )
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(entry)
        self._count += 1

    def close(self, avg_lag_ms: int = 0) -> None:
        now = datetime.now()
        duration = now - self._start_time
        hours, remainder = divmod(int(duration.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        footer = (
            f"\n=== Session ended — {now.strftime('%H:%M:%S')} | "
            f"Duration: {hours:02d}:{minutes:02d}:{seconds:02d} | "
            f"Utterances: {self._count} | "
            f"Avg lag: {avg_lag_ms / 1000:.1f}s ===\n"
        )
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(footer)

    @property
    def path(self) -> Path:
        return self._path
