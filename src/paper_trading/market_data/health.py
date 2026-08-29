"""Small thread-safe live-feed health model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import Lock
from typing import Callable


class FeedState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class FeedHealthSnapshot:
    state: FeedState
    connected: bool
    last_tick_received_at: datetime | None
    last_tick_exchange_at: datetime | None
    ticks_received: int
    reconnect_count: int
    malformed_ticks: int
    dropped_ticks: int
    duplicate_ticks: int
    out_of_order_ticks: int
    volume_regressions: int
    last_error: str | None


class FeedHealth:
    def __init__(self, stale_after_seconds: float, clock: Callable[[], datetime]) -> None:
        self.stale_after_seconds = stale_after_seconds
        self.clock = clock
        self._lock = Lock()
        self.connected = False
        self.state = FeedState.DISCONNECTED
        self.connected_at: datetime | None = None
        self.last_tick_received_at: datetime | None = None
        self.last_tick_exchange_at: datetime | None = None
        self.ticks_received = 0
        self.reconnect_count = 0
        self.malformed_ticks = 0
        self.dropped_ticks = 0
        self.duplicate_ticks = 0
        self.out_of_order_ticks = 0
        self.volume_regressions = 0
        self.last_error: str | None = None

    def mark_connecting(self) -> None:
        with self._lock:
            self.state = FeedState.CONNECTING

    def mark_connected(self) -> None:
        with self._lock:
            self.connected = True
            self.connected_at = self.clock()
            self.state = FeedState.HEALTHY
            self.last_error = None

    def mark_disconnected(self, reason: str | None = None) -> None:
        with self._lock:
            self.connected = False
            self.state = FeedState.DISCONNECTED
            self.last_error = reason

    def mark_error(self, reason: str) -> None:
        with self._lock:
            self.connected = False
            self.state = FeedState.ERROR
            self.last_error = reason

    def mark_stopped(self) -> None:
        with self._lock:
            self.connected = False
            self.state = FeedState.STOPPED

    def record_tick(self, received_at: datetime, exchange_at: datetime | None) -> None:
        with self._lock:
            self.ticks_received += 1
            self.last_tick_received_at = received_at
            if exchange_at is not None and (
                self.last_tick_exchange_at is None or exchange_at > self.last_tick_exchange_at
            ):
                self.last_tick_exchange_at = exchange_at
            if self.connected:
                self.state = FeedState.HEALTHY

    def record_reconnect(self) -> None:
        with self._lock:
            self.reconnect_count += 1

    def increment(self, field: str) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)

    def snapshot(self) -> FeedHealthSnapshot:
        with self._lock:
            state = self.state
            reference = self.last_tick_received_at or self.connected_at
            if self.connected and reference is not None:
                age = (self.clock() - reference).total_seconds()
                if age > self.stale_after_seconds:
                    state = FeedState.STALE
            return FeedHealthSnapshot(
                state, self.connected, self.last_tick_received_at,
                self.last_tick_exchange_at, self.ticks_received, self.reconnect_count,
                self.malformed_ticks, self.dropped_ticks, self.duplicate_ticks,
                self.out_of_order_ticks, self.volume_regressions, self.last_error,
            )
