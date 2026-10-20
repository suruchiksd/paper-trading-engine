"""Exact frozen single-trade replay used only for research/paper parity tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta
from typing import Iterable

from ...models import Candle
from .adapter import (
    GapFadeConfig,
    OpeningWindow,
    PreviousClose,
    apply_entry_geometry,
    evaluate_symbol,
)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    eligible: bool
    rejection_reason: str
    gap_pct: float | None = None
    retention: float | None = None
    entry_timestamp: object | None = None
    entry_price_raw: float | None = None
    entry_price_slipped: float | None = None
    stop: float | None = None
    risk_pct_raw: float | None = None
    quantity: int | None = None
    exit_timestamp: object | None = None
    exit_price_raw: float | None = None
    exit_reason: str | None = None


def replay_frozen_trade(
    config: GapFadeConfig,
    *,
    symbol: str,
    candles: Iterable[Candle],
    previous_close: PreviousClose,
) -> ReplayResult:
    bars = sorted(candles, key=lambda item: item.timestamp)
    if not bars:
        return ReplayResult(False, "NO_BARS")
    trading_date = bars[0].timestamp.date()
    opening = OpeningWindow()
    for candle in bars:
        opening.ingest(candle)
    decision = evaluate_symbol(
        config=config, symbol=symbol, trading_date=trading_date,
        opening=opening.snapshot(symbol, trading_date), previous_close=previous_close,
    )
    if not decision.eligible:
        return ReplayResult(False, decision.rejection_reason, decision.gap_pct, decision.retention)
    entry = next((bar for bar in bars if bar.timestamp.time().replace(tzinfo=None) == time(9, 30)), None)
    if entry is None:
        return ReplayResult(False, "MISSING_OR_INVALID_EXACT_NEXT_BAR",
                            decision.gap_pct, decision.retention)
    apply_entry_geometry(
        decision, config, raw_entry_reference=entry.open,
        available_notional=config.max_gross_exposure,
    )
    if not decision.eligible:
        return ReplayResult(
            False, decision.rejection_reason, decision.gap_pct, decision.retention,
            entry.timestamp, entry.open, stop=decision.stop,
            risk_pct_raw=decision.stop_distance_pct,
        )
    slipped_entry = entry.open * (1.0 - config.slippage_bps / 10_000.0)
    assert decision.stop is not None
    exit_timestamp = None
    exit_raw = None
    exit_reason = None
    for bar in bars:
        clock = bar.timestamp.time().replace(tzinfo=None)
        if bar.timestamp < entry.timestamp or clock >= time(15, 0):
            continue
        if bar.high >= decision.stop:
            exit_timestamp = bar.timestamp + timedelta(minutes=1)
            exit_raw = max(bar.open, decision.stop)
            exit_reason = "STOP"
            break
    if exit_reason is None:
        eod = next((bar for bar in bars if bar.timestamp.time().replace(tzinfo=None) == time(15, 0)), None)
        if eod is None:
            return ReplayResult(False, "MISSING_15_00_FORCED_EXIT_BAR",
                                decision.gap_pct, decision.retention)
        exit_timestamp, exit_raw, exit_reason = eod.timestamp, eod.open, "EOD"
    return ReplayResult(
        True, "", decision.gap_pct, decision.retention,
        entry.timestamp, entry.open, slipped_entry, decision.stop,
        decision.stop_distance_pct, decision.quantity,
        exit_timestamp, exit_raw, exit_reason,
    )
