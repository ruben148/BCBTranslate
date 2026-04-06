from __future__ import annotations

import logging
import queue
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal

from core.audio_router import AudioRouter
from core.azure_wrapper import AzureTranslationService
from core.config_manager import ConfigManager
from core.models import AppConfig, DeviceDirection, TranslationMetrics, Utterance
from core.monitor import Monitor
from utils.transcript_writer import TranscriptWriter

logger = logging.getLogger(__name__)


class TranslationPipeline(QObject):
    """Orchestrate the full STT → queue → TTS translation flow."""

    # GUI-facing signals
    utterance_complete = pyqtSignal(str, str, int)   # source, translated, lag_ms
    partial_result = pyqtSignal(str)                  # interim translated text
    status_changed = pyqtSignal(str)                  # status message for log
    error_occurred = pyqtSignal(str)
    metrics_updated = pyqtSignal(TranslationMetrics)
    connection_changed = pyqtSignal(bool)             # connected/disconnected
    segmentation_updated = pyqtSignal(int, float)     # timeout_ms, avg_duration_s

    def __init__(
        self,
        config_manager: ConfigManager,
        audio_router: AudioRouter,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._cfg = config_manager
        self._audio_router = audio_router
        self._monitor = Monitor(self)
        self._monitor.metrics_updated.connect(self.metrics_updated.emit)

        self._azure: AzureTranslationService | None = None
        self._tts_queue: queue.Queue[Utterance | None] = queue.Queue()
        self._tts_thread: threading.Thread | None = None
        self._transcript: TranscriptWriter | None = None
        self._is_running = False

        self._reconnect_attempts = 0
        self._reconnect_lock = threading.Lock()

    # -- public api --------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def monitor(self) -> Monitor:
        return self._monitor

    def start(self) -> None:
        if self._is_running:
            return

        cfg = self._cfg.config
        key = self._cfg.azure_speech_key()
        region = self._cfg.azure_speech_region()
        if not key or not region:
            self.error_occurred.emit("Azure credentials are missing. Check .env file.")
            return

        self._azure = AzureTranslationService(
            speech_key=key,
            speech_region=region,
            config=cfg,
            audio_router=self._audio_router,
            parent=self,
        )
        self._azure.on_translated.connect(self._on_translated)
        self._azure.on_recognizing.connect(self.partial_result.emit)
        self._azure.on_error.connect(self._on_azure_error)
        self._azure.on_connected.connect(self._on_azure_connected)
        self._azure.on_disconnected.connect(self._on_azure_disconnected)
        self._azure.segmentation_timeout_updated.connect(
            self._on_segmentation_timeout_updated
        )

        self._azure.build_synthesizer()

        # Transcript writer
        if cfg.save_transcripts and cfg.transcript_directory:
            try:
                self._transcript = TranscriptWriter(
                    directory=cfg.transcript_directory,
                    source_language=cfg.source_language,
                    target_language=cfg.target_language,
                    voice=cfg.voice_name,
                    rate=cfg.speaking_rate,
                    pitch=cfg.pitch,
                )
                self.status_changed.emit(
                    f"Transcript: {self._transcript.path}"
                )
            except Exception:
                logger.exception("Failed to create transcript writer")

        # TTS worker thread
        self._tts_queue = queue.Queue()
        self._tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self._tts_thread.start()

        self._is_running = True
        # Start Azure recognition (callbacks may arrive immediately)
        self._azure.start_recognition()
        self._monitor.start_session()
        self._reconnect_attempts = 0

        self.status_changed.emit("Translation started")
        self.connection_changed.emit(True)

    def stop(self) -> None:
        if not self._is_running:
            return

        self._is_running = False
        self.status_changed.emit("Stopping translation...")

        az = self._azure
        if az:
            # Block SDK handlers and auto-segmentation restarts before stopping,
            # so queued Qt events cannot enqueue TTS after this point.
            az.notify_pipeline_stopping()
            az.stop_recognition()

        # Drain TTS queue (utterances already queued are still synthesized)
        self._tts_queue.put(None)
        if self._tts_thread and self._tts_thread.is_alive():
            self._tts_thread.join(timeout=5)

        # Close transcript
        if self._transcript:
            self._transcript.close(avg_lag_ms=self._monitor.avg_lag_ms)
            self._transcript = None

        self._monitor.end_session()

        if az:
            self._disconnect_azure_signals(az)
            az.shutdown()
            self._azure = None

        self.status_changed.emit("Translation stopped")
        self.connection_changed.emit(False)

    def _disconnect_azure_signals(self, az: AzureTranslationService) -> None:
        try:
            az.on_translated.disconnect(self._on_translated)
            az.on_recognizing.disconnect(self.partial_result.emit)
            az.on_error.disconnect(self._on_azure_error)
            az.on_connected.disconnect(self._on_azure_connected)
            az.on_disconnected.disconnect(self._on_azure_disconnected)
            az.segmentation_timeout_updated.disconnect(
                self._on_segmentation_timeout_updated
            )
        except TypeError:
            # No matching connection (e.g. partial teardown)
            pass

    def _on_azure_connected(self) -> None:
        self.connection_changed.emit(True)

    def _on_azure_disconnected(self) -> None:
        self.connection_changed.emit(False)

    def _on_segmentation_timeout_updated(self, timeout_ms: int, avg_duration: float) -> None:
        self.segmentation_updated.emit(timeout_ms, avg_duration)

    # -- config hot-reload -------------------------------------------------

    def update_tts_settings(self) -> None:
        """Called when speed/pitch/voice are changed mid-session.
        The next utterance will use the new settings automatically since
        SSML is built per-utterance from current config values."""
        pass

    def apply_input_device_change(self) -> None:
        """Rebind the capture device used for Azure recognition (no app restart).

        Runs the SDK switch on a worker thread so the GUI stays responsive.
        """
        az = self._azure
        if not self._is_running or az is None:
            return

        def _switch() -> None:
            try:
                az.switch_input_device()
                self.status_changed.emit("Input device changed")
            except Exception as exc:
                logger.exception("Input device switch failed")
                self.error_occurred.emit(f"Could not switch input device: {exc}")

        threading.Thread(
            target=_switch,
            daemon=True,
            name="bcb-input-switch",
        ).start()

    def apply_output_device_change(self) -> None:
        """Rebind TTS playback device(s) without restarting translation."""
        if not self._is_running:
            return
        cfg = self._cfg.config
        out_dev = self._audio_router.find_device_by_name(
            cfg.output_device_name, DeviceDirection.OUTPUT
        )
        sec_dev = self._audio_router.find_device_by_name(
            cfg.secondary_output_device_name, DeviceDirection.OUTPUT
        )
        primary_id = out_dev.device_id if out_dev else None
        secondary_id = sec_dev.device_id if sec_dev else None
        try:
            self._audio_router.switch_output_streams(
                primary_device_id=primary_id,
                secondary_device_id=secondary_id,
                sample_rate=cfg.sample_rate,
                channels=cfg.channels,
            )
            self.status_changed.emit("Output device changed")
        except Exception as exc:
            logger.exception("Output device switch failed")
            self.error_occurred.emit(f"Could not switch output device: {exc}")

    def apply_segmentation_mode_change(self) -> None:
        """Rebuild the Azure recognizer so segmentation properties take effect."""
        az = self._azure
        if not self._is_running or az is None:
            return

        def _restart() -> None:
            try:
                az.restart_recognition()
                self.status_changed.emit("Segmentation settings applied")
            except Exception as exc:
                logger.exception("Segmentation restart failed")
                self.error_occurred.emit(f"Could not apply segmentation settings: {exc}")

        threading.Thread(
            target=_restart,
            daemon=True,
            name="bcb-segmentation-restart",
        ).start()

    # -- internal: translation callback ------------------------------------

    def _on_translated(self, source: str, translated: str, timestamp: float) -> None:
        if not self._is_running:
            return
        utterance = Utterance(
            text=translated,
            source_text=source,
            recognized_at=timestamp,
            queued_at=time.monotonic(),
        )

        cfg = self._cfg.config
        depth = self._tts_queue.qsize()

        # Handle overflow
        if depth >= cfg.max_tts_queue_size:
            if cfg.drop_oldest_on_overflow:
                try:
                    dropped = self._tts_queue.get_nowait()
                    self._monitor.record_drop()
                    self.status_changed.emit(
                        f"Dropped oldest utterance (queue full at {depth})"
                    )
                except queue.Empty:
                    pass

        self._tts_queue.put(utterance)
        self._monitor.set_queue_depth(self._tts_queue.qsize())

    # -- internal: TTS worker ----------------------------------------------

    def _tts_worker(self) -> None:
        logger.info("TTS worker started")
        while True:
            item = self._tts_queue.get()
            if item is None:
                break

            utterance: Utterance = item
            utterance.synthesis_started_at = time.monotonic()

            cfg = self._cfg.config
            effective_rate = cfg.speaking_rate

            # Adaptive rate
            if cfg.adaptive_rate_enabled:
                depth = self._tts_queue.qsize()
                if depth >= cfg.tts_queue_warning_threshold:
                    excess = depth - cfg.tts_queue_warning_threshold
                    effective_rate = cfg.speaking_rate + excess * 0.1
                    effective_rate = max(0.5, min(effective_rate, 2.5))
                    self._monitor.set_effective_rate(effective_rate)
                else:
                    self._monitor.set_effective_rate(cfg.speaking_rate)

            try:
                ssml = self._azure.build_ssml(
                    text=utterance.text,
                    rate=effective_rate,
                )
                result = self._azure.synthesize_ssml(ssml)
                utterance.synthesis_done_at = time.monotonic()

                self._monitor.record_utterance(utterance)
                self._monitor.set_queue_depth(self._tts_queue.qsize())

                lag = utterance.total_lag_ms
                self.utterance_complete.emit(
                    utterance.source_text, utterance.text, lag
                )

                if self._transcript:
                    self._transcript.write(
                        utterance.source_text, utterance.text, lag
                    )

            except Exception as exc:
                logger.exception("TTS synthesis failed")
                self._monitor.record_error(str(exc))
                self.error_occurred.emit(f"TTS error: {exc}")

            self._tts_queue.task_done()

        logger.info("TTS worker stopped")

    # -- internal: error handling & reconnect ------------------------------

    def _on_azure_error(self, message: str) -> None:
        if not self._is_running:
            return
        self._monitor.record_error(message)
        self.error_occurred.emit(message)

        if "AuthenticationFailure" in message or "401" in message:
            self.status_changed.emit("Authentication failed — check Azure credentials")
            return

        self._attempt_reconnect()

    def _attempt_reconnect(self) -> None:
        with self._reconnect_lock:
            cfg = self._cfg.config
            if self._reconnect_attempts >= cfg.reconnect_attempts:
                self.error_occurred.emit(
                    f"Reconnection failed after {cfg.reconnect_attempts} attempts"
                )
                self.stop()
                return

            self._reconnect_attempts += 1
            delay = cfg.reconnect_delay_seconds * (2 ** (self._reconnect_attempts - 1))
            self.status_changed.emit(
                f"Reconnecting (attempt {self._reconnect_attempts}/{cfg.reconnect_attempts}) "
                f"in {delay}s..."
            )

        def _reconnect():
            time.sleep(delay)
            if not self._is_running:
                return
            try:
                if self._azure:
                    self._azure.stop_recognition()
                    self._azure.start_recognition()
                    self._reconnect_attempts = 0
                    self.status_changed.emit("Reconnected successfully")
            except Exception as exc:
                logger.exception("Reconnect failed")
                self._on_azure_error(str(exc))

        threading.Thread(target=_reconnect, daemon=True).start()
