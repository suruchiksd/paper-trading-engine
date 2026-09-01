"""Typed loading and validation for YAML application configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from datetime import time
from typing import Any, Mapping

import yaml

from .market_data.quality import DataQualityAction, HistoricalDataQualityPolicy
from .execution.paper import IntrabarAmbiguityPolicy, SlippageConfig


@dataclass(frozen=True, slots=True)
class Settings:
    timezone: str
    exchange: str
    default_candle_interval: str
    database_path: Path
    logging_path: Path
    operating_mode: str


@dataclass(frozen=True, slots=True)
class RiskSettings:
    starting_capital: float
    risk_per_trade_pct: float
    maximum_positions: int
    maximum_daily_loss_pct: float
    maximum_position_exposure_pct: float
    new_entry_cutoff: str
    square_off_time: str


@dataclass(frozen=True, slots=True)
class HistoricalDataSettings:
    root: Path
    equities_subdir: str
    indices_subdir: str
    instrument_master: Path
    quality: HistoricalDataQualityPolicy


@dataclass(frozen=True, slots=True)
class PaperBrokerSettings:
    fill_mode: str
    intrabar_ambiguity: IntrabarAmbiguityPolicy
    slippage: SlippageConfig
    transaction_cost_bps: float


@dataclass(frozen=True, slots=True)
class PersistenceSettings:
    enabled: bool
    database_path: Path


@dataclass(frozen=True, slots=True)
class LiveMarketDataSettings:
    provider: str
    exchange: str
    mode: str
    stale_after_seconds: float
    symbols: tuple[str, ...]
    reconnect_max_tries: int
    reconnect_max_delay: int
    candle_finalize_delay_seconds: float


@dataclass(frozen=True, slots=True)
class LivePaperSettings:
    max_quote_age_seconds: float
    allow_ltp_fallback: bool
    market_slippage_bps: float
    stop_market_slippage_bps: float


@dataclass(frozen=True, slots=True)
class SessionControlsSettings:
    timezone: str
    new_entry_cutoff: time
    intraday_square_off_time: time
    session_end_time: time
    reject_new_entries_after_cutoff: bool
    auto_square_off_intraday: bool
    cancel_open_intraday_orders_at_eod: bool
    auto_export_on_complete: bool
    reports_path: Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    settings: Settings
    risk: RiskSettings
    historical_data: HistoricalDataSettings
    paper_broker: PaperBrokerSettings
    persistence: PersistenceSettings
    live_market_data: LiveMarketDataSettings
    live_paper: LivePaperSettings
    session_controls: SessionControlsSettings


def _read_yaml(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"configuration file not found: {path}")
    with path.open(encoding="utf-8") as stream:
        content = yaml.safe_load(stream)
    if not isinstance(content, dict):
        raise ValueError(f"configuration must be a mapping: {path}")
    return content


def load_config(config_dir: str | Path = "config") -> AppConfig:
    """Load settings.yaml and risk.yaml from *config_dir*."""
    root = Path(config_dir)
    settings_data = _read_yaml(root / "settings.yaml")
    risk_data = _read_yaml(root / "risk.yaml")
    try:
        settings = Settings(
            timezone=str(settings_data["timezone"]),
            exchange=str(settings_data["exchange"]),
            default_candle_interval=str(settings_data["default_candle_interval"]),
            database_path=Path(settings_data["database_path"]),
            logging_path=Path(settings_data["logging_path"]),
            operating_mode=str(settings_data["operating_mode"]),
        )
        historical_data = settings_data["historical_data"]
        quality_data = historical_data.get("quality", {})
        quality = HistoricalDataQualityPolicy(
            duplicate_timestamps=DataQualityAction(
                quality_data.get("duplicate_timestamps", "error").upper()
            ),
            invalid_ohlc=DataQualityAction(quality_data.get("invalid_ohlc", "error").upper()),
            missing_bars=DataQualityAction(quality_data.get("missing_bars", "error").upper()),
        )
        historical = HistoricalDataSettings(
            root=Path(historical_data["root"]),
            equities_subdir=str(historical_data.get("equities_subdir", "equities")),
            indices_subdir=str(historical_data.get("indices_subdir", "indices")),
            instrument_master=Path(historical_data["instrument_master"]),
            quality=quality,
        )
        broker_data = settings_data.get("paper_broker", {})
        slippage_data = broker_data.get("slippage", {})
        paper_broker = PaperBrokerSettings(
            fill_mode=str(broker_data.get("fill_mode", "candle")),
            intrabar_ambiguity=IntrabarAmbiguityPolicy(
                str(broker_data.get("intrabar_ambiguity", "conservative")).upper()
            ),
            slippage=SlippageConfig(
                market_bps=float(slippage_data.get("market_bps", 0)),
                stop_market_bps=float(slippage_data.get("stop_market_bps", 0)),
            ),
            transaction_cost_bps=float(broker_data.get("transaction_cost_bps", 0)),
        )
        if paper_broker.fill_mode != "candle":
            raise ValueError("paper_broker.fill_mode must be 'candle'")
        if paper_broker.transaction_cost_bps < 0:
            raise ValueError("transaction_cost_bps cannot be negative")
        persistence_data = settings_data.get("persistence", {})
        persistence_path = Path(
            persistence_data.get(
                "database_path",
                settings_data.get("database_path", "data/state/paper_trading.sqlite"),
            )
        )
        if not persistence_path.is_absolute():
            persistence_path = root.parent / persistence_path
        persistence = PersistenceSettings(
            enabled=bool(persistence_data.get("enabled", True)),
            database_path=persistence_path,
        )
        live_data = settings_data.get("live_market_data", {})
        live_market_data = LiveMarketDataSettings(
            provider=str(live_data.get("provider", "zerodha")).lower(),
            exchange=str(live_data.get("exchange", "NSE")).upper(),
            mode=str(live_data.get("mode", "full")).lower(),
            stale_after_seconds=float(live_data.get("stale_after_seconds", 10)),
            symbols=tuple(str(symbol) for symbol in live_data.get("symbols", ["RELIANCE"])),
            reconnect_max_tries=int(live_data.get("reconnect_max_tries", 10)),
            reconnect_max_delay=int(live_data.get("reconnect_max_delay", 30)),
            candle_finalize_delay_seconds=float(
                live_data.get("candle_finalize_delay_seconds", 2)
            ),
        )
        if live_market_data.provider != "zerodha":
            raise ValueError("live_market_data.provider must be 'zerodha'")
        if live_market_data.mode not in {"full", "quote", "ltp"}:
            raise ValueError("live_market_data.mode must be full, quote, or ltp")
        if live_market_data.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if not live_market_data.symbols:
            raise ValueError("at least one live symbol is required")
        if live_market_data.reconnect_max_tries < 0 or live_market_data.reconnect_max_delay <= 0:
            raise ValueError("invalid live reconnect settings")
        if live_market_data.candle_finalize_delay_seconds < 0:
            raise ValueError("candle_finalize_delay_seconds cannot be negative")
        live_paper_data = settings_data.get("live_paper", {})
        live_paper = LivePaperSettings(
            max_quote_age_seconds=float(live_paper_data.get("max_quote_age_seconds", 5)),
            allow_ltp_fallback=bool(live_paper_data.get("allow_ltp_fallback", True)),
            market_slippage_bps=float(live_paper_data.get("market_slippage_bps", 2)),
            stop_market_slippage_bps=float(
                live_paper_data.get("stop_market_slippage_bps", 3)
            ),
        )
        if live_paper.max_quote_age_seconds <= 0:
            raise ValueError("live_paper.max_quote_age_seconds must be positive")
        if live_paper.market_slippage_bps < 0 or live_paper.stop_market_slippage_bps < 0:
            raise ValueError("live paper slippage cannot be negative")
        controls_data = settings_data.get("session_controls", {})
        from .session_controls import parse_hhmm
        reports_path = Path(controls_data.get("reports_path", "data/sessions"))
        if not reports_path.is_absolute():
            reports_path = root.parent / reports_path
        session_controls = SessionControlsSettings(
            timezone=str(controls_data.get("timezone", settings.timezone)),
            new_entry_cutoff=parse_hhmm(str(controls_data.get("new_entry_cutoff", "15:00"))),
            intraday_square_off_time=parse_hhmm(
                str(controls_data.get("intraday_square_off_time", "15:15"))
            ),
            session_end_time=parse_hhmm(str(controls_data.get("session_end_time", "15:30"))),
            reject_new_entries_after_cutoff=bool(
                controls_data.get("reject_new_entries_after_cutoff", True)
            ),
            auto_square_off_intraday=bool(
                controls_data.get("auto_square_off_intraday", False)
            ),
            cancel_open_intraday_orders_at_eod=bool(
                controls_data.get("cancel_open_intraday_orders_at_eod", True)
            ),
            auto_export_on_complete=bool(controls_data.get("auto_export_on_complete", False)),
            reports_path=reports_path,
        )
        if not (
            session_controls.new_entry_cutoff
            <= session_controls.intraday_square_off_time
            <= session_controls.session_end_time
        ):
            raise ValueError("session control times must be in chronological order")
        risk = RiskSettings(**risk_data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid configuration in {root}: {exc}") from exc
    if risk.starting_capital <= 0 or risk.maximum_positions <= 0:
        raise ValueError("starting_capital and maximum_positions must be positive")
    for value in (
        risk.risk_per_trade_pct,
        risk.maximum_daily_loss_pct,
        risk.maximum_position_exposure_pct,
    ):
        if not 0 < value <= 100:
            raise ValueError("risk percentages must be in the range (0, 100]")
    return AppConfig(
        settings=settings,
        risk=risk,
        historical_data=historical,
        paper_broker=paper_broker,
        persistence=persistence,
        live_market_data=live_market_data,
        live_paper=live_paper,
        session_controls=session_controls,
    )
