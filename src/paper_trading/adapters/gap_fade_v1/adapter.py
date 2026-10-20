"""Frozen Phase-7B signal boundary shared by replay and live paper.

There is intentionally no broker integration in this module.  It implements only
the already-frozen BASE_0930_OPEN_EOD decision and the explicitly requested paper
risk overlay.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import yaml

from ...models import Candle

IST = ZoneInfo("Asia/Kolkata")
STRATEGY_ID = "gap_fade_v1"
STRATEGY_VERSION = "1.0.0"
FROZEN_CONFIG_SHA256 = "594f3ff8964f7b637b6f282d0f5475be4c9a6a4a1a17c46c25da6594a77a2ce4"
FROZEN_STRATEGY_SHA256 = "a54e28b50089567f78fd1b33c12048ee21eedfefb6282c88e5abd131adfc8bc8"


@dataclass(frozen=True, slots=True)
class GapFadeConfig:
    path: Path
    sha256: str
    raw: Mapping[str, object]
    symbols: tuple[str, ...]
    sector_by_symbol: Mapping[str, str]
    gap_minimum: float
    retention_maximum_exclusive: float
    stop_buffer_bps: float
    risk_pct_minimum: float
    risk_pct_maximum: float
    risk_rupees: float
    max_position_notional: float
    max_gross_exposure: float
    max_positions: int
    max_positions_per_sector: int
    max_trades: int
    daily_hard_loss: float
    slippage_bps: float
    decision_grace_seconds: int


def load_gap_fade_config(path: str | Path) -> GapFadeConfig:
    path = Path(path).resolve()
    payload = path.read_bytes()
    raw = yaml.safe_load(payload)
    if raw["strategy"] != {
        "id": STRATEGY_ID,
        "version": STRATEGY_VERSION,
        "mode": "paper_only",
        "status": "FORWARD_OBSERVATION_ONLY_EDGE_NOT_VALIDATED",
    }:
        raise ValueError("gap fade configuration must remain paper-only observation V1")
    frozen = raw["frozen_research"]
    signal = raw["signal"]
    stop = raw["stop"]
    exit_rule = raw["exit"]
    exact = {
        "phase7b_config_sha256": FROZEN_CONFIG_SHA256,
        "selected_strategy_sha256": FROZEN_STRATEGY_SHA256,
        "selected_cell": "BASE_0930_OPEN_EOD",
        "validation_2023": "FAIL",
    }
    if any(frozen.get(key) != value for key, value in exact.items()):
        raise ValueError("frozen Phase-7B identity does not match the approved observation candidate")
    if not (
        signal.get("direction") == "SHORT"
        and float(signal.get("minimum_positive_gap_inclusive")) == 0.015
        and float(signal.get("maximum_retention_exclusive")) == 0.50
        and signal.get("completed_source_bar") == "09:29"
        and signal.get("decision_time") == "09:30"
        and signal.get("context_filter") == "NONE"
        and stop.get("family") == "OPEN_RECLAIM"
        and float(stop.get("buffer_bps")) == 2.0
        and float(stop.get("raw_stop_distance_pct_minimum_inclusive")) == 0.0020
        and float(stop.get("raw_stop_distance_pct_maximum_inclusive")) == 0.0125
        and exit_rule.get("square_off_time") == "15:00"
        and exit_rule.get("profit_target") == "NONE"
    ):
        raise ValueError("frozen Phase-7B alpha parameters were altered")
    manifest = path.parent.parent / str(raw["universe"]["manifest"])
    with manifest.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    symbols = tuple(row["symbol"].strip().upper() for row in rows)
    sectors = {row["symbol"].strip().upper(): row["sector"].strip() for row in rows}
    expected = int(raw["universe"]["expected_eligible_count"])
    if len(symbols) != expected or len(set(symbols)) != expected or set(symbols) != set(sectors):
        raise ValueError("Phase-7B universe manifest count/uniqueness mismatch")
    execution = raw["paper_execution"]
    return GapFadeConfig(
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        raw=raw,
        symbols=symbols,
        sector_by_symbol=sectors,
        gap_minimum=float(signal["minimum_positive_gap_inclusive"]),
        retention_maximum_exclusive=float(signal["maximum_retention_exclusive"]),
        stop_buffer_bps=float(stop["buffer_bps"]),
        risk_pct_minimum=float(stop["raw_stop_distance_pct_minimum_inclusive"]),
        risk_pct_maximum=float(stop["raw_stop_distance_pct_maximum_inclusive"]),
        risk_rupees=float(execution["risk_rupees_per_trade"]),
        max_position_notional=float(execution["maximum_position_notional"]),
        max_gross_exposure=float(execution["maximum_gross_exposure"]),
        max_positions=int(execution["maximum_simultaneous_positions"]),
        max_positions_per_sector=int(execution["maximum_positions_per_sector"]),
        max_trades=int(execution["maximum_new_trades_per_day"]),
        daily_hard_loss=float(execution["daily_hard_loss_rupees"]),
        slippage_bps=float(execution["slippage_bps_per_side"]),
        decision_grace_seconds=int(execution["decision_collection_grace_seconds"]),
    )


def verify_frozen_research_identity(config: GapFadeConfig) -> dict[str, str]:
    """Verify both source config bytes and canonical selected-strategy identity."""
    frozen = config.raw["frozen_research"]
    root = Path(str(frozen["repository"]))
    source_config = root / str(frozen["phase7b_config"])
    selected_path = root / str(frozen["selected_strategy"])
    source_hash = hashlib.sha256(source_config.read_bytes()).hexdigest()
    payload = json.loads(selected_path.read_text(encoding="utf-8"))
    recorded = payload.pop("selected_strategy_sha256")
    canonical = hashlib.sha256(
        (json.dumps(payload, sort_keys=True, default=str) + "\n").encode()
    ).hexdigest()
    if source_hash != FROZEN_CONFIG_SHA256:
        raise ValueError(f"Phase-7B preregistration hash mismatch: {source_hash}")
    if recorded != FROZEN_STRATEGY_SHA256 or canonical != FROZEN_STRATEGY_SHA256:
        raise ValueError("Phase-7B selected strategy canonical hash mismatch")
    if payload["selected_cell"]["cell_id"] != "BASE_0930_OPEN_EOD":
        raise ValueError("Phase-7B selected strategy cell mismatch")
    return {
        "phase7b_config_sha256": source_hash,
        "selected_strategy_sha256": canonical,
        "selected_strategy_file_sha256": hashlib.sha256(selected_path.read_bytes()).hexdigest(),
    }


@dataclass(frozen=True, slots=True)
class PreviousClose:
    value: float
    source_date: date

    def __post_init__(self) -> None:
        if not math.isfinite(self.value) or self.value <= 0:
            raise ValueError("previous close must be positive and finite")


@dataclass(frozen=True, slots=True)
class OpeningSnapshot:
    trading_date: date
    session_open: float
    price_0929: float
    source_end: datetime
    observed_bars: int


class OpeningWindow:
    """Collect exact completed 09:15..09:29 one-minute bars per symbol."""

    def __init__(self) -> None:
        self._bars: dict[str, dict[datetime, Candle]] = {}

    def ingest(self, candle: Candle) -> bool:
        local = candle.timestamp.astimezone(IST)
        clock = local.time().replace(tzinfo=None)
        if candle.interval != "1minute" or not candle.completed or not time(9, 15) <= clock <= time(9, 29):
            return False
        symbol = candle.instrument.tradingsymbol.upper()
        rows = self._bars.setdefault(symbol, {})
        if local in rows:
            return False
        rows[local] = candle
        return True

    def snapshot(self, symbol: str, trading_date: date) -> OpeningSnapshot | None:
        start = datetime.combine(trading_date, time(9, 15), IST)
        expected = tuple(start + timedelta(minutes=offset) for offset in range(15))
        rows = self._bars.get(symbol.upper(), {})
        if any(timestamp not in rows for timestamp in expected):
            return None
        first, last = rows[expected[0]], rows[expected[-1]]
        return OpeningSnapshot(trading_date, first.open, last.close, expected[-1], len(expected))

    def symbols_complete(self, symbols: Iterable[str], trading_date: date) -> bool:
        return all(self.snapshot(symbol, trading_date) is not None for symbol in symbols)


@dataclass(slots=True)
class GapFadeDecision:
    symbol: str
    sector: str
    candidate_timestamp: str
    signal_timestamp: str
    previous_close: float | None = None
    previous_close_source_date: str = ""
    today_open: float | None = None
    gap_pct: float | None = None
    price_0929: float | None = None
    retention: float | None = None
    eligible: bool = False
    accepted: bool = False
    rejection_reason: str = ""
    priority_score: float | None = None
    rank: int | None = None
    entry_reference: float | None = None
    stop: float | None = None
    stop_distance_pct: float | None = None
    quantity: int | None = None
    rms_result: str = "NOT_EVALUATED"
    order_status: str = "NOT_SUBMITTED"
    fill_status: str = "NOT_FILLED"
    order_timestamp: str = ""
    fill_timestamp: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_symbol(
    *,
    config: GapFadeConfig,
    symbol: str,
    trading_date: date,
    opening: OpeningSnapshot | None,
    previous_close: PreviousClose | None,
) -> GapFadeDecision:
    signal_at = datetime.combine(trading_date, time(9, 30), IST).isoformat()
    row = GapFadeDecision(symbol, config.sector_by_symbol[symbol], signal_at, signal_at)
    if previous_close is None or previous_close.source_date >= trading_date:
        row.rejection_reason = "PREVIOUS_VALID_CLOSE_UNAVAILABLE"
        return row
    row.previous_close = previous_close.value
    row.previous_close_source_date = previous_close.source_date.isoformat()
    if opening is None:
        row.rejection_reason = "INCOMPLETE_0915_0929_WINDOW"
        return row
    row.today_open = opening.session_open
    row.price_0929 = opening.price_0929
    gap = opening.session_open / previous_close.value - 1.0
    row.gap_pct = gap
    if not math.isfinite(gap) or gap < config.gap_minimum:
        row.rejection_reason = "POSITIVE_GAP_BELOW_1P5PCT"
        return row
    retention = ((opening.price_0929 / previous_close.value) - 1.0) / gap
    row.retention = retention
    if not math.isfinite(retention) or retention >= config.retention_maximum_exclusive:
        row.rejection_reason = "RETENTION_NOT_BELOW_50PCT"
        return row
    row.eligible = True
    row.priority_score = gap * (1.0 - retention)
    row.rejection_reason = "QUALIFIED_SIGNAL"
    return row


def apply_entry_geometry(
    row: GapFadeDecision,
    config: GapFadeConfig,
    *,
    raw_entry_reference: float | None,
    available_notional: float,
) -> GapFadeDecision:
    if not row.eligible:
        return row
    if raw_entry_reference is None or not math.isfinite(raw_entry_reference) or raw_entry_reference <= 0:
        row.eligible = False
        row.rejection_reason = "NEXT_EXECUTABLE_BID_UNAVAILABLE"
        return row
    assert row.today_open is not None
    stop = row.today_open * (1.0 + config.stop_buffer_bps / 10_000.0)
    row.entry_reference = raw_entry_reference
    row.stop = stop
    if stop <= raw_entry_reference:
        row.eligible = False
        row.rejection_reason = "INVALID_SHORT_STOP_GEOMETRY"
        return row
    raw_risk_pct = (stop - raw_entry_reference) / raw_entry_reference
    row.stop_distance_pct = raw_risk_pct
    if not config.risk_pct_minimum <= raw_risk_pct <= config.risk_pct_maximum:
        row.eligible = False
        row.rejection_reason = "RISK_PCT_OUTSIDE_FROZEN_RANGE"
        return row
    slipped_entry = raw_entry_reference * (1.0 - config.slippage_bps / 10_000.0)
    quantity = position_size(
        risk_rupees=config.risk_rupees,
        entry_price=slipped_entry,
        stop_price=stop,
        maximum_position_notional=config.max_position_notional,
        available_notional=available_notional,
    )
    row.quantity = quantity
    if quantity < 1:
        row.eligible = False
        row.rejection_reason = "CAPITAL_OR_NOTIONAL_LIMIT"
        return row
    row.rejection_reason = "ELIGIBLE_FOR_PORTFOLIO_SELECTION"
    return row


def position_size(
    *,
    risk_rupees: float,
    entry_price: float,
    stop_price: float,
    maximum_position_notional: float,
    available_notional: float,
) -> int:
    risk_per_share = stop_price - entry_price
    if min(risk_rupees, entry_price, stop_price, maximum_position_notional, available_notional) <= 0:
        return 0
    if risk_per_share <= 0:
        return 0
    return max(0, min(
        math.floor(risk_rupees / risk_per_share),
        math.floor(maximum_position_notional / entry_price),
        math.floor(available_notional / entry_price),
    ))


def select_portfolio_candidates(
    rows: Iterable[GapFadeDecision],
    *,
    maximum_positions: int,
    maximum_positions_per_sector: int,
) -> list[GapFadeDecision]:
    ordered = sorted(
        (row for row in rows if row.eligible),
        key=lambda row: (-(row.priority_score or 0.0), row.symbol),
    )
    selected: list[GapFadeDecision] = []
    sector_counts: dict[str, int] = {}
    seen_symbols: set[str] = set()
    for rank, row in enumerate(ordered, start=1):
        row.rank = rank
        if row.symbol in seen_symbols:
            row.rejection_reason = "ONE_TRADE_PER_SYMBOL_SESSION"
            continue
        seen_symbols.add(row.symbol)
        if len(selected) >= maximum_positions:
            row.rejection_reason = "MAXIMUM_DAILY_TRADES"
            continue
        if sector_counts.get(row.sector, 0) >= maximum_positions_per_sector:
            row.rejection_reason = "SECTOR_CONCURRENCY_LIMIT"
            continue
        row.accepted = True
        row.rejection_reason = "SELECTED_FOR_PAPER_ENTRY"
        selected.append(row)
        sector_counts[row.sector] = sector_counts.get(row.sector, 0) + 1
    return selected
