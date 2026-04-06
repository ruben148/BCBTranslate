from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time


class DeviceDirection(Enum):
    INPUT = "input"
    OUTPUT = "output"


@dataclass
class AudioDevice:
    device_id: int
    name: str
    host_api: str
    channels: int
    default_sample_rate: float
    is_virtual: bool
    direction: DeviceDirection
    is_default: bool = False

    VIRTUAL_KEYWORDS = (
        "cable", "voicemeeter", "vb-audio", "virtual", "voice meeter",
    )

    @classmethod
    def from_sounddevice_info(
        cls, index: int, info: dict, direction: DeviceDirection,
        default_input_idx: int = -1, default_output_idx: int = -1,
    ) -> AudioDevice:
        name = info["name"]
        host_api_name = info.get("host_api_name", "")
        is_virtual = any(kw in name.lower() for kw in cls.VIRTUAL_KEYWORDS)
        is_default = (
            (direction == DeviceDirection.INPUT and index == default_input_idx)
            or (direction == DeviceDirection.OUTPUT and index == default_output_idx)
        )
        ch = (
            info["max_input_channels"]
            if direction == DeviceDirection.INPUT
            else info["max_output_channels"]
        )
        return cls(
            device_id=index,
            name=name,
            host_api=host_api_name,
            channels=ch,
            default_sample_rate=info["default_samplerate"],
            is_virtual=is_virtual,
            direction=direction,
            is_default=is_default,
        )

    def display_name(self) -> str:
        parts = [self.name]
        if self.is_virtual:
            parts.append("[Virtual]")
        if self.is_default:
            parts.append("(Default)")
        return " ".join(parts)

    def __str__(self) -> str:
        return self.display_name()


@dataclass
class Utterance:
    text: str
    source_text: str = ""
    recognized_at: float = field(default_factory=time.monotonic)
    queued_at: float = 0.0
    synthesis_started_at: float = 0.0
    synthesis_done_at: float = 0.0

    @property
    def total_lag_ms(self) -> int:
        if self.synthesis_done_at > 0 and self.recognized_at > 0:
            return int((self.synthesis_done_at - self.recognized_at) * 1000)
        return 0

    @property
    def queue_wait_ms(self) -> int:
        if self.synthesis_started_at > 0 and self.queued_at > 0:
            return int((self.synthesis_started_at - self.queued_at) * 1000)
        return 0


@dataclass
class TranslationMetrics:
    current_lag_ms: int = 0
    avg_lag_ms: int = 0
    queue_depth: int = 0
    total_utterances: int = 0
    dropped_utterances: int = 0
    azure_errors: int = 0
    session_duration_s: float = 0.0
    is_connected: bool = False
    last_error: str | None = None
    effective_rate: float = 1.0


@dataclass
class AppConfig:
    # Azure
    speech_key_env_var: str = "AZURE_SPEECH_KEY"
    speech_region_env_var: str = "AZURE_SPEECH_REGION"

    # Translation
    translation_mode: str = "standard"  # "standard" or "interpreter"
    source_language: str = "ro-RO"
    target_language: str = "en"
    profanity_filter: str = "masked"
    continuous_language_detection: bool = False

    # TTS
    voice_name: str = "en-US-JennyNeural"
    speaking_rate: float = 1.0
    pitch: str = "+0%"
    tts_volume: int = 100

    # Audio devices — stored by name for stability across sessions
    input_device_name: str | None = None
    output_device_name: str | None = None
    secondary_output_device_name: str | None = None
    sample_rate: int = 16000
    channels: int = 1
    noise_suppression: bool = False
    input_gain: float = 1.0
    segmentation_silence_timeout_ms: int = 500
    auto_segmentation_enabled: bool = False
    auto_seg_target_min_s: float = 5.0
    auto_seg_target_max_s: float = 15.0
    # When True, do not set SegmentationSilenceTimeoutMs / EndSilenceTimeoutMs;
    # Azure service defaults apply. Semantic segmentation stays enabled.
    use_default_segmentation: bool = False

    # UI
    always_on_top: bool = False
    start_minimized: bool = False
    theme: str = "dark"
    hotkey_start_stop: str = "Ctrl+Shift+T"
    show_vu_meter: bool = True
    audio_devices_expanded: bool = False
    translation_section_expanded: bool = False

    # Logging
    log_to_file: bool = False
    log_directory: str = ""
    log_level: str = "INFO"
    save_transcripts: bool = False
    transcript_directory: str = ""

    # Advanced
    tts_queue_warning_threshold: int = 3
    max_tts_queue_size: int = 10
    reconnect_attempts: int = 5
    reconnect_delay_seconds: int = 2
    adaptive_rate_enabled: bool = True
    drop_oldest_on_overflow: bool = True

    # Updates
    auto_check_updates: bool = True

    # WebRTC streaming
    webrtc_whip_url: str = ""
    webrtc_bearer_token: str = ""
    webrtc_audio_source: str = "original"
    webrtc_backend: str = "ffmpeg"  # "ffmpeg" or "aiortc"
    # Gain applied only to PCM sent to the WebRTC encoder (not Azure or speakers).
    webrtc_stream_gain: float = 1.0
