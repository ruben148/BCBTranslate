from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np
import sounddevice as sd

from core.models import AudioDevice, DeviceDirection

logger = logging.getLogger(__name__)


class AudioRouter:
    """Enumerate audio devices, manage input/output streams, provide VU levels."""

    _INPUT_RATE = 48_000
    _INPUT_BLOCKSIZE = 960  # 20 ms at 48 kHz
    # Azure TTS often delivers ~0.5s PCM bursts; feeding PortAudio in small
    # steady blocks keeps the host buffer full between bursts.
    _TTS_PLAYBACK_CHUNK_FRAMES = 480  # 30 ms at 16 kHz mono

    def __init__(self):
        self._input_stream: sd.InputStream | None = None
        self._input_device_id: int | None = None
        self._vu_active = False
        self._output_streams: list[sd.RawOutputStream] = []
        self._tts_pcm_buffer = bytearray()
        self._tts_buffer_cond = threading.Condition()
        self._pcm_odd_byte: int | None = None
        self._playback_stop = False
        self._output_frame_bytes = 2
        self._output_chunk_bytes = self._TTS_PLAYBACK_CHUNK_FRAMES * 2
        self._playback_thread: threading.Thread | None = None
        self._vu_callback: Callable[[float], None] | None = None
        self._vu_lock = threading.Lock()
        self._current_rms: float = 0.0
        self._gain: float = 1.0
        self._output_listeners: list[Callable[[bytes], None]] = []
        self._output_listeners_lock = threading.Lock()
        self._output_device_lock = threading.Lock()
        self._input_listeners: list[Callable] = []
        self._input_listeners_lock = threading.Lock()

    # -- device enumeration ------------------------------------------------

    def list_input_devices(self) -> list[AudioDevice]:
        return self._list_devices(DeviceDirection.INPUT)

    def list_output_devices(self) -> list[AudioDevice]:
        return self._list_devices(DeviceDirection.OUTPUT)

    def _list_devices(self, direction: DeviceDirection) -> list[AudioDevice]:
        devices: list[AudioDevice] = []
        try:
            all_devs = sd.query_devices()
            host_apis = sd.query_hostapis()
            defaults = sd.default.device
            default_in = defaults[0] if isinstance(defaults, (list, tuple)) else defaults
            default_out = defaults[1] if isinstance(defaults, (list, tuple)) else defaults
        except Exception:
            logger.exception("Failed to enumerate audio devices")
            return devices

        for i, info in enumerate(all_devs):
            ch_key = (
                "max_input_channels"
                if direction == DeviceDirection.INPUT
                else "max_output_channels"
            )
            if info[ch_key] < 1:
                continue

            api_idx = info.get("hostapi", 0)
            api_name = host_apis[api_idx]["name"] if api_idx < len(host_apis) else ""
            enriched = dict(info)
            enriched["host_api_name"] = api_name

            dev = AudioDevice.from_sounddevice_info(
                index=i,
                info=enriched,
                direction=direction,
                default_input_idx=default_in,
                default_output_idx=default_out,
            )
            devices.append(dev)

        return devices

    def find_device_by_name(
        self, name: str | None, direction: DeviceDirection
    ) -> AudioDevice | None:
        if not name:
            return None
        devs = (
            self.list_input_devices()
            if direction == DeviceDirection.INPUT
            else self.list_output_devices()
        )
        for d in devs:
            if d.name == name:
                return d
        for d in devs:
            if name.lower() in d.name.lower():
                return d
        return None

    def get_default_device(self, direction: DeviceDirection) -> AudioDevice | None:
        devs = (
            self.list_input_devices()
            if direction == DeviceDirection.INPUT
            else self.list_output_devices()
        )
        for d in devs:
            if d.is_default:
                return d
        return devs[0] if devs else None

    # -- VU meter ----------------------------------------------------------

    @property
    def current_rms(self) -> float:
        with self._vu_lock:
            return self._current_rms

    @property
    def gain(self) -> float:
        return self._gain

    @gain.setter
    def gain(self, value: float) -> None:
        self._gain = max(0.0, value)

    def start_vu_stream(
        self, device_id: int | None, callback: Callable[[float], None] | None = None
    ) -> None:
        self._vu_callback = callback
        self._vu_active = True
        self._ensure_input_stream(device_id)

    def stop_vu_stream(self) -> None:
        self._vu_callback = None
        self._vu_active = False
        with self._vu_lock:
            self._current_rms = 0.0
        self._maybe_stop_input_stream()

    # -- shared input stream -----------------------------------------------

    def _ensure_input_stream(self, device_id: int | None) -> None:
        if (
            self._input_stream is not None
            and self._input_device_id == device_id
        ):
            return
        self._close_input_stream()

        def _audio_cb(indata, frames, time_info, status):
            if status:
                logger.debug("Input stream status: %s", status)
            rms = float(np.sqrt(np.mean((indata * self._gain) ** 2)))
            with self._vu_lock:
                self._current_rms = rms
            cb = self._vu_callback
            if cb is not None:
                cb(rms)
            with self._input_listeners_lock:
                for listener in self._input_listeners:
                    try:
                        listener(indata)
                    except Exception:
                        logger.debug("Input listener error", exc_info=True)

        try:
            self._input_stream = sd.InputStream(
                device=device_id,
                channels=1,
                samplerate=self._INPUT_RATE,
                dtype="float32",
                blocksize=self._INPUT_BLOCKSIZE,
                callback=_audio_cb,
            )
            self._input_stream.start()
            self._input_device_id = device_id
            logger.info(
                "Shared input stream started (device=%s, %d Hz)",
                device_id,
                self._INPUT_RATE,
            )
        except Exception:
            logger.exception("Failed to start shared input stream")

    def _close_input_stream(self) -> None:
        if self._input_stream is not None:
            try:
                self._input_stream.stop()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None

    def _maybe_stop_input_stream(self) -> None:
        with self._input_listeners_lock:
            has_listeners = len(self._input_listeners) > 0
        if not has_listeners and not self._vu_active:
            self._close_input_stream()

    def add_input_listener(
        self, callback: Callable, device_id: int | None = None,
    ) -> None:
        with self._input_listeners_lock:
            if callback not in self._input_listeners:
                self._input_listeners.append(callback)
        if self._input_stream is None and device_id is not None:
            self._ensure_input_stream(device_id)

    def remove_input_listener(self, callback: Callable) -> None:
        with self._input_listeners_lock:
            if callback in self._input_listeners:
                self._input_listeners.remove(callback)
        self._maybe_stop_input_stream()

    # -- output streams for dual output ------------------------------------

    def _create_output_streams(
        self,
        primary_device_id: int | None,
        secondary_device_id: int | None,
        sample_rate: int,
        channels: int,
    ) -> list[sd.RawOutputStream]:
        device_ids = [primary_device_id]
        if secondary_device_id is not None:
            device_ids.append(secondary_device_id)
        streams: list[sd.RawOutputStream] = []
        for dev_id in device_ids:
            try:
                stream = self._open_tts_raw_output_stream(
                    device_id=dev_id,
                    sample_rate=sample_rate,
                    channels=channels,
                )
                stream.start()
                streams.append(stream)
                logger.info("Output stream opened (device=%s)", dev_id)
            except Exception:
                logger.exception("Failed to open output stream (device=%s)", dev_id)
        return streams

    def open_output_streams(
        self,
        primary_device_id: int | None,
        secondary_device_id: int | None = None,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        self.close_output_streams()
        self._output_frame_bytes = 2 * max(1, channels)
        self._output_chunk_bytes = self._TTS_PLAYBACK_CHUNK_FRAMES * channels * 2

        streams = self._create_output_streams(
            primary_device_id,
            secondary_device_id,
            sample_rate,
            channels,
        )
        with self._output_device_lock:
            self._output_streams = streams

        if self._output_streams:
            with self._tts_buffer_cond:
                self._tts_pcm_buffer.clear()
                self._pcm_odd_byte = None
                self._playback_stop = False
            self._playback_thread = threading.Thread(
                target=self._output_playback_worker,
                daemon=True,
                name="tts-output-playback",
            )
            self._playback_thread.start()

    def switch_output_streams(
        self,
        primary_device_id: int | None,
        secondary_device_id: int | None = None,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        """Rebind TTS playback to new device(s) without stopping the playback thread.

        Used when the operator changes output while translation is running. The
        jitter buffer and Azure push callback stay attached; only PortAudio
        sinks are replaced.
        """
        self._output_frame_bytes = 2 * max(1, channels)
        self._output_chunk_bytes = self._TTS_PLAYBACK_CHUNK_FRAMES * channels * 2

        if self._playback_thread is None or not self._playback_thread.is_alive():
            self.open_output_streams(
                primary_device_id,
                secondary_device_id,
                sample_rate,
                channels,
            )
            return

        new_streams = self._create_output_streams(
            primary_device_id,
            secondary_device_id,
            sample_rate,
            channels,
        )
        if not new_streams:
            logger.error(
                "switch_output_streams: could not open any device; "
                "keeping previous output streams"
            )
            return

        with self._output_device_lock:
            old = self._output_streams
            self._output_streams = new_streams

        for stream in old:
            try:
                stream.stop()
                stream.close()
            except Exception:
                logger.debug("Error closing old output stream", exc_info=True)

    @staticmethod
    def _open_tts_raw_output_stream(
        *,
        device_id: int | None,
        sample_rate: int,
        channels: int,
    ) -> sd.RawOutputStream:
        """Open output with a large target latency so ~500ms gaps between Azure
        chunks do not empty the driver buffer."""
        common = dict(
            device=device_id,
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=AudioRouter._TTS_PLAYBACK_CHUNK_FRAMES,
        )
        try:
            return sd.RawOutputStream(**common, latency=1.0)
        except Exception:
            logger.debug(
                "1.0s output latency not accepted; falling back to high/default",
                exc_info=True,
            )
        try:
            return sd.RawOutputStream(**common, latency="high")
        except Exception:
            return sd.RawOutputStream(**common)

    def _output_playback_worker(self) -> None:
        """Feed PortAudio in fixed small blocks from a jitter buffer so playback
        stays continuous when the Azure SDK delivers audio in large bursts."""
        chunk_b = self._output_chunk_bytes
        frame_b = self._output_frame_bytes

        while True:
            raw: bytes | None = None
            with self._tts_buffer_cond:
                while True:
                    if self._playback_stop and len(self._tts_pcm_buffer) == 0:
                        return
                    buflen = len(self._tts_pcm_buffer)
                    if buflen >= chunk_b:
                        raw = bytes(self._tts_pcm_buffer[:chunk_b])
                        del self._tts_pcm_buffer[:chunk_b]
                        break
                    if self._playback_stop and buflen >= frame_b:
                        aligned = (buflen // frame_b) * frame_b
                        raw = bytes(self._tts_pcm_buffer[:aligned])
                        del self._tts_pcm_buffer[:aligned]
                        break
                    if self._playback_stop:
                        self._tts_pcm_buffer.clear()
                        return
                    self._tts_buffer_cond.wait(timeout=0.05)

            if not raw:
                continue
            try:
                audio_array = np.frombuffer(raw, dtype=np.int16)
                if audio_array.size == 0:
                    continue
                with self._output_device_lock:
                    streams = list(self._output_streams)
                for stream in streams:
                    stream.write(audio_array)
            except Exception:
                logger.debug("Output stream write error", exc_info=True)

    def _align_int16_pcm(self, pcm_data: bytes) -> bytes:
        """Ensure an even byte length so int16 samples stay frame-aligned.

        Odd-length chunks from speech SDKs (or WAV-strip edge cases) shift
        sample boundaries and cause severe crackling and pops.
        """
        if not pcm_data:
            return b""
        if self._pcm_odd_byte is not None:
            pcm_data = bytes((self._pcm_odd_byte,)) + pcm_data
            self._pcm_odd_byte = None
        if len(pcm_data) % 2:
            self._pcm_odd_byte = pcm_data[-1]
            pcm_data = pcm_data[:-1]
        return pcm_data

    def write_output(self, pcm_data: bytes) -> None:
        pcm_data = self._align_int16_pcm(pcm_data)
        # Notify listeners FIRST so WebRTC buffers receive data before the
        # playback queue (same ordering as before the playback thread existed).
        with self._output_listeners_lock:
            for listener in self._output_listeners:
                try:
                    listener(pcm_data)
                except Exception:
                    logger.debug("Output listener error", exc_info=True)

        if not pcm_data:
            return
        if self._playback_thread is None or not self._playback_thread.is_alive():
            return
        with self._tts_buffer_cond:
            self._tts_pcm_buffer.extend(pcm_data)
            self._tts_buffer_cond.notify_all()

    def add_output_listener(self, callback: Callable[[bytes], None]) -> None:
        with self._output_listeners_lock:
            if callback not in self._output_listeners:
                self._output_listeners.append(callback)

    def remove_output_listener(self, callback: Callable[[bytes], None]) -> None:
        with self._output_listeners_lock:
            if callback in self._output_listeners:
                self._output_listeners.remove(callback)

    def close_output_streams(self) -> None:
        if self._playback_thread is not None:
            with self._tts_buffer_cond:
                self._playback_stop = True
                self._tts_buffer_cond.notify_all()
            self._playback_thread.join(timeout=5.0)
            self._playback_thread = None
            with self._tts_buffer_cond:
                self._tts_pcm_buffer.clear()
                self._pcm_odd_byte = None
                self._playback_stop = False

        with self._output_device_lock:
            to_close = list(self._output_streams)
            self._output_streams.clear()
        for stream in to_close:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    # -- cleanup -----------------------------------------------------------

    def shutdown(self) -> None:
        self._vu_active = False
        self._vu_callback = None
        with self._input_listeners_lock:
            self._input_listeners.clear()
        self._close_input_stream()
        self.close_output_streams()
