"""Helpers for PCM streams that may include WAV (RIFF) wrappers."""

from __future__ import annotations

import io
import logging
import wave

logger = logging.getLogger(__name__)


def extract_pcm_from_synthesis_chunk(
    buffer: bytearray,
    chunk: bytes,
    *,
    max_buffer_bytes: int = 512_000,
) -> bytes:
    """Append ``chunk`` to ``buffer`` and return PCM ready for int16 playback.

    Azure ``TranslationSynthesisResult.audio`` is usually raw 16 kHz mono PCM,
    but some endpoints still wrap chunks in WAV containers. Playing RIFF headers
    or partial headers as PCM causes loud pops and crackling.

    Returns ``b""`` when more bytes are needed to decide (incomplete WAV header).
    """
    if not chunk:
        return b""

    buffer.extend(chunk)

    if len(buffer) < 4:
        if b"RIFF".startswith(bytes(buffer)):
            return b""
        raw = bytes(buffer)
        buffer.clear()
        return raw

    if buffer[:4] != b"RIFF":
        raw = bytes(buffer)
        buffer.clear()
        return raw

    pcm = _try_read_wav_pcm(bytes(buffer))
    if pcm is not None:
        buffer.clear()
        return pcm

    if len(buffer) > max_buffer_bytes:
        logger.warning(
            "WAV parse buffer exceeded %d bytes — flushing as raw PCM",
            max_buffer_bytes,
        )
        raw = bytes(buffer)
        buffer.clear()
        return raw

    return b""


def flush_synthesis_buffer(buffer: bytearray) -> bytes:
    """Flush remaining bytes (e.g. on synthesis-complete with empty chunk)."""
    if not buffer:
        return b""
    raw = bytes(buffer)
    buffer.clear()
    return raw


def _try_read_wav_pcm(data: bytes) -> bytes | None:
    """Return PCM samples if ``data`` is a complete WAV file, else None."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            return wf.readframes(wf.getnframes())
    except Exception:
        return None
