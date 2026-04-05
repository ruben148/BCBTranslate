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
import subprocess
import sys
import threading
import time
from typing import Callable
from urllib.parse import urljoin

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from core.translated_pcm_adapter import TranslatedPcmStereoAdapter
from core.whip_core import (
    WHIPProxy,
    resolve_sdp_hostnames,
    whip_delete_resource,
    whip_post_offer,
)

logger = logging.getLogger(__name__)

_CREATIONFLAGS = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

# Brief pause before opening a new WHIP session after the last one was torn down
# (helps peers that release ICE/DTLS slightly behind our HTTP signaling).
_WHIP_RECONNECT_SETTLE_SEC = 0.25

# Max rate for forwarding RMS into the WebRTC stream VU meter (audio thread).
_STREAM_LEVEL_EMIT_INTERVAL_SEC = 0.05

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


HAS_FFMPEG_WHIP = False
_ffmpeg_path = _find_ffmpeg()
if _ffmpeg_path:
    HAS_FFMPEG_WHIP = _ffmpeg_supports_whip(_ffmpeg_path)

HAS_ANY_BACKEND = HAS_FFMPEG_WHIP or HAS_AIORTC

_STUN_SERVERS = [
    "stun:stun.l.google.com:19302",
    "stun:stun1.l.google.com:19302",
]


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
    stream_level_changed = pyqtSignal(float)

    def __init__(self, audio_router, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._audio_router = audio_router
        self._state = "idle"
        self._backend: str | None = None  # "ffmpeg" | "aiortc"
        self._closing = False

        # Shared
        self._capture_stream = None
        self._input_listener_cb: Callable | None = None
        self._output_listener_cb: Callable[[bytes], None] | None = None

        # FFmpeg backend
        self._process: subprocess.Popen | None = None
        self._write_queue: queue.Queue[bytes | None] | None = None
        self._writer_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._translated_feeder_thread: threading.Thread | None = None
        self._whip_proxy: WHIPProxy | None = None

        # aiortc backend
        self._loop: asyncio.AbstractEventLoop | None = None
        self._aio_thread: threading.Thread | None = None
        self._pc = None
        self._track: AudioBufferTrack | None = None
        self._whip_resource_url: str | None = None
        self._whip_bearer_token: str = ""
        self._whip_earliest_reconnect_mono: float = 0.0

        # WebRTC-only gain + VU (see WebRTCPanel stream meter).
        self._stream_gain: float = 1.0
        self._stream_level_last_emit: float = 0.0

    def set_stream_gain(self, gain: float) -> None:
        """Apply multiplier to audio sent on the WHIP stream only (0…5×)."""
        self._stream_gain = max(0.0, min(5.0, float(gain)))

    def _emit_stream_level_rms(self, rms: float) -> None:
        if self._closing:
            return
        now = time.monotonic()
        if now - self._stream_level_last_emit < _STREAM_LEVEL_EMIT_INTERVAL_SEC:
            return
        self._stream_level_last_emit = now
        try:
            self.stream_level_changed.emit(rms)
        except RuntimeError:
            pass

    def _reset_stream_level_meter(self) -> None:
        self._stream_level_last_emit = 0.0
        if self._closing:
            return
        try:
            self.stream_level_changed.emit(0.0)
        except RuntimeError:
            pass

    def _mark_whip_reconnect_settle(self) -> None:
        self._whip_earliest_reconnect_mono = (
            time.monotonic() + _WHIP_RECONNECT_SETTLE_SEC
        )

    def _wait_whip_reconnect_settle(self) -> None:
        rem = self._whip_earliest_reconnect_mono - time.monotonic()
        if rem > 0:
            time.sleep(rem)

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
        stream_gain: float = 1.0,
        preferred_backend: str = "ffmpeg",
    ) -> None:
        if self._state not in ("idle", "error"):
            self._emit_log("Stream is already active", "warning")
            return
        if not whip_url.strip():
            self._emit_log("WHIP URL is required", "error")
            return

        if preferred_backend not in ("ffmpeg", "aiortc"):
            preferred_backend = "ffmpeg"

        self._closing = False
        self.set_stream_gain(stream_gain)
        self._cleanup_all()
        self._set_state("connecting")

        if preferred_backend == "ffmpeg":
            ffmpeg = _find_ffmpeg()
            if not ffmpeg or not _ffmpeg_supports_whip(ffmpeg):
                self._set_state("error")
                self._emit_log(
                    "FFmpeg not found or missing WHIP muxer support", "error",
                )
                return
            self._backend = "ffmpeg"
            self._emit_log("Starting stream (FFmpeg backend)…", "info")
            self._start_ffmpeg(
                ffmpeg, whip_url.strip(), bearer_token.strip(),
                audio_source, input_device_id, sample_rate, gain,
            )
        elif preferred_backend == "aiortc":
            if not HAS_AIORTC:
                self._set_state("error")
                self._emit_log(
                    "aiortc is not installed (pip install aiortc)", "error",
                )
                return
            self._backend = "aiortc"
            self._emit_log("Starting stream (aiortc backend)…", "info")
            self._start_aiortc(
                whip_url.strip(), bearer_token.strip(),
                audio_source, input_device_id, sample_rate, gain,
            )
        else:
            self._set_state("error")
            self._emit_log(f"Unknown backend: {preferred_backend}", "error")

    def stop(self) -> None:
        if self._state == "idle":
            return
        self._set_state("stopping")
        self._emit_log("Stopping stream…", "info")
        try:
            self._stop_audio_capture()
            if self._backend == "ffmpeg":
                self._stop_ffmpeg()
            elif self._backend == "aiortc":
                self._stop_aiortc()
        except Exception:
            logger.exception("Error during stream stop")
        finally:
            self._backend = None
            self._set_state("idle")
            self._reset_stream_level_meter()
            self._emit_log("Stream stopped", "info")

    def shutdown(self) -> None:
        """Full teardown for application exit — safe to call from closeEvent."""
        self._closing = True
        try:
            if self._state != "idle":
                self._stop_audio_capture()
                if self._backend == "ffmpeg":
                    self._stop_ffmpeg()
                elif self._backend == "aiortc":
                    self._stop_aiortc()
            self._cleanup_all()
        except Exception:
            logger.exception("Error during stream shutdown")
        finally:
            self._state = "idle"

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

        self._wait_whip_reconnect_settle()

        self._whip_proxy = WHIPProxy(url, token)
        proxy_port = self._whip_proxy.start()
        proxy_url = f"http://127.0.0.1:{proxy_port}/"
        self._emit_log(
            f"WHIP proxy on :{proxy_port} (fixes ICE candidates)", "info",
        )

        cmd: list[str] = [
            ffmpeg, "-hide_banner", "-loglevel", "info",
            "-probesize", "32",
            "-analyzeduration", "0",
            "-fflags", "+nobuffer",
            "-f", "s16le",
            "-ar", str(capture_rate),
            "-ac", "2",
            "-i", "pipe:0",
            "-ar", "48000",
            "-c:a", "libopus",
            "-b:a", "128k",
            "-application", "lowdelay",
            "-frame_duration", "20",
            "-flush_packets", "1",
            "-max_delay", "0",
            "-f", "whip",
            "-handshake_timeout", "15000",
            proxy_url,
        ]

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

        self._write_queue = queue.Queue(maxsize=384)
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
        # Close stdin FIRST so any blocked stdin.write() in the writer
        # thread raises immediately, unblocking it.
        if self._process and self._process.stdin:
            try:
                self._process.stdin.close()
            except Exception:
                pass

        # Now signal the writer thread to exit (non-blocking so we never
        # deadlock if the queue is still full).
        if self._write_queue:
            try:
                self._write_queue.put_nowait(None)
            except queue.Full:
                pass

        # Wait for FFmpeg to exit; force-kill on timeout.
        if self._process:
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self._process.kill()
                except Exception:
                    pass
                try:
                    self._process.wait(timeout=2)
                except Exception:
                    pass
            except Exception:
                pass

        # Clear stdin writer / stderr only after the process is gone so readers
        # still see self._process is the Popen they started with (avoids applying
        # exit codes or "streaming" lines to a newer FFmpeg after stop/start).
        for t in (self._writer_thread, self._stderr_thread):
            if t is not None and t.is_alive():
                t.join(timeout=3)
        self._process = None
        self._write_queue = None
        if self._translated_feeder_thread is not None and self._translated_feeder_thread.is_alive():
            self._translated_feeder_thread.join(timeout=3)
        self._writer_thread = None
        self._stderr_thread = None
        self._translated_feeder_thread = None

        if self._whip_proxy:
            try:
                self._whip_proxy.delete_resource()
            except Exception:
                pass
            try:
                self._whip_proxy.stop()
            except Exception:
                pass
            self._whip_proxy = None
            self._mark_whip_reconnect_settle()

    def _ffmpeg_writer(self) -> None:
        """Drain the write queue into FFmpeg's stdin (dedicated thread)."""
        try:
            while True:
                wq = self._write_queue
                if wq is None:
                    break
                data = wq.get()
                if data is None:
                    break
                proc = self._process
                if proc is None or proc.poll() is not None:
                    break
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except Exception:
                    break
        except Exception:
            pass

    def _ffmpeg_stderr_reader(self) -> None:
        """Parse FFmpeg stderr and forward relevant lines to the UI log."""
        proc = self._process
        if proc is None:
            return
        connected_reported = False
        error_seen = False
        try:
            for raw_line in proc.stderr:
                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                lower = text.lower()

                if "error" in lower or "failed" in lower or "invalid" in lower:
                    if self._process is proc:
                        self._emit_log(text, "error")
                    error_seen = True
                elif (
                    self._process is proc
                    and not connected_reported
                    and not error_seen
                    and ("speed=" in lower or "size=" in lower)
                ):
                    connected_reported = True
                    self._set_state("streaming")
                    self._emit_log("Streaming audio", "success")
                elif any(k in lower for k in ("whip", "ice", "dtls")):
                    if self._process is proc:
                        self._emit_log(text, "info")
                elif "stream mapping" in lower or "output #" in lower:
                    if self._process is proc:
                        self._emit_log(text, "info")
        except Exception:
            pass
        if self._process is not proc:
            return
        rc = proc.poll()
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
            self._start_pipe_translated(capture_rate)

    def _start_pipe_original(
        self, device_id: int | None, capture_rate: int, gain: float
    ) -> None:
        def _on_input(indata: np.ndarray) -> None:
            try:
                wq = self._write_queue
                if wq is None:
                    return
                ch = indata[:, 0].astype(np.float64)
                scaled = ch * gain * self._stream_gain
                rms = float(np.sqrt(np.mean(scaled * scaled))) if scaled.size else 0.0
                self._emit_stream_level_rms(rms)
                pcm = np.clip(scaled * 32767.0, -32768, 32767).astype(np.int16)
                stereo = np.repeat(pcm, 2)
                wq.put_nowait(stereo.tobytes())
            except Exception:
                pass

        self._input_listener_cb = _on_input
        self._audio_router.add_input_listener(_on_input, device_id=device_id)
        self._emit_log("Capturing mic input (48 kHz, shared stream)", "success")

    def _start_pipe_translated(self, capture_rate: int) -> None:
        adapter = TranslatedPcmStereoAdapter(
            mono_sample_rate=capture_rate,
            frame_ms=20.0,
            max_buffer_seconds=2.0,
        )

        def _on_output(pcm_data: bytes) -> None:
            adapter.push_mono_pcm(pcm_data)

        self._output_listener_cb = _on_output
        self._audio_router.add_output_listener(_on_output)

        frame_duration = 0.02

        def _feeder() -> None:
            next_time = time.monotonic()
            while True:
                wq = self._write_queue
                if wq is None:
                    break
                frame = adapter.pop_stereo_s16le_frame()
                arr = np.frombuffer(frame, dtype=np.int16).reshape(-1, 2)
                scaled = np.clip(
                    arr.astype(np.float32) * self._stream_gain,
                    -32768.0,
                    32767.0,
                ).astype(np.int16)
                mono_f = scaled[:, 0].astype(np.float64) / 32768.0
                rms = float(np.sqrt(np.mean(mono_f * mono_f))) if mono_f.size else 0.0
                self._emit_stream_level_rms(rms)
                out = scaled.reshape(-1).tobytes()
                try:
                    wq.put_nowait(out)
                except queue.Full:
                    pass

                next_time += frame_duration
                delay = next_time - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -0.5:
                    next_time = time.monotonic()

        self._translated_feeder_thread = threading.Thread(
            target=_feeder, daemon=True, name="ffmpeg-translated-feeder",
        )
        self._translated_feeder_thread.start()
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
        self._whip_bearer_token = ""

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _aio_connect(
        self, url, token, audio_source, device_id, sample_rate, gain,
    ) -> None:
        self._whip_bearer_token = token
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
                None,
                whip_post_offer,
                url,
                self._pc.localDescription.sdp,
                token,
            )
            if resource_url and not resource_url.startswith("http"):
                resource_url = urljoin(url, resource_url)
            self._whip_resource_url = resource_url
            self._emit_log("SDP answer received", "success")

            answer_sdp = resolve_sdp_hostnames(answer_sdp)
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
                ru = self._whip_resource_url
                bt = self._whip_bearer_token

                def _do_delete() -> None:
                    whip_delete_resource(ru, bt)

                try:
                    await self._loop.run_in_executor(None, _do_delete)
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
        rate = AudioBufferTrack.SAMPLE_RATE  # 48 kHz

        def _on_input(indata: np.ndarray) -> None:
            try:
                track = self._track
                if track is None:
                    return
                ch = indata[:, 0].astype(np.float64)
                scaled = ch * gain * self._stream_gain
                rms = float(np.sqrt(np.mean(scaled * scaled))) if scaled.size else 0.0
                self._emit_stream_level_rms(rms)
                pcm = np.clip(scaled * 32767.0, -32768, 32767).astype(np.int16)
                track.push_audio(pcm, source_rate=rate)
            except Exception:
                pass

        self._input_listener_cb = _on_input
        self._audio_router.add_input_listener(_on_input, device_id=device_id)
        self._emit_log("Capturing mic input (48 kHz, shared stream)", "success")

    def _start_aiortc_translated(self, sample_rate):
        def _on_output(pcm_data: bytes) -> None:
            try:
                track = self._track
                if track is None:
                    return
                raw = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
                scaled = np.clip(raw * self._stream_gain, -32768.0, 32767.0)
                mono_f = scaled.astype(np.float64) / 32768.0
                rms = float(np.sqrt(np.mean(mono_f * mono_f))) if mono_f.size else 0.0
                self._emit_stream_level_rms(rms)
                pcm = scaled.astype(np.int16)
                track.push_audio(pcm, source_rate=sample_rate)
            except Exception:
                pass

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
        if self._input_listener_cb is not None:
            self._audio_router.remove_input_listener(self._input_listener_cb)
            self._input_listener_cb = None
        if self._output_listener_cb is not None:
            self._audio_router.remove_output_listener(self._output_listener_cb)
            self._output_listener_cb = None

    def _cleanup_all(self) -> None:
        """Tear down any remnants from a prior run."""
        try:
            self._stop_audio_capture()
        except Exception:
            pass

        # FFmpeg
        if self._process:
            if self._process.stdin:
                try:
                    self._process.stdin.close()
                except Exception:
                    pass
            if self._write_queue:
                try:
                    self._write_queue.put_nowait(None)
                except queue.Full:
                    pass
            if self._process.poll() is None:
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                except Exception:
                    pass
        self._write_queue = None
        for t in (self._writer_thread, self._stderr_thread, self._translated_feeder_thread):
            if t is not None and t.is_alive():
                t.join(timeout=2)
        self._process = None
        self._writer_thread = None
        self._stderr_thread = None
        self._translated_feeder_thread = None
        if self._whip_proxy:
            try:
                self._whip_proxy.delete_resource()
            except Exception:
                pass
            try:
                self._whip_proxy.stop()
            except Exception:
                pass
            self._whip_proxy = None
            self._mark_whip_reconnect_settle()

        # aiortc
        if self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
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
        if self._closing:
            return
        try:
            self.state_changed.emit(state)
        except RuntimeError:
            pass

    def _emit_log(self, message: str, level: str) -> None:
        log_fn = getattr(logger, level if level != "success" else "info", logger.info)
        log_fn("WebRTC: %s", message)
        if self._closing:
            return
        try:
            self.log_message.emit(message, level)
        except RuntimeError:
            pass
