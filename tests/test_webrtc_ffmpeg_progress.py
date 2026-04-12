"""FFmpeg stderr parsing helpers for WHIP streaming."""

from __future__ import annotations

from core.webrtc_streamer import _ffmpeg_mux_progress_line


def test_mux_progress_accepts_audio_stats_line() -> None:
    line = (
        "size=     128kB time=00:00:02.04 bitrate= 205.1kbits/s speed=1.01x"
    )
    assert _ffmpeg_mux_progress_line(line) is True


def test_mux_progress_accepts_frame_style_line() -> None:
    line = "frame=  100 fps= 25 q=-1.0 size=     256kB time=00:00:04.00 speed=1.00x"
    assert _ffmpeg_mux_progress_line(line) is True


def test_mux_progress_rejects_size_without_speed() -> None:
    assert _ffmpeg_mux_progress_line("size=       0kB time=00:00:00.00") is False


def test_mux_progress_rejects_bare_size_substring() -> None:
    assert _ffmpeg_mux_progress_line("buffer size=1024") is False
