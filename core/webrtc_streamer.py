"""WebRTC WHIP audio streamer.

Streams audio (original mic input or translated TTS output) to a WebRTC
server using the WHIP (WebRTC-HTTP Ingestion Protocol) protocol.

Two backends are supported, selected automatically:

1. **FFmpeg** (preferred) — delegates encoding, ICE, DTLS, and SRTP to
   native C code via an ``ffmpeg`` subprocess.  Achieves latency comparable
   to dedicated tools like BUTT (~200-300 ms).
2. **aiortc** (fallback) — pure-Python WebRTC stack.  Higher latency
   (~600-1000 ms) but requires no external binaries.
"""

from __future__ import annotations

import asyncio
import collections
import fractions
import logging
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urljoin

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

_CREATIONFLAGS = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

try:
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    import av  # noqa: F401 — needed at runtime for AudioFrame

    HAS_AIORTC = True
except ImportError:
    HAS_AIORTC = False
    MediaStreamTrack = object  # fallback base for class definition


# ---------------------------------------------------------------------------
# FFmpeg detection
# ---------------------------------------------------------------------------

def _find_ffmpeg() -> str | None:
    """Return the path to an ``ffmpeg`` binary, or *None*."""
    # Bundled next to the executable (PyInstaller builds)
    if getattr(sys, "frozen", False):
        bundled = __import__("pathlib").Path(sys.executable).parent / "ffmpeg.exe"
        if bundled.exists():
            return str(bundled)
    # Next to the project root
    from pathlib import Path

    local = Path(__file__).resolve().parent.parent / "ffmpeg.exe"
    if local.exists():
        return str(local)
    # On PATH
    return shutil.which("ffmpeg")


def _ffmpeg_supports_whip(ffmpeg: str) -> bool:
    """Return *True* if the FFmpeg build contains the WHIP muxer."""
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-muxers"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_CREATIONFLAGS,
        )
        for line in r.stdout.splitlines():
            if "whip" in line.lower():
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# aiortc helpers (SDP hostname resolution, STUN)
# ---------------------------------------------------------------------------

_STUN_SERVERS = [
    "stun:stun.l.google.com:19302",
    "stun:stun1.l.google.com:19302",
]


def _is_ip_address(addr: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, addr)
            return True
        except (socket.error, OSError):
            pass
    return False


def _resolve_sdp_hostnames(sdp: str) -> str:
    """Resolve hostname ICE candidates so aioice accepts them."""
    sep = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(sep)
    result: list[str] = []
    for line in lines:
        if line.startswith("a=candidate:"):
            parts = line.split()
            if len(parts) > 4 and not _is_ip_address(parts[4]):
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


# ---------------------------------------------------------------------------
# AudioBufferTrack  (aiortc fallback only)
# ---------------------------------------------------------------------------

class AudioBufferTrack(MediaStreamTrack):
    """Deque-buffered audio track for the aiortc backend."""

    kind = "audio"
    SAMPLE_RATE = 48000
    FRAME_SIZE = SAMPLE_RATE * 20 // 1000  # 960 (20 ms)
    MAX_BUFFER_MS = 500

    def __init__(self) -> None:
        super().__init__()
        self._chunks: collections.deque[np.ndarray] = collections.deque()
        self._partial = np.array([], dtype=np.int16)
        self._total_samples = 0
        self._lock = threading.Lock()
        self._pts = 0
        self._start_time: float | None = None
        self._time_base = fractions.Fraction(1, self.SAMPLE_RATE)
        self._max_samples = self.SAMPLE_RATE * self.MAX_BUFFER_MS // 1000

    def push_audio(self, pcm_int16: np.ndarray, source_rate: int = 48000) -> None:
        if source_rate != self.SAMPLE_RATE:
            pcm_int16 = self._resample(pcm_int16, source_rate, self.SAMPLE_RATE)
        with self._lock:
            self._chunks.append(pcm_int16)
            self._total_samples += len(pcm_int16)
            while self._total_samples > self._max_samples and self._chunks:
                dropped = self._chunks.popleft()
                self._total_samples -= len(dropped)

    async def recv(self):  # noqa: D401
        import av as _av

        if self._start_time is None:
            self._start_time = time.time()
        target = self._start_time + self._pts / self.SAMPLE_RATE
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        with self._lock:
            data = self._drain(self.FRAME_SIZE)
        frame = _av.AudioFrame.from_ndarray(
            data.reshape(1, -1), format="s16", layout="mono"
        )
        frame.sample_rate = self.SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = self._time_base
        self._pts += self.FRAME_SIZE
        return frame

    def _drain(self, needed: int) -> np.ndarray:
        parts: list[np.ndarray] = []
        remaining = needed
        if self._partial.size:
            take = min(remaining, len(self._partial))
            parts.append(self._partial[:take])
            self._partial = (
                self._partial[take:]
                if take < len(self._partial)
                else np.array([], dtype=np.int16)
            )
            remaining -= take
        while remaining > 0 and self._chunks:
            chunk = self._chunks.popleft()
            self._total_samples -= len(chunk)
            take = min(remaining, len(chunk))
            parts.append(chunk[:take])
            if take < len(chunk):
                self._partial = chunk[take:]
            remaining -= take
        if not parts:
            return np.zeros(needed, dtype=np.int16)
        result = np.concatenate(parts) if len(parts) > 1 else parts[0]
        if len(result) < needed:
            result = np.pad(result, (0, needed - len(result)))
        return result

    @staticmethod
    def _resample(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
        if from_rate == to_rate:
            return samples
        n_out = int(len(samples) * to_rate / from_rate)
        x_old = np.arange(len(samples))
        x_new = np.linspace(0, len(samples) - 1, n_out)
        return np.interp(x_new, x_old, samples.astype(np.float64)).astype(np.int16)


# ---------------------------------------------------------------------------
# WebRTCStreamer
# ---------------------------------------------------------------------------

class WebRTCStreamer(QObject):
    """Streams audio to a WebRTC endpoint via the WHIP protocol."""

    log_message = pyqtSignal(str, str)
    state_changed = pyqtSignal(str)

    def __init__(self, audio_router, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._audio_router = audio_router
        self._state = "idle"
        self._backend: str | None = None  # "ffmpeg" | "aiortc"

        # Shared
        self._capture_stream = None
        self._output_listener_cb: Callable[[bytes], None] | None = None

        # FFmpeg backend
        self._process: subprocess.Popen | None = None
        self._write_queue: queue.Queue[bytes | None] | None = None
        self._writer_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

        # aiortc backend
        self._loop: asyncio.AbstractEventLoop | None = None
        self._aio_thread: threading.Thread | None = None
        self._pc = None
        self._track: AudioBufferTrack | None = None
        self._whip_resource_url: str | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_streaming(self) -> bool:
        return self._state == "streaming"

    # ======================================================================
    # Public API
    # ======================================================================

    def start(
        self,
        whip_url: str,
        bearer_token: str,
        audio_source: str,
        input_device_id: int | None = None,
        sample_rate: int = 16000,
        gain: float = 1.0,
    ) -> None:
        if self._state not in ("idle", "error"):
            self._emit_log("Stream is already active", "warning")
            return
        if not whip_url.strip():
            self._emit_log("WHIP URL is required", "error")
            return

        self._cleanup_all()
        self._set_state("connecting")

        ffmpeg = _find_ffmpeg()
        if ffmpeg and _ffmpeg_supports_whip(ffmpeg):
            self._backend = "ffmpeg"
            self._emit_log("Starting stream (FFmpeg backend)…", "info")
            self._start_ffmpeg(
                ffmpeg, whip_url.strip(), bearer_token.strip(),
                audio_source, input_device_id, sample_rate, gain,
            )
        elif HAS_AIORTC:
            self._backend = "aiortc"
            self._emit_log("Starting stream (aiortc backend)…", "info")
            self._start_aiortc(
                whip_url.strip(), bearer_token.strip(),
                audio_source, input_device_id, sample_rate, gain,
            )
        else:
            self._set_state("error")
            self._emit_log(
                "No streaming backend found. Install FFmpeg (recommended) "
                "or the aiortc Python package.",
                "error",
            )

    def stop(self) -> None:
        if self._state == "idle":
            return
        self._set_state("stopping")
        self._emit_log("Stopping stream…", "info")
        self._stop_audio_capture()
        if self._backend == "ffmpeg":
            self._stop_ffmpeg()
        else:
            self._stop_aiortc()
        self._backend = None
        self._set_state("idle")
        self._emit_log("Stream stopped", "info")

    # ======================================================================
    # FFmpeg backend
    # ======================================================================

    def _start_ffmpeg(
        self,
        ffmpeg: str,
        url: str,
        token: str,
        audio_source: str,
        device_id: int | None,
        sample_rate: int,
        gain: float,
    ) -> None:
        capture_rate = 48000 if audio_source == "original" else sample_rate

        cmd: list[str] = [
            ffmpeg, "-hide_banner", "-loglevel", "info",
            "-f", "s16le",
            "-ar", str(capture_rate),
            "-ac", "1",
            "-i", "pipe:0",
            "-c:a", "libopus",
            "-b:a", "128k",
            "-application", "lowdelay",
            "-f", "whip",
        ]
        if token:
            cmd += ["-bearer_token", token]
        cmd.append(url)

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_CREATIONFLAGS,
            )
        except FileNotFoundError:
            self._set_state("error")
            self._emit_log("FFmpeg binary not found", "error")
            return
        except Exception as exc:
            self._set_state("error")
            self._emit_log(f"Failed to start FFmpeg: {exc}", "error")
            return

        self._emit_log(f"FFmpeg started (PID {self._process.pid})", "info")

        self._write_queue = queue.Queue(maxsize=250)
        self._writer_thread = threading.Thread(
            target=self._ffmpeg_writer, daemon=True, name="ffmpeg-writer",
        )
        self._writer_thread.start()

        self._stderr_thread = threading.Thread(
            target=self._ffmpeg_stderr_reader, daemon=True, name="ffmpeg-stderr",
        )
        self._stderr_thread.start()

        self._start_capture_for_pipe(audio_source, device_id, capture_rate, gain)

    def _stop_ffmpeg(self) -> None:
        if self._write_queue:
            self._write_queue.put(None)
        if self._process and self._process.stdin:
            try:
                self._process.stdin.close()
            except Exception:
                pass
        if self._process:
            try:
                self._process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self._process.kill()
                logger.debug("FFmpeg killed after timeout")
            self._process = None
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=2)
        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=2)
        self._write_queue = None
        self._writer_thread = None
        self._stderr_thread = None

    def _ffmpeg_writer(self) -> None:
        """Drain the write queue into FFmpeg's stdin (dedicated thread)."""
        while True:
            data = self._write_queue.get()
            if data is None:
                break
            if self._process is None or self._process.poll() is not None:
                break
            try:
                self._process.stdin.write(data)
            except (BrokenPipeError, OSError):
                break

    def _ffmpeg_stderr_reader(self) -> None:
        """Parse FFmpeg stderr and forward relevant lines to the UI log."""
        connected_reported = False
        try:
            for raw_line in self._process.stderr:
                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                lower = text.lower()

                if "error" in lower or "failed" in lower or "invalid" in lower:
                    self._emit_log(text, "error")
                elif not connected_reported and (
                    "speed=" in lower or "size=" in lower
                ):
                    connected_reported = True
                    self._set_state("streaming")
                    self._emit_log("Streaming audio", "success")
                elif any(k in lower for k in ("whip", "ice", "dtls")):
                    self._emit_log(text, "info")
                elif "stream mapping" in lower or "output #" in lower:
                    self._emit_log(text, "info")
        except Exception:
            pass
        if self._process:
            rc = self._process.poll()
            if rc and rc != 0 and self._state not in ("idle", "stopping"):
                self._set_state("error")
                self._emit_log(f"FFmpeg exited with code {rc}", "error")

    # -- pipe-based audio capture (FFmpeg) ---------------------------------

    def _start_capture_for_pipe(
        self,
        audio_source: str,
        device_id: int | None,
        capture_rate: int,
        gain: float,
    ) -> None:
        if audio_source == "original":
            self._start_pipe_original(device_id, capture_rate, gain)
        else:
            self._start_pipe_translated()

    def _start_pipe_original(
        self, device_id: int | None, capture_rate: int, gain: float
    ) -> None:
        import sounddevice as sd

        block = 960 if capture_rate == 48000 else 1024

        def _cb(indata, frames, time_info, status):
            if status:
                logger.debug("FFmpeg capture status: %s", status)
            if self._write_queue is None:
                return
            amplified = indata[:, 0] * gain * 32767
            pcm = np.clip(amplified, -32768, 32767).astype(np.int16)
            try:
                self._write_queue.put_nowait(pcm.tobytes())
            except queue.Full:
                pass

        try:
            self._capture_stream = sd.InputStream(
                device=device_id,
                channels=1,
                samplerate=capture_rate,
                dtype="float32",
                blocksize=block,
                callback=_cb,
            )
            self._capture_stream.start()
            self._emit_log(
                f"Capturing mic input ({capture_rate} Hz, {block}-sample blocks)",
                "success",
            )
        except Exception as exc:
            self._emit_log(f"Failed to start audio capture: {exc}", "error")

    def _start_pipe_translated(self) -> None:
        def _on_output(pcm_data: bytes) -> None:
            if self._write_queue is None:
                return
            try:
                self._write_queue.put_nowait(pcm_data)
            except queue.Full:
                pass

        self._output_listener_cb = _on_output
        self._audio_router.add_output_listener(_on_output)
        self._emit_log("Listening for translated TTS audio output", "success")

    # ======================================================================
    # aiortc backend
    # ======================================================================

    def _start_aiortc(
        self,
        url: str,
        token: str,
        audio_source: str,
        device_id: int | None,
        sample_rate: int,
        gain: float,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._aio_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="webrtc-loop",
        )
        self._aio_thread.start()
        asyncio.run_coroutine_threadsafe(
            self._aio_connect(url, token, audio_source, device_id, sample_rate, gain),
            self._loop,
        )

    def _stop_aiortc(self) -> None:
        if self._loop and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._aio_disconnect(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception:
                logger.debug("aiortc disconnect timed out", exc_info=True)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._aio_thread and self._aio_thread.is_alive():
            self._aio_thread.join(timeout=3)
        self._loop = None
        self._aio_thread = None
        self._pc = None
        self._track = None
        self._whip_resource_url = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _aio_connect(
        self, url, token, audio_source, device_id, sample_rate, gain,
    ) -> None:
        try:
            self._track = AudioBufferTrack()
            self._emit_log("Audio track created (48 kHz mono Opus)", "info")
            config = RTCConfiguration(iceServers=[RTCIceServer(urls=_STUN_SERVERS)])
            self._pc = RTCPeerConnection(configuration=config)

            @self._pc.on("connectionstatechange")
            async def _on_conn():
                st = self._pc.connectionState
                self._emit_log(f"Connection state: {st}", "info")
                if st == "connected":
                    self._set_state("streaming")
                    self._emit_log("Streaming audio", "success")
                elif st == "failed":
                    self._set_state("error")
                    self._emit_log("Connection failed", "error")
                elif st == "closed" and self._state != "stopping":
                    self._set_state("idle")

            @self._pc.on("iceconnectionstatechange")
            async def _on_ice():
                self._emit_log(
                    f"ICE state: {self._pc.iceConnectionState}", "info"
                )

            self._pc.addTrack(self._track)
            self._emit_log("Creating SDP offer…", "info")
            offer = await self._pc.createOffer()
            await self._pc.setLocalDescription(offer)
            while self._pc.iceGatheringState != "complete":
                await asyncio.sleep(0.1)
            self._emit_log("ICE gathering complete", "info")
            self._emit_log("Sending SDP offer to WHIP endpoint…", "info")

            answer_sdp, resource_url = await self._loop.run_in_executor(
                None, self._whip_post, url, self._pc.localDescription.sdp, token,
            )
            if resource_url and not resource_url.startswith("http"):
                resource_url = urljoin(url, resource_url)
            self._whip_resource_url = resource_url
            self._emit_log("SDP answer received", "success")

            answer_sdp = _resolve_sdp_hostnames(answer_sdp)
            answer = RTCSessionDescription(sdp=answer_sdp, type="answer")
            await self._pc.setRemoteDescription(answer)
            self._emit_log("Remote description set — negotiation complete", "info")

            self._start_aiortc_capture(audio_source, device_id, sample_rate, gain)
        except Exception as exc:
            self._set_state("error")
            self._emit_log(f"Connection error: {exc}", "error")
            logger.exception("aiortc connection failed")
            self._stop_audio_capture()
            if self._pc:
                try:
                    await self._pc.close()
                except Exception:
                    pass
                self._pc = None
            self._track = None

    async def _aio_disconnect(self) -> None:
        try:
            if self._pc:
                await self._pc.close()
                self._pc = None
            if self._whip_resource_url:
                try:
                    await self._loop.run_in_executor(
                        None, self._whip_delete, self._whip_resource_url,
                    )
                    self._emit_log("WHIP resource deleted", "info")
                except Exception:
                    logger.debug("WHIP DELETE failed", exc_info=True)
                self._whip_resource_url = None
        except Exception:
            logger.debug("Error during disconnect", exc_info=True)

    # -- aiortc audio capture ----------------------------------------------

    def _start_aiortc_capture(self, audio_source, device_id, sample_rate, gain):
        if audio_source == "original":
            self._start_aiortc_original(device_id, sample_rate, gain)
        else:
            self._start_aiortc_translated(sample_rate)

    def _start_aiortc_original(self, device_id, sample_rate, gain):
        import sounddevice as sd

        rate = AudioBufferTrack.SAMPLE_RATE
        block = AudioBufferTrack.FRAME_SIZE

        def _cb(indata, frames, time_info, status):
            if status:
                logger.debug("aiortc capture: %s", status)
            if self._track is None:
                return
            amplified = indata[:, 0] * gain * 32767
            pcm = np.clip(amplified, -32768, 32767).astype(np.int16)
            self._track.push_audio(pcm, source_rate=rate)

        try:
            self._capture_stream = sd.InputStream(
                device=device_id, channels=1, samplerate=rate,
                dtype="float32", blocksize=block, callback=_cb,
            )
            self._capture_stream.start()
            self._emit_log("Capturing mic input (48 kHz, aiortc)", "success")
        except Exception as exc:
            self._emit_log(f"Capture failed: {exc}", "error")

    def _start_aiortc_translated(self, sample_rate):
        def _on_output(pcm_data: bytes) -> None:
            if self._track is None:
                return
            pcm = np.frombuffer(pcm_data, dtype=np.int16)
            self._track.push_audio(pcm, source_rate=sample_rate)

        self._output_listener_cb = _on_output
        self._audio_router.add_output_listener(_on_output)
        self._emit_log("Listening for translated TTS audio (aiortc)", "success")

    # ======================================================================
    # Shared helpers
    # ======================================================================

    def _stop_audio_capture(self) -> None:
        if self._capture_stream is not None:
            try:
                self._capture_stream.stop()
                self._capture_stream.close()
            except Exception:
                pass
            self._capture_stream = None
        if self._output_listener_cb is not None:
            self._audio_router.remove_output_listener(self._output_listener_cb)
            self._output_listener_cb = None

    def _cleanup_all(self) -> None:
        """Tear down any remnants from a prior run."""
        self._stop_audio_capture()
        # FFmpeg
        if self._process and self._process.poll() is None:
            self._process.kill()
        self._process = None
        if self._write_queue:
            self._write_queue.put(None)
        self._write_queue = None
        # aiortc
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._aio_thread and self._aio_thread.is_alive():
            self._aio_thread.join(timeout=2)
        self._loop = None
        self._aio_thread = None
        self._pc = None
        self._track = None
        self._whip_resource_url = None
        self._backend = None

    def _set_state(self, state: str) -> None:
        self._state = state
        self.state_changed.emit(state)

    def _emit_log(self, message: str, level: str) -> None:
        self.log_message.emit(message, level)
        log_fn = getattr(logger, level if level != "success" else "info", logger.info)
        log_fn("WebRTC: %s", message)

    @staticmethod
    def _whip_post(url: str, sdp: str, token: str) -> tuple[str, str | None]:
        headers = {"Content-Type": "application/sdp"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            url, data=sdp.encode("utf-8"), headers=headers, method="POST",
        )
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

    @staticmethod
    def _whip_delete(url: str) -> None:
        req = urllib.request.Request(url, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception:
            logger.debug("WHIP DELETE failed for %s", url, exc_info=True)
