"""WHIP signaling helpers (SDP fixes, HTTP offer/delete, localhost proxy).

Kept free of Qt so it can be unit-tested without the GUI event loop.
"""

from __future__ import annotations

import http.server
import ipaddress
import logging
import socket
import threading
from socketserver import ThreadingMixIn
import urllib.error
import urllib.request
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


class _ThreadingWhipHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    """WHIP signaling may use concurrent HTTP (e.g. trickle ICE PATCH + reads)."""

    daemon_threads = True
    allow_reuse_address = True


def is_ip_address(addr: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, addr)
            return True
        except (socket.error, OSError):
            pass
    return False


def resolve_sdp_hostnames(sdp: str) -> str:
    """Resolve hostname ICE candidates so aioice accepts them."""
    sep = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(sep)
    result: list[str] = []
    for line in lines:
        if line.startswith("a=candidate:"):
            parts = line.split()
            if len(parts) > 4 and not is_ip_address(parts[4]):
                hostname = parts[4]
                try:
                    infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
                    if infos:
                        ip = infos[0][4][0]
                        logger.info("Resolved ICE candidate %s → %s", hostname, ip)
                        parts[4] = ip
                        line = " ".join(parts)
                except socket.gaierror:
                    logger.warning("Cannot resolve ICE candidate: %s", hostname)
        result.append(line)
    return sep.join(result)


def _is_private_ip(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_private
    except ValueError:
        return False


def fix_ice_candidates(sdp: str) -> str:
    """Resolve hostnames and drop private-IP candidates when public ones exist.

    FFmpeg's WHIP muxer often picks the first ICE candidate. Servers may list a
    private VPC candidate before a reachable public one — strip privates when
    at least one public candidate exists.
    """
    sep = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(sep)

    resolved: list[tuple[str, bool]] = []
    has_public = False

    for line in lines:
        if not line.startswith("a=candidate:"):
            resolved.append((line, False))
            continue

        parts = line.split()
        if len(parts) <= 4:
            resolved.append((line, False))
            continue

        addr = parts[4]

        if not is_ip_address(addr):
            try:
                infos = socket.getaddrinfo(addr, None, socket.AF_INET)
                if infos:
                    ip = infos[0][4][0]
                    logger.info("WHIP proxy: resolved %s → %s", addr, ip)
                    parts[4] = ip
                    line = " ".join(parts)
                    addr = ip
            except socket.gaierror:
                logger.warning("WHIP proxy: cannot resolve %s", addr)

        is_priv = is_ip_address(addr) and _is_private_ip(addr)
        if not is_priv:
            has_public = True
        resolved.append((line, is_priv))

    if not has_public:
        return sep.join(line for line, _ in resolved)

    result: list[str] = []
    for line, is_priv in resolved:
        if is_priv:
            logger.info("WHIP proxy: dropping private-IP candidate")
            continue
        result.append(line)
    return sep.join(result)


def whip_post_offer(url: str, sdp: str, token: str) -> tuple[str, str | None]:
    """POST SDP offer; return (answer_sdp, location_or_none)."""
    headers = {"Content-Type": "application/sdp"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=sdp.encode("utf-8"), headers=headers, method="POST",
    )
    req.add_header("Connection", "close")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            answer = resp.read().decode("utf-8")
            location = resp.headers.get("Location")
            return answer, location
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"WHIP HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach WHIP server: {exc.reason}") from exc


def whip_delete_resource(url: str, token: str) -> None:
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Connection", "close")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        logger.debug("WHIP DELETE failed for %s", url, exc_info=True)


class WHIPProxy:
    """Localhost HTTP server that proxies WHIP signaling to the real endpoint."""

    def __init__(self, target_url: str, bearer_token: str) -> None:
        self._target_url = target_url
        self._bearer_token = bearer_token
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._resource_url: str = ""

    def start(self) -> int:
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""

                headers = {"Content-Type": "application/sdp"}
                if proxy._bearer_token:
                    headers["Authorization"] = f"Bearer {proxy._bearer_token}"

                try:
                    req = urllib.request.Request(
                        proxy._target_url, data=body,
                        headers=headers, method="POST",
                    )
                    req.add_header("Connection", "close")
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        answer_sdp = resp.read().decode("utf-8")
                        location = resp.headers.get("Location")
                        status = resp.status
                except urllib.error.HTTPError as exc:
                    self.send_response(exc.code)
                    self.end_headers()
                    try:
                        self.wfile.write(exc.read())
                    except Exception:
                        pass
                    return
                except Exception as exc:
                    logger.error("WHIP proxy POST failed: %s", exc)
                    self.send_response(502)
                    self.end_headers()
                    self.wfile.write(str(exc).encode())
                    return

                fixed_sdp = fix_ice_candidates(answer_sdp)

                if location:
                    if location.startswith("http"):
                        proxy._resource_url = location
                    else:
                        proxy._resource_url = urljoin(
                            proxy._target_url, location,
                        )

                body_bytes = fixed_sdp.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/sdp")
                self.send_header("Content-Length", str(len(body_bytes)))
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(body_bytes)

            def do_DELETE(self) -> None:
                url = proxy._resource_url
                if not url:
                    self.send_response(200)
                    self.end_headers()
                    return
                try:
                    req = urllib.request.Request(url, method="DELETE")
                    req.add_header("Connection", "close")
                    if proxy._bearer_token:
                        req.add_header(
                            "Authorization",
                            f"Bearer {proxy._bearer_token}",
                        )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        self.send_response(resp.status)
                        self.end_headers()
                except Exception:
                    self.send_response(200)
                    self.end_headers()

            def do_PATCH(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                url = proxy._resource_url
                if not url:
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    ct = self.headers.get(
                        "Content-Type", "application/trickle-ice-sdpfrag",
                    )
                    req = urllib.request.Request(
                        url, data=body,
                        headers={"Content-Type": ct}, method="PATCH",
                    )
                    req.add_header("Connection", "close")
                    if proxy._bearer_token:
                        req.add_header(
                            "Authorization",
                            f"Bearer {proxy._bearer_token}",
                        )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = resp.read()
                        self.send_response(resp.status)
                        self.end_headers()
                        self.wfile.write(data)
                except Exception:
                    self.send_response(502)
                    self.end_headers()

        self._server = _ThreadingWhipHTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="whip-proxy",
        )
        self._thread.start()
        logger.info("WHIP proxy started on 127.0.0.1:%d → %s", port, self._target_url)
        return port

    def delete_resource(self) -> None:
        url = self._resource_url
        if not url:
            return
        whip_delete_resource(url, self._bearer_token)
        self._resource_url = ""

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
