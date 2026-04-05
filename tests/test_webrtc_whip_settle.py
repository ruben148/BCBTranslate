"""WHIP reconnect delay policy (used by FFmpeg backend)."""

from __future__ import annotations

from core.webrtc_streamer import (
    _WHIP_RECONNECT_BASE_SEC,
    _whip_reconnect_delay_sec,
)


def test_whip_reconnect_delay_base_when_no_failures() -> None:
    assert _whip_reconnect_delay_sec(0) == _WHIP_RECONNECT_BASE_SEC


def test_whip_reconnect_delay_grows_then_caps() -> None:
    assert _whip_reconnect_delay_sec(1) == _WHIP_RECONNECT_BASE_SEC + 0.5
    assert _whip_reconnect_delay_sec(2) == _WHIP_RECONNECT_BASE_SEC + 1.0
    assert _whip_reconnect_delay_sec(3) == _WHIP_RECONNECT_BASE_SEC + 2.0
    assert _whip_reconnect_delay_sec(4) == _WHIP_RECONNECT_BASE_SEC + 4.0
    assert _whip_reconnect_delay_sec(5) == _WHIP_RECONNECT_BASE_SEC + 8.0
    assert _whip_reconnect_delay_sec(99) == _whip_reconnect_delay_sec(5)
