"""Deterministic completed one-minute candles from normalized live ticks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from ..models import Candle, Instrument, Tick


class TickDisposition(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    VOLUME_RESET = "VOLUME_RESET"
    MISSING_TIMESTAMP = "MISSING_TIMESTAMP"


class VolumeRegressionPolicy(StrEnum):
    RESET_BASELINE = "RESET_BASELINE"
    DROP_TICK = "DROP_TICK"


@dataclass(frozen=True, slots=True)
class CandleBuildUpdate:
    disposition: TickDisposition
    completed_candle: Candle | None = None
    missing_minutes: int = 0


@dataclass(slots=True)
class _CandleState:
    instrument: Instrument
    minute: datetime
    open: float
    high: float
    low: float
    close: float
    volume_start: int | None
    last_cumulative_volume: int | None
    last_exchange_timestamp: datetime


class OneMinuteCandleBuilder:
    """Maintains independent symbol state and never fabricates missing candles."""

    def __init__(
        self,
        volume_regression_policy: VolumeRegressionPolicy = VolumeRegressionPolicy.RESET_BASELINE,
    ) -> None:
        self.volume_regression_policy = volume_regression_policy
        self._states: dict[str, _CandleState] = {}
        self._last_finalized: dict[str, datetime] = {}
        self._fingerprints: dict[str, set[tuple[object, ...]]] = {}

    def process(self, tick: Tick) -> CandleBuildUpdate:
        if tick.exchange_timestamp is None:
            return CandleBuildUpdate(TickDisposition.MISSING_TIMESTAMP)
        key = tick.instrument.key
        timestamp = tick.exchange_timestamp
        minute = timestamp.replace(second=0, microsecond=0)
        fingerprint = (
            timestamp,
            tick.last_price,
            tick.volume,
            tick.best_bid,
            tick.best_ask,
        )
        if fingerprint in self._fingerprints.setdefault(key, set()):
            return CandleBuildUpdate(TickDisposition.DUPLICATE)
        finalized = self._last_finalized.get(key)
        state = self._states.get(key)
        if finalized is not None and minute <= finalized:
            return CandleBuildUpdate(TickDisposition.OUT_OF_ORDER)
        if state is not None and timestamp < state.last_exchange_timestamp:
            return CandleBuildUpdate(TickDisposition.OUT_OF_ORDER)

        volume_reset = bool(
            state is not None
            and tick.volume is not None
            and state.last_cumulative_volume is not None
            and tick.volume < state.last_cumulative_volume
        )
        if volume_reset and self.volume_regression_policy is VolumeRegressionPolicy.DROP_TICK:
            return CandleBuildUpdate(TickDisposition.VOLUME_RESET)

        completed: Candle | None = None
        missing_minutes = 0
        if state is None:
            self._states[key] = self._new_state(tick, minute, tick.volume)
            self._fingerprints[key] = {fingerprint}
        elif minute > state.minute:
            completed = self._finalize(state)
            self._last_finalized[key] = state.minute
            missing_minutes = max(0, int((minute - state.minute).total_seconds() // 60) - 1)
            baseline = state.last_cumulative_volume if missing_minutes == 0 else tick.volume
            if volume_reset:
                baseline = tick.volume
            self._states[key] = self._new_state(tick, minute, baseline)
            self._fingerprints[key] = {fingerprint}
        else:
            if volume_reset:
                state.volume_start = tick.volume
            if self._is_regular_session_open_minute(state.minute) and tick.session_open is not None:
                state.open = tick.session_open
                state.high = max(state.high, tick.session_open)
                state.low = min(state.low, tick.session_open)
            state.high = max(state.high, tick.last_price)
            state.low = min(state.low, tick.last_price)
            state.close = tick.last_price
            state.last_exchange_timestamp = timestamp
            state.last_cumulative_volume = tick.volume
            self._fingerprints[key].add(fingerprint)
        disposition = TickDisposition.VOLUME_RESET if volume_reset else TickDisposition.ACCEPTED
        return CandleBuildUpdate(disposition, completed, missing_minutes)

    def finalize_due(self, now: datetime, delay_seconds: float = 0) -> tuple[Candle, ...]:
        """Finalize observed states whose minute has ended; fabricate nothing."""
        due: list[tuple[str, _CandleState]] = []
        for key, state in self._states.items():
            deadline = state.minute + timedelta(minutes=1, seconds=delay_seconds)
            if now >= deadline:
                due.append((key, state))
        candles = []
        for key, state in sorted(due, key=lambda item: (item[1].minute, item[0])):
            candles.append(self._finalize(state))
            self._last_finalized[key] = state.minute
            self._states.pop(key, None)
            self._fingerprints.pop(key, None)
        return tuple(candles)

    def flush(self) -> tuple[Candle, ...]:
        """Finalize every observed in-progress candle during a clean session close."""
        candles = []
        for key, state in sorted(self._states.items(), key=lambda item: (item[1].minute, item[0])):
            candles.append(self._finalize(state))
            self._last_finalized[key] = state.minute
        self._states.clear()
        self._fingerprints.clear()
        return tuple(candles)

    @staticmethod
    def _new_state(tick: Tick, minute: datetime, baseline: int | None) -> _CandleState:
        assert tick.exchange_timestamp is not None
        opening_price = (
            tick.session_open
            if OneMinuteCandleBuilder._is_regular_session_open_minute(minute)
            and tick.session_open is not None
            else tick.last_price
        )
        return _CandleState(
            tick.instrument,
            minute,
            opening_price,
            max(opening_price, tick.last_price),
            min(opening_price, tick.last_price),
            tick.last_price,
            baseline,
            tick.volume,
            tick.exchange_timestamp,
        )

    @staticmethod
    def _is_regular_session_open_minute(minute: datetime) -> bool:
        local = minute.astimezone(ZoneInfo("Asia/Kolkata"))
        return local.hour == 9 and local.minute == 15

    @staticmethod
    def _finalize(state: _CandleState) -> Candle:
        if state.volume_start is None or state.last_cumulative_volume is None:
            volume = 0
        else:
            volume = max(0, state.last_cumulative_volume - state.volume_start)
        return Candle(
            instrument=state.instrument,
            interval="1minute",
            timestamp=state.minute,
            open=state.open,
            high=state.high,
            low=state.low,
            close=state.close,
            volume=volume,
            completed=True,
        )
