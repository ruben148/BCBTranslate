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

    def __init__(self):
        self._input_stream: sd.InputStream | None = None
        self._output_streams: list[sd.RawOutputStream] = []
        self._vu_callback: Callable[[float], None] | None = None
        self._vu_lock = threading.Lock()
        self._current_rms: float = 0.0
        self._gain: float = 1.0
        self._output_listeners: list[Callable[[bytes], None]] = []
        self._output_listeners_lock = threading.Lock()

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
        self.stop_vu_stream()
        self._vu_callback = callback

        def _audio_cb(indata, frames, time_info, status):
            if status:
                logger.debug("VU stream status: %s", status)
            rms = float(np.sqrt(np.mean((indata * self._gain) ** 2)))
            with self._vu_lock:
                self._current_rms = rms
            if self._vu_callback:
                self._vu_callback(rms)

        try:
            self._input_stream = sd.InputStream(
                device=device_id,
                channels=1,
                samplerate=16000,
                dtype="float32",
                blocksize=1024,
                callback=_audio_cb,
            )
            self._input_stream.start()
            logger.info("VU meter stream started (device=%s)", device_id)
        except Exception:
            logger.exception("Failed to start VU meter stream")

    def stop_vu_stream(self) -> None:
        if self._input_stream is not None:
            try:
                self._input_stream.stop()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None
            with self._vu_lock:
                self._current_rms = 0.0

    # -- output streams for dual output ------------------------------------

    def open_output_streams(
        self,
        primary_device_id: int | None,
        secondary_device_id: int | None = None,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        self.close_output_streams()
        device_ids = [primary_device_id]
        if secondary_device_id is not None:
            device_ids.append(secondary_device_id)
        for dev_id in device_ids:
            try:
                stream = sd.RawOutputStream(
                    device=dev_id,
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="int16",
                )
                stream.start()
                self._output_streams.append(stream)
                logger.info("Output stream opened (device=%s)", dev_id)
            except Exception:
                logger.exception("Failed to open output stream (device=%s)", dev_id)

    def write_output(self, pcm_data: bytes) -> None:
        audio_array = np.frombuffer(pcm_data, dtype=np.int16)
        for stream in self._output_streams:
            try:
                stream.write(audio_array)
            except Exception:
                logger.debug("Output stream write error", exc_info=True)
        with self._output_listeners_lock:
            for listener in self._output_listeners:
                try:
                    listener(pcm_data)
                except Exception:
                    logger.debug("Output listener error", exc_info=True)

    def add_output_listener(self, callback: Callable[[bytes], None]) -> None:
        with self._output_listeners_lock:
            if callback not in self._output_listeners:
                self._output_listeners.append(callback)

    def remove_output_listener(self, callback: Callable[[bytes], None]) -> None:
        with self._output_listeners_lock:
            if callback in self._output_listeners:
                self._output_listeners.remove(callback)

    def close_output_streams(self) -> None:
        for stream in self._output_streams:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._output_streams.clear()

    # -- cleanup -----------------------------------------------------------

    def shutdown(self) -> None:
        self.stop_vu_stream()
        self.close_output_streams()
