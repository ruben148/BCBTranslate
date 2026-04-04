"""WebRTC WHIP audio streamer.

Streams audio (original mic input or translated TTS output) to a WebRTC
server using the WHIP (WebRTC-HTTP Ingestion Protocol) protocol.
"""

from __future__ import annotations

import asyncio
import collections
import fractions
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urljoin

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

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
    """Resolve hostname-based ICE candidates in the SDP to IP addresses.

    aioice rejects candidates whose connection-address is not a valid
    IPv4/IPv6 literal.  Some WHIP servers (e.g. mediamtx) emit candidates
    with DNS hostnames instead.  We resolve them here so ICE can proceed.
    """
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
                        logger.info(
                            "Resolved ICE candidate hostname %s → %s", hostname, ip
                        )
                        parts[4] = ip
                        line = " ".join(parts)
                except socket.gaierror:
                    logger.warning(
                        "Cannot resolve ICE candidate hostname: %s", hostname
                    )
        result.append(line)
    return sep.join(result)


class AudioBufferTrack(MediaStreamTrack):
    """Audio track fed by an external PCM buffer (thread-safe).

    Accepts 16-bit mono PCM at any sample rate, resamples to 48 kHz
    internally, and delivers 20 ms Opus-ready frames via ``recv()``.
    """

    kind = "audio"

    SAMPLE_RATE = 48000
    FRAME_DURATION_MS = 20
    FRAME_SIZE = SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 960
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
        """Push PCM int16 samples from any thread."""
        if source_rate != self.SAMPLE_RATE:
            pcm_int16 = self._resample(pcm_int16, source_rate, self.SAMPLE_RATE)
        with self._lock:
            self._chunks.append(pcm_int16)
            self._total_samples += len(pcm_int16)
            while self._total_samples > self._max_samples and self._chunks:
                dropped = self._chunks.popleft()
                self._total_samples -= len(dropped)

    async def recv(self):  # noqa: D401 — aiortc API
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
        """Pull exactly *needed* samples from the chunk deque (lock held)."""
        parts: list[np.ndarray] = []
        remaining = needed
        # Consume leftover partial chunk first
        if self._partial.size:
            take = min(remaining, len(self._partial))
            parts.append(self._partial[:take])
            self._partial = self._partial[take:] if take < len(self._partial) else np.array([], dtype=np.int16)
            remaining -= take
        # Then consume full chunks
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
    def _resample(
        samples: np.ndarray, from_rate: int, to_rate: int
    ) -> np.ndarray:
        if from_rate == to_rate:
            return samples
        n_out = int(len(samples) * to_rate / from_rate)
        x_old = np.arange(len(samples))
        x_new = np.linspace(0, len(samples) - 1, n_out)
        return np.interp(x_new, x_old, samples.astype(np.float64)).astype(np.int16)


class WebRTCStreamer(QObject):
    """Streams audio to a WebRTC endpoint via the WHIP protocol."""

    log_message = pyqtSignal(str, str)   # (message, level)
    state_changed = pyqtSignal(str)      # idle | connecting | streaming | error | stopping

    def __init__(self, audio_router, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._audio_router = audio_router
        self._state = "idle"

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pc: RTCPeerConnection | None = None
        self._track: AudioBufferTrack | None = None
        self._capture_stream = None
        self._whip_resource_url: str | None = None
        self._output_listener_cb: Callable[[bytes], None] | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_streaming(self) -> bool:
        return self._state == "streaming"

    # -- public api --------------------------------------------------------

    def start(
        self,
        whip_url: str,
        bearer_token: str,
        audio_source: str,
        input_device_id: int | None = None,
        sample_rate: int = 16000,
        gain: float = 1.0,
    ) -> None:
        if not HAS_AIORTC:
            self._emit_log(
                "aiortc is not installed. Run: pip install aiortc", "error"
            )
            return

        if self._state not in ("idle", "error"):
            self._emit_log("Stream is already active", "warning")
            return

        if not whip_url.strip():
            self._emit_log("WHIP URL is required", "error")
            return

        self._cleanup_previous()

        self._set_state("connecting")
        self._emit_log("Starting WebRTC stream…", "info")

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="webrtc-loop"
        )
        self._thread.start()

        asyncio.run_coroutine_threadsafe(
            self._connect(
                whip_url.strip(),
                bearer_token.strip(),
                audio_source,
                input_device_id,
                sample_rate,
                gain,
            ),
            self._loop,
        )

    def stop(self) -> None:
        if self._state == "idle":
            return

        self._set_state("stopping")
        self._emit_log("Stopping stream…", "info")

        self._stop_audio_capture()

        if self._loop and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception:
                logger.debug("Disconnect timed out", exc_info=True)
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

        self._loop = None
        self._thread = None
        self._pc = None
        self._track = None
        self._whip_resource_url = None

        self._set_state("idle")
        self._emit_log("Stream stopped", "info")

    # -- internals ---------------------------------------------------------

    def _cleanup_previous(self) -> None:
        """Tear down remnants of a prior attempt (e.g. after an error)."""
        self._stop_audio_capture()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._loop = None
        self._thread = None
        self._pc = None
        self._track = None
        self._whip_resource_url = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _connect(
        self,
        url: str,
        token: str,
        audio_source: str,
        device_id: int | None,
        sample_rate: int,
        gain: float,
    ) -> None:
        try:
            self._track = AudioBufferTrack()
            self._emit_log("Audio track created (48 kHz mono Opus)", "info")

            config = RTCConfiguration(
                iceServers=[RTCIceServer(urls=_STUN_SERVERS)]
            )
            self._pc = RTCPeerConnection(configuration=config)

            @self._pc.on("connectionstatechange")
            async def _on_conn_state():
                st = self._pc.connectionState
                self._emit_log(f"Connection state: {st}", "info")
                if st == "connected":
                    self._set_state("streaming")
                    self._emit_log("Streaming audio", "success")
                elif st == "failed":
                    self._set_state("error")
                    self._emit_log("Connection failed", "error")
                elif st == "closed":
                    if self._state != "stopping":
                        self._set_state("idle")

            @self._pc.on("iceconnectionstatechange")
            async def _on_ice():
                self._emit_log(
                    f"ICE connection state: {self._pc.iceConnectionState}", "info"
                )

            self._pc.addTrack(self._track)
            self._emit_log("Creating SDP offer…", "info")

            offer = await self._pc.createOffer()
            await self._pc.setLocalDescription(offer)

            # Wait for ICE gathering
            while self._pc.iceGatheringState != "complete":
                await asyncio.sleep(0.1)
            self._emit_log("ICE gathering complete", "info")

            self._emit_log("Sending SDP offer to WHIP endpoint…", "info")
            answer_sdp, resource_url = await self._loop.run_in_executor(
                None,
                self._whip_post,
                url,
                self._pc.localDescription.sdp,
                token,
            )
            if resource_url and not resource_url.startswith("http"):
                resource_url = urljoin(url, resource_url)
            self._whip_resource_url = resource_url
            self._emit_log("SDP answer received", "success")

            answer_sdp = _resolve_sdp_hostnames(answer_sdp)

            answer = RTCSessionDescription(sdp=answer_sdp, type="answer")
            await self._pc.setRemoteDescription(answer)
            self._emit_log("Remote description set — negotiation complete", "info")

            self._start_audio_capture(audio_source, device_id, sample_rate, gain)

        except Exception as exc:
            self._set_state("error")
            self._emit_log(f"Connection error: {exc}", "error")
            logger.exception("WebRTC connection failed")
            self._stop_audio_capture()
            if self._pc:
                try:
                    await self._pc.close()
                except Exception:
                    pass
                self._pc = None
            self._track = None

    async def _disconnect(self) -> None:
        try:
            if self._pc:
                await self._pc.close()
                self._pc = None
            if self._whip_resource_url:
                try:
                    await self._loop.run_in_executor(
                        None, self._whip_delete, self._whip_resource_url
                    )
                    self._emit_log("WHIP resource deleted", "info")
                except Exception:
                    logger.debug("Failed to DELETE WHIP resource", exc_info=True)
                self._whip_resource_url = None
        except Exception:
            logger.debug("Error during disconnect", exc_info=True)

    # -- audio capture -----------------------------------------------------

    def _start_audio_capture(
        self,
        audio_source: str,
        device_id: int | None,
        sample_rate: int,
        gain: float,
    ) -> None:
        if audio_source == "original":
            self._start_original_capture(device_id, sample_rate, gain)
        else:
            self._start_translated_capture(sample_rate)

    def _start_original_capture(
        self, device_id: int | None, sample_rate: int, gain: float
    ) -> None:
        import sounddevice as sd

        # Capture natively at 48 kHz with one-Opus-frame blocks (960 samples
        # = 20 ms) to avoid resampling and minimise capture latency.
        capture_rate = AudioBufferTrack.SAMPLE_RATE  # 48000
        block = AudioBufferTrack.FRAME_SIZE           # 960

        def _callback(indata, frames, time_info, status):
            if status:
                logger.debug("WebRTC capture status: %s", status)
            if self._track is None:
                return
            amplified = indata[:, 0] * gain * 32767
            pcm = np.clip(amplified, -32768, 32767).astype(np.int16)
            self._track.push_audio(pcm, source_rate=capture_rate)

        try:
            self._capture_stream = sd.InputStream(
                device=device_id,
                channels=1,
                samplerate=capture_rate,
                dtype="float32",
                blocksize=block,
                callback=_callback,
            )
            self._capture_stream.start()
            self._emit_log("Capturing original audio from mic input (48 kHz)", "success")
        except Exception as exc:
            self._emit_log(f"Failed to start audio capture: {exc}", "error")

    def _start_translated_capture(self, sample_rate: int) -> None:
        def _on_output(pcm_data: bytes) -> None:
            if self._track is None:
                return
            pcm = np.frombuffer(pcm_data, dtype=np.int16)
            self._track.push_audio(pcm, source_rate=sample_rate)

        self._output_listener_cb = _on_output
        self._audio_router.add_output_listener(_on_output)
        self._emit_log("Listening for translated TTS audio output", "success")

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

    # -- helpers -----------------------------------------------------------

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
            url, data=sdp.encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                answer_sdp = resp.read().decode("utf-8")
                location = resp.headers.get("Location")
                return answer_sdp, location
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise RuntimeError(
                f"WHIP server returned HTTP {exc.code}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Cannot reach WHIP server: {exc.reason}"
            ) from exc

    @staticmethod
    def _whip_delete(url: str) -> None:
        req = urllib.request.Request(url, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception:
            logger.debug("WHIP DELETE failed for %s", url, exc_info=True)
