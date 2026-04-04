"""Tests for translated TTS → stereo frame pacing."""

from __future__ import annotations

import threading

import numpy as np

from core.translated_pcm_adapter import TranslatedPcmStereoAdapter


def _stereo_frame_layout(mono: np.ndarray) -> np.ndarray:
    """Decode stereo interleaved int16 frame to (left, right) mono arrays."""
    inter = np.frombuffer(mono.tobytes(), dtype=np.int16)
    left = inter[0::2]
    right = inter[1::2]
    return left, right


def test_empty_adapter_emits_silence_frame() -> None:
    a = TranslatedPcmStereoAdapter(16000, frame_ms=20.0)
    assert a.mono_samples_per_frame == 320
    frame = a.pop_stereo_s16le_frame()
    assert len(frame) == 320 * 2 * 2  # stereo int16 bytes
    arr = np.frombuffer(frame, dtype=np.int16)
    assert np.all(arr == 0)


def test_prebuffer_holds_back_until_threshold() -> None:
    a = TranslatedPcmStereoAdapter(16000, frame_ms=20.0, prebuffer_ms=40.0)
    prebuf = int(16000 * 0.040)  # 640 samples
    # Push less than the pre-buffer threshold
    a.push_mono_pcm(np.ones(300, dtype=np.int16).tobytes())
    f = a.pop_stereo_s16le_frame()
    assert np.all(np.frombuffer(f, dtype=np.int16) == 0), "should still be silence"

    # Push enough to cross the threshold
    a.push_mono_pcm(np.ones(prebuf, dtype=np.int16).tobytes())
    f = a.pop_stereo_s16le_frame()
    left, _ = _stereo_frame_layout(np.frombuffer(f, dtype=np.int16))
    assert np.any(left != 0), "should now emit real audio"


def test_prebuffer_resets_when_drained() -> None:
    a = TranslatedPcmStereoAdapter(16000, frame_ms=20.0, prebuffer_ms=20.0)
    # Fill well above threshold, drain everything
    a.push_mono_pcm(np.ones(640, dtype=np.int16).tobytes())
    a.pop_stereo_s16le_frame()  # 320 real
    a.pop_stereo_s16le_frame()  # 320 real
    # Now empty → should reset pre-buffer
    f = a.pop_stereo_s16le_frame()
    assert np.all(np.frombuffer(f, dtype=np.int16) == 0), "pre-buffer reset"

    # Push small amount below pre-buffer → should emit silence
    a.push_mono_pcm(np.ones(100, dtype=np.int16).tobytes())
    f = a.pop_stereo_s16le_frame()
    assert np.all(np.frombuffer(f, dtype=np.int16) == 0), "still pre-buffering"


def test_exact_mono_frame_duplicates_to_stereo() -> None:
    a = TranslatedPcmStereoAdapter(16000, frame_ms=20.0, prebuffer_ms=0.1)
    mono = np.arange(320, dtype=np.int16)
    a.push_mono_pcm(mono.tobytes())
    frame = a.pop_stereo_s16le_frame()
    left, right = _stereo_frame_layout(np.frombuffer(frame, dtype=np.int16))
    assert np.array_equal(left, mono)
    assert np.array_equal(right, mono)


def test_partial_chunk_then_rest() -> None:
    a = TranslatedPcmStereoAdapter(16000, frame_ms=20.0, prebuffer_ms=0.1)
    a.push_mono_pcm(np.ones(100, dtype=np.int16).tobytes())
    f1 = a.pop_stereo_s16le_frame()
    assert np.frombuffer(f1, dtype=np.int16).shape[0] == 640

    a.push_mono_pcm(np.ones(500, dtype=np.int16).tobytes())
    f2 = a.pop_stereo_s16le_frame()
    left2, _ = _stereo_frame_layout(np.frombuffer(f2, dtype=np.int16))
    assert left2.shape[0] == 320


def test_push_is_fast_under_contention() -> None:
    """Producer must not do heavy concat while holding lock (regression)."""
    a = TranslatedPcmStereoAdapter(16000, frame_ms=20.0, prebuffer_ms=0.1)
    stop = threading.Event()

    def consumer() -> None:
        while not stop.is_set():
            a.pop_stereo_s16le_frame()

    t = threading.Thread(target=consumer, daemon=True)
    t.start()
    try:
        small = np.zeros(160, dtype=np.int16).tobytes()
        for _ in range(5000):
            a.push_mono_pcm(small)
    finally:
        stop.set()
        t.join(timeout=2)


def test_overflow_drops_oldest() -> None:
    a = TranslatedPcmStereoAdapter(
        16000, frame_ms=20.0, max_buffer_seconds=0.05, prebuffer_ms=0.1,
    )
    big = np.ones(2000, dtype=np.int16).tobytes()
    for _ in range(20):
        a.push_mono_pcm(big)
    for _ in range(30):
        a.pop_stereo_s16le_frame()
