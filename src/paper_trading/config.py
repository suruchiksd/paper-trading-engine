"""Typed loading and validation for YAML application configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .market_data.quality import DataQualityAction, HistoricalDataQualityPolicy


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
class AppConfig:
    settings: Settings
    risk: RiskSettings
    historical_data: HistoricalDataSettings


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
    return AppConfig(settings=settings, risk=risk, historical_data=historical)
