"""Unit tests for WHIP SDP helpers and HTTP client (no Qt, no FFmpeg)."""

from __future__ import annotations

import io
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from core.whip_core import (
    fix_ice_candidates,
    is_ip_address,
    resolve_sdp_hostnames,
    whip_delete_resource,
    whip_post_offer,
)


def test_is_ip_address() -> None:
    assert is_ip_address("192.168.1.1") is True
    assert is_ip_address("example.com") is False


def test_fix_ice_candidates_keeps_all_private_when_no_public() -> None:
    sdp = (
        "v=0\r\n"
        "a=candidate:1 1 UDP 2130706431 192.168.0.5 12345 typ host\r\n"
        "a=candidate:2 1 UDP 2130706431 10.0.0.1 54321 typ host\r\n"
    )
    out = fix_ice_candidates(sdp)
    assert "192.168.0.5" in out
    assert "10.0.0.1" in out


def test_fix_ice_candidates_drops_private_when_public_present() -> None:
    sdp = (
        "v=0\r\n"
        "a=candidate:1 1 UDP 2130706431 192.168.0.5 12345 typ host\r\n"
        "a=candidate:2 1 UDP 2130706431 8.8.8.8 9 typ srflx raddr 0.0.0.0 rport 0\r\n"
    )
    out = fix_ice_candidates(sdp)
    assert "192.168.0.5" not in out
    assert "8.8.8.8" in out


def test_resolve_sdp_hostnames_replaces_hostname_with_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, *_args, **_kwargs):
        if host == "turn.example.invalid":
            return [(None, None, None, None, ("198.51.100.10", 0))]
        raise OSError("no")

    monkeypatch.setattr("core.whip_core.socket.getaddrinfo", fake_getaddrinfo)
    sdp = "a=candidate:1 1 UDP 2130706431 turn.example.invalid 3478 typ host\r\n"
    out = resolve_sdp_hostnames(sdp)
    assert "198.51.100.10" in out
    assert "turn.example.invalid" not in out


def test_whip_post_offer_http_error_includes_code() -> None:
    err = urllib.error.HTTPError(
        "https://x/whip",
        401,
        "Unauthorized",
        hdrs={},
        fp=io.BytesIO(b"invalid token"),
    )

    with patch("core.whip_core.urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="WHIP HTTP 401"):
            whip_post_offer("https://x/whip", "v=0\r\n", "")


def test_whip_delete_resource_success() -> None:
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 204

    with patch("core.whip_core.urllib.request.urlopen", return_value=mock_resp):
        assert whip_delete_resource("https://x/whip/r1", "tok") is True


def test_whip_delete_resource_failure_returns_false() -> None:
    with patch(
        "core.whip_core.urllib.request.urlopen",
        side_effect=urllib.error.URLError("network"),
    ):
        assert whip_delete_resource("https://x/whip/r1", "") is False


def test_whip_post_offer_success() -> None:
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = b"v=0\r\no=-\r\n"
    mock_resp.headers = {"Location": "https://x/whip/abc"}

    with patch("core.whip_core.urllib.request.urlopen", return_value=mock_resp):
        sdp, loc = whip_post_offer("https://x/whip", "offer", "tok")
    assert "v=0" in sdp
    assert loc == "https://x/whip/abc"
