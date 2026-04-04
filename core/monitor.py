from __future__ import annotations

import threading
import time
from collections import deque

from PyQt6.QtCore import QObject, pyqtSignal

from core.models import TranslationMetrics, Utterance


class Monitor(QObject):
    """Track translation lag, queue depth, and session health."""

    metrics_updated = pyqtSignal(TranslationMetrics)

    WINDOW_SIZE = 20

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._history: deque[Utterance] = deque(maxlen=self.WINDOW_SIZE)
        self._total_utterances = 0
        self._dropped_utterances = 0
        self._azure_errors = 0
        self._session_start: float = 0.0
        self._queue_depth = 0
        self._is_connected = False
        self._last_error: str | None = None
        self._effective_rate: float = 1.0

    # -- session lifecycle -------------------------------------------------

    def start_session(self) -> None:
        with self._lock:
            self._session_start = time.monotonic()
            self._history.clear()
            self._total_utterances = 0
            self._dropped_utterances = 0
            self._azure_errors = 0
            self._is_connected = True
            self._last_error = None

    def end_session(self) -> None:
        with self._lock:
            self._is_connected = False

    # -- event recording ---------------------------------------------------

    def record_utterance(self, utterance: Utterance) -> None:
        with self._lock:
            self._history.append(utterance)
            self._total_utterances += 1

    def record_drop(self) -> None:
        with self._lock:
            self._dropped_utterances += 1

    def record_error(self, message: str) -> None:
        with self._lock:
            self._azure_errors += 1
            self._last_error = message

    def set_queue_depth(self, depth: int) -> None:
        with self._lock:
            self._queue_depth = depth

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self._is_connected = connected

    def set_effective_rate(self, rate: float) -> None:
        with self._lock:
            self._effective_rate = rate

    # -- metrics -----------------------------------------------------------

    def snapshot(self) -> TranslationMetrics:
        with self._lock:
            lags = [u.total_lag_ms for u in self._history if u.total_lag_ms > 0]
            current = lags[-1] if lags else 0
            avg = int(sum(lags) / len(lags)) if lags else 0
            elapsed = (
                time.monotonic() - self._session_start
                if self._session_start > 0
                else 0.0
            )
            m = TranslationMetrics(
                current_lag_ms=current,
                avg_lag_ms=avg,
                queue_depth=self._queue_depth,
                total_utterances=self._total_utterances,
                dropped_utterances=self._dropped_utterances,
                azure_errors=self._azure_errors,
                session_duration_s=elapsed,
                is_connected=self._is_connected,
                last_error=self._last_error,
                effective_rate=self._effective_rate,
            )
        self.metrics_updated.emit(m)
        return m

    @property
    def avg_lag_ms(self) -> int:
        with self._lock:
            lags = [u.total_lag_ms for u in self._history if u.total_lag_ms > 0]
            return int(sum(lags) / len(lags)) if lags else 0
