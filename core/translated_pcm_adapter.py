"""Buffer irregular TTS mono PCM and build fixed-duration stereo S16LE frames.

Azure invokes ``write_output`` on the SDK thread; this adapter keeps ``push``
O(1) and moves merging work to the consumer thread so synthesis never blocks
on numpy concatenation while holding a lock shared with the streamer.
"""

from __future__ import annotations

import threading

import numpy as np


class TranslatedPcmStereoAdapter:
    """Mono int16 TTS → stereo int16 frames (L=R) for FFmpeg raw input."""

    __slots__ = (
        "_lock",
        "_chunks",
        "_pending_mono",
        "_mono_sample_rate",
        "_mono_per_frame",
        "_stereo_samples",
        "_max_pending_mono",
    )

    def __init__(
        self,
        mono_sample_rate: int,
        frame_ms: float = 20.0,
        max_buffer_seconds: float = 2.0,
    ) -> None:
        if mono_sample_rate < 8000:
            raise ValueError("mono_sample_rate must be at least 8000")
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._pending_mono = np.array([], dtype=np.int16)
        self._mono_sample_rate = mono_sample_rate
        self._mono_per_frame = max(1, int(round(mono_sample_rate * frame_ms / 1000)))
        self._stereo_samples = self._mono_per_frame * 2
        self._max_pending_mono = int(mono_sample_rate * max_buffer_seconds)

    @property
    def mono_samples_per_frame(self) -> int:
        return self._mono_per_frame

    @property
    def stereo_samples_per_frame(self) -> int:
        return self._stereo_samples

    def push_mono_pcm(self, pcm_bytes: bytes) -> None:
        """Fast path for the TTS thread — only append a small array under lock."""
        if not pcm_bytes:
            return
        chunk = np.frombuffer(pcm_bytes, dtype=np.int16).copy()
        if chunk.size == 0:
            return
        with self._lock:
            self._chunks.append(chunk)

    def _drain_chunks_to_pending_locked(self) -> None:
        if not self._chunks:
            return
        parts = [self._pending_mono] + self._chunks
        self._chunks.clear()
        self._pending_mono = (
            np.concatenate(parts) if len(parts) > 1 else parts[0]
        )
        if self._pending_mono.size > self._max_pending_mono:
            # Drop oldest samples to cap latency / memory (live interpretation).
            self._pending_mono = self._pending_mono[-self._max_pending_mono :]

    def pop_stereo_s16le_frame(self) -> bytes:
        """Return one L=R interleaved stereo frame; pad with silence if needed."""
        with self._lock:
            self._drain_chunks_to_pending_locked()
            need = self._mono_per_frame
            if self._pending_mono.size >= need:
                mono = self._pending_mono[:need]
                self._pending_mono = self._pending_mono[need:]
            else:
                mono = self._pending_mono
                self._pending_mono = np.array([], dtype=np.int16)

        if mono.size < need:
            mono = np.pad(mono, (0, need - mono.size))

        stereo = np.repeat(mono, 2)
        return stereo.tobytes()
