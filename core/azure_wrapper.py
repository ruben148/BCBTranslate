from __future__ import annotations

import logging
import threading
import time
from collections import deque

import azure.cognitiveservices.speech as speechsdk
from PyQt6.QtCore import QObject, pyqtSignal

from core.audio_router import AudioRouter
from core.models import AppConfig, DeviceDirection
from utils.ssml_builder import SSMLBuilder

logger = logging.getLogger(__name__)

TICKS_PER_SECOND = 10_000_000


class AutoSegmentationManager:
    """Proportional controller that adjusts the SDK's segmentation silence
    timeout so that recognised-utterance durations stay within a target range.

    Only the SDK property is changed; audio is never cut application-side.
    """

    WINDOW = 5          # rolling window of recent utterances
    MIN_SAMPLES = 3     # minimum observations before acting
    MIN_CHANGE = 50     # ignore adjustments smaller than this (ms)
    STEP_SCALE = 300    # proportional gain factor

    def __init__(
        self,
        target_min_s: float = 5.0,
        target_max_s: float = 15.0,
        initial_timeout_ms: int = 500,
    ):
        self.target_min = target_min_s
        self.target_max = target_max_s
        self.current_timeout_ms = initial_timeout_ms
        self._durations: deque[float] = deque(maxlen=self.WINDOW)

    def reset(self, timeout_ms: int | None = None) -> None:
        self._durations.clear()
        if timeout_ms is not None:
            self.current_timeout_ms = timeout_ms

    def feed(self, duration_s: float) -> int | None:
        """Record an utterance duration. Returns a new timeout (ms) if an
        adjustment is warranted, otherwise ``None``."""
        if duration_s <= 0:
            return None
        self._durations.append(duration_s)

        if len(self._durations) < self.MIN_SAMPLES:
            return None

        avg = sum(self._durations) / len(self._durations)

        if avg > self.target_max:
            overshoot = (avg - self.target_max) / self.target_max
            step = max(self.MIN_CHANGE, int(overshoot * self.STEP_SCALE))
            new_timeout = self.current_timeout_ms - step
        elif avg < self.target_min:
            undershoot = (self.target_min - avg) / self.target_min
            step = max(self.MIN_CHANGE, int(undershoot * self.STEP_SCALE))
            new_timeout = self.current_timeout_ms + step
        else:
            return None

        new_timeout = max(100, min(5000, new_timeout))

        if abs(new_timeout - self.current_timeout_ms) < self.MIN_CHANGE:
            return None

        self.current_timeout_ms = new_timeout
        return new_timeout


class AzureTranslationService(QObject):
    """Wraps Azure Speech SDK for translation (STT) and synthesis (TTS)."""

    on_translated = pyqtSignal(str, str, float)   # source, translated, timestamp
    on_recognizing = pyqtSignal(str)               # partial result
    on_error = pyqtSignal(str)
    on_connected = pyqtSignal()
    on_disconnected = pyqtSignal()
    segmentation_timeout_updated = pyqtSignal(int, float)  # new_timeout_ms, avg_duration_s

    def __init__(
        self,
        speech_key: str,
        speech_region: str,
        config: AppConfig,
        audio_router: AudioRouter,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._speech_key = speech_key
        self._speech_region = speech_region
        self._config = config
        self._audio_router = audio_router

        self._recognizer: speechsdk.translation.TranslationRecognizer | None = None
        self._synthesizer: speechsdk.SpeechSynthesizer | None = None

        self._push_input_stream: speechsdk.audio.PushAudioInputStream | None = None
        self._push_output_stream: speechsdk.audio.PushAudioOutputStream | None = None

        self._is_recognizing = False
        self._lock = threading.Lock()
        self._restarting = False
        # Set False when the pipeline begins teardown so SDK callbacks and
        # auto-segmentation restarts cannot enqueue work after Stop.
        self._accept_events = True

        self._auto_seg = AutoSegmentationManager(
            target_min_s=config.auto_seg_target_min_s,
            target_max_s=config.auto_seg_target_max_s,
            initial_timeout_ms=config.segmentation_silence_timeout_ms,
        )

    # -- recognition (STT + translation) -----------------------------------

    def start_recognition(self) -> None:
        with self._lock:
            if self._is_recognizing:
                return
            try:
                self._build_recognizer()
                self._recognizer.start_continuous_recognition()
                self._is_recognizing = True
                self.on_connected.emit()
                logger.info("Azure recognition started")
            except Exception as exc:
                msg = f"Failed to start recognition: {exc}"
                logger.exception(msg)
                self.on_error.emit(msg)

    def stop_recognition(self) -> None:
        with self._lock:
            if not self._is_recognizing:
                return
            try:
                self._recognizer.stop_continuous_recognition()
            except Exception:
                logger.debug("Error stopping recognizer", exc_info=True)
            self._is_recognizing = False
            self._close_input_stream()
            self.on_disconnected.emit()
            logger.info("Azure recognition stopped")

    def _build_recognizer(self) -> None:
        translation_config = speechsdk.translation.SpeechTranslationConfig(
            subscription=self._speech_key,
            region=self._speech_region,
        )
        translation_config.speech_recognition_language = self._config.source_language
        translation_config.add_target_language(self._config.target_language)

        profanity = self._config.profanity_filter
        if profanity == "removed":
            translation_config.set_profanity(speechsdk.ProfanityOption.Removed)
        elif profanity == "raw":
            translation_config.set_profanity(speechsdk.ProfanityOption.Raw)
        else:
            translation_config.set_profanity(speechsdk.ProfanityOption.Masked)

        if self._config.noise_suppression:
            translation_config.set_property(
                speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs,
                "3000",
            )

        seg_timeout = self._config.segmentation_silence_timeout_ms
        translation_config.set_property(
            speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
            str(max(100, min(5000, seg_timeout))),
        )

        audio_config = self._build_input_audio_config()

        self._recognizer = speechsdk.translation.TranslationRecognizer(
            translation_config=translation_config,
            audio_config=audio_config,
        )

        self._recognizer.recognized.connect(self._on_recognized)
        self._recognizer.recognizing.connect(self._on_recognizing)
        self._recognizer.canceled.connect(self._on_canceled)
        self._recognizer.session_started.connect(
            lambda _: logger.debug("Azure session started")
        )
        self._recognizer.session_stopped.connect(
            lambda _: logger.debug("Azure session stopped")
        )

    def _build_input_audio_config(self) -> speechsdk.audio.AudioConfig:
        dev_name = self._config.input_device_name
        dev = self._audio_router.find_device_by_name(dev_name, DeviceDirection.INPUT)

        if dev is None:
            logger.info("Using default microphone for Azure input")
            return speechsdk.audio.AudioConfig(use_default_microphone=True)

        # Use a PushAudioInputStream fed by sounddevice for non-default devices
        stream_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=self._config.sample_rate,
            bits_per_sample=16,
            channels=self._config.channels,
        )
        self._push_input_stream = speechsdk.audio.PushAudioInputStream(stream_format)
        self._start_input_capture(dev.device_id)
        return speechsdk.audio.AudioConfig(stream=self._push_input_stream)

    def _start_input_capture(self, device_id: int) -> None:
        """Capture audio from sounddevice and push to Azure."""
        import sounddevice as sd
        import numpy as np

        gain = max(0.0, self._config.input_gain)

        def callback(indata, frames, time_info, status):
            if status:
                logger.debug("Input capture status: %s", status)
            if self._push_input_stream is not None:
                amplified = indata[:, 0] * gain * 32767
                pcm = np.clip(amplified, -32768, 32767).astype(np.int16).tobytes()
                try:
                    self._push_input_stream.write(pcm)
                except Exception:
                    pass

        try:
            self._capture_stream = sd.InputStream(
                device=device_id,
                channels=self._config.channels,
                samplerate=self._config.sample_rate,
                dtype="float32",
                blocksize=1024,
                callback=callback,
            )
            self._capture_stream.start()
            logger.info("Input capture started (device=%s)", device_id)
        except Exception:
            logger.exception("Failed to start input capture")

    def _close_input_stream(self) -> None:
        if hasattr(self, "_capture_stream") and self._capture_stream is not None:
            try:
                self._capture_stream.stop()
                self._capture_stream.close()
            except Exception:
                pass
            self._capture_stream = None
        if self._push_input_stream is not None:
            try:
                self._push_input_stream.close()
            except Exception:
                pass
            self._push_input_stream = None

    # -- recognition callbacks ---------------------------------------------

    def _on_recognized(self, evt: speechsdk.translation.TranslationRecognitionEventArgs):
        if not self._accept_events:
            return
        if evt.result.reason == speechsdk.ResultReason.TranslatedSpeech:
            source = evt.result.text
            target_lang = self._config.target_language
            translated = evt.result.translations.get(target_lang, "")
            if source.strip() and translated.strip():
                ts = time.monotonic()
                self.on_translated.emit(source, translated, ts)

            if self._config.auto_segmentation_enabled:
                duration_s = evt.result.duration / TICKS_PER_SECOND
                self._evaluate_auto_segmentation(duration_s)

    def _on_recognizing(self, evt: speechsdk.translation.TranslationRecognitionEventArgs):
        if not self._accept_events:
            return
        if evt.result.reason == speechsdk.ResultReason.TranslatingSpeech:
            target_lang = self._config.target_language
            partial = evt.result.translations.get(target_lang, "")
            if partial.strip():
                self.on_recognizing.emit(partial)

    def _on_canceled(self, evt: speechsdk.translation.TranslationRecognitionCanceledEventArgs):
        details = evt.cancellation_details
        if details.reason == speechsdk.CancellationReason.Error:
            msg = f"Azure error: {details.error_details}"
            logger.error(msg)
            self.on_error.emit(msg)
        elif details.reason == speechsdk.CancellationReason.EndOfStream:
            logger.info("Azure: end of stream")
        else:
            logger.warning("Azure recognition canceled: %s", details.reason)

    # -- auto segmentation -------------------------------------------------

    def _evaluate_auto_segmentation(self, duration_s: float) -> None:
        new_timeout = self._auto_seg.feed(duration_s)
        if new_timeout is None:
            return

        avg = sum(self._auto_seg._durations) / len(self._auto_seg._durations)
        self._config.segmentation_silence_timeout_ms = new_timeout
        logger.info(
            "Auto-segmentation: timeout → %d ms  (avg utterance %.1fs)",
            new_timeout, avg,
        )
        self.segmentation_timeout_updated.emit(new_timeout, round(avg, 1))
        self._restart_recognizer()

    def _restart_recognizer(self) -> None:
        """Stop and re-start the recognizer on a background thread so the
        SDK callback thread is not blocked."""
        if self._restarting:
            return
        self._restarting = True

        def _do_restart():
            try:
                if not self._accept_events:
                    return
                self.stop_recognition()
                if not self._accept_events:
                    return
                self.start_recognition()
            finally:
                self._restarting = False

        threading.Thread(target=_do_restart, daemon=True).start()

    # -- synthesis (TTS) ---------------------------------------------------

    def build_synthesizer(self) -> speechsdk.SpeechSynthesizer:
        """Build a synthesizer.

        Always routes TTS audio through ``AudioRouter.write_output()`` so
        that output listeners (e.g. the WebRTC streamer) receive the PCM
        data regardless of which output device is selected.  When no
        explicit output device is configured the system default is used.
        """
        speech_config = speechsdk.SpeechConfig(
            subscription=self._speech_key,
            region=self._speech_region,
        )
        speech_config.speech_synthesis_voice_name = self._config.voice_name
        speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
        )

        out_name = self._config.output_device_name
        sec_name = self._config.secondary_output_device_name
        out_dev = self._audio_router.find_device_by_name(out_name, DeviceDirection.OUTPUT)
        sec_dev = self._audio_router.find_device_by_name(sec_name, DeviceDirection.OUTPUT)

        primary_id = out_dev.device_id if out_dev else None
        secondary_id = sec_dev.device_id if sec_dev else None

        self._audio_router.open_output_streams(
            primary_device_id=primary_id,
            secondary_device_id=secondary_id,
            sample_rate=self._config.sample_rate,
            channels=self._config.channels,
        )

        class _OutputCallback(speechsdk.audio.PushAudioOutputStreamCallback):
            def __init__(self, router: AudioRouter):
                super().__init__()
                self._router = router

            def write(self, audio_buffer: memoryview) -> int:
                data = bytes(audio_buffer)
                self._router.write_output(data)
                return len(data)

            def close(self):
                pass

        callback = _OutputCallback(self._audio_router)
        push_stream = speechsdk.audio.PushAudioOutputStream(callback)
        audio_output = speechsdk.audio.AudioOutputConfig(stream=push_stream)

        self._synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_config,
            audio_config=audio_output,
        )
        return self._synthesizer

    def synthesize_ssml(self, ssml: str) -> speechsdk.SpeechSynthesisResult:
        if self._synthesizer is None:
            self.build_synthesizer()
        return self._synthesizer.speak_ssml_async(ssml).get()

    def build_ssml(self, text: str, rate: float | None = None, pitch: str | None = None) -> str:
        return SSMLBuilder.build(
            text=text,
            voice=self._config.voice_name,
            rate=rate or self._config.speaking_rate,
            pitch=pitch or self._config.pitch,
            volume=self._config.tts_volume,
        )

    # -- voice listing -----------------------------------------------------

    def list_voices(self, locale: str = "") -> list[speechsdk.VoiceInfo]:
        """Retrieve available TTS voices from Azure.

        Uses a short-lived synthesizer with a pull output stream so we do not
        open PortAudio output devices (unlike ``build_synthesizer()``). This
        keeps voice listing working from Settings before translation is started.
        """
        try:
            speech_config = speechsdk.SpeechConfig(
                subscription=self._speech_key,
                region=self._speech_region,
            )
            synth = speechsdk.SpeechSynthesizer(
                speech_config=speech_config,
                audio_config=speechsdk.audio.AudioOutputConfig(
                    stream=speechsdk.audio.PullAudioOutputStream()
                ),
            )
            result = synth.get_voices_async(locale).get()
            if result.reason == speechsdk.ResultReason.VoicesListRetrieved:
                return list(result.voices)
            logger.warning("Failed to list voices: %s", result.reason)
        except Exception:
            logger.exception("Failed to list voices")
        return []

    # -- connection test ---------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        try:
            config = speechsdk.SpeechConfig(
                subscription=self._speech_key,
                region=self._speech_region,
            )
            synth = speechsdk.SpeechSynthesizer(
                speech_config=config,
                audio_config=speechsdk.audio.AudioOutputConfig(
                    stream=speechsdk.audio.PullAudioOutputStream()
                ),
            )
            result = synth.speak_text_async("test").get()
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                return True, "Connection successful"
            return False, f"Unexpected result: {result.reason}"
        except Exception as exc:
            return False, str(exc)

    # -- cleanup -----------------------------------------------------------

    def notify_pipeline_stopping(self) -> None:
        """Called by TranslationPipeline as soon as Stop is requested."""
        self._accept_events = False

    def shutdown(self) -> None:
        self._accept_events = False
        self.stop_recognition()
        self._audio_router.close_output_streams()
        self._synthesizer = None
