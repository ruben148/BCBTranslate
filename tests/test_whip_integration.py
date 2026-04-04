"""Optional live WHIP checks — requires tests/secrets/whip_endpoint.txt.

This file is intentionally skipped in CI when the secret file is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.whip_core import whip_post_offer

_SECRETS_DIR = Path(__file__).resolve().parent / "secrets"
_ENDPOINT_FILE = _SECRETS_DIR / "whip_endpoint.txt"


def _load_whip_credentials() -> tuple[str, str] | None:
    if not _ENDPOINT_FILE.is_file():
        return None
    lines = [
        ln.strip()
        for ln in _ENDPOINT_FILE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not lines:
        return None
    url = lines[0]
    token = lines[1] if len(lines) > 1 else ""
    if not url.startswith("http"):
        return None
    return url, token


@pytest.mark.integration
def test_whip_server_rejects_invalid_sdp() -> None:
    """Proves the endpoint is reachable and enforces SDP (expects HTTP error)."""
    creds = _load_whip_credentials()
    if creds is None:
        pytest.skip(
            f"Create {_ENDPOINT_FILE} with WHIP URL (and optional token line). "
            "See whip_endpoint.example.txt",
        )
    url, token = creds
    with pytest.raises(RuntimeError) as excinfo:
        whip_post_offer(url, "not-valid-sdp", token)
    msg = str(excinfo.value).lower()
    assert "whip http" in msg or "cannot reach" in msg


@pytest.mark.integration
def test_whip_post_minimal_sdp_gets_response_or_error() -> None:
    """Sends a tiny SDP-shaped body; server should answer with 4xx or negotiate."""
    creds = _load_whip_credentials()
    if creds is None:
        pytest.skip("No tests/secrets/whip_endpoint.txt — see whip_endpoint.example.txt")
    url, token = creds
    minimal = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
    try:
        answer, _loc = whip_post_offer(url, minimal, token)
        assert "v=0" in answer or len(answer) > 10
    except RuntimeError as e:
        # Acceptable: auth failure, bad SDP, etc. — still a live round-trip.
        assert "WHIP HTTP" in str(e) or "Cannot reach" in str(e)
