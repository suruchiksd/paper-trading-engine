"""Read-only adapter for canonical yearly historical Parquet partitions."""

from __future__ import annotations

import warnings
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from ..models import Candle, Instrument
from .quality import (
    DataQualityAction,
    HistoricalDataQualityError,
    HistoricalDataQualityPolicy,
    HistoricalDataQualityWarning,
)

IST = ZoneInfo("Asia/Kolkata")
REQUIRED_COLUMNS = {
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "symbol",
    "instrument_token",
}


class HistoricalAssetClass(StrEnum):
    EQUITY = "equity"
    INDEX = "index"


class ParquetHistoricalCandleRepository:
    def __init__(
        self,
        root: str | Path,
        *,
        equities_subdir: str = "equities",
        indices_subdir: str = "indices",
        quality_policy: HistoricalDataQualityPolicy | None = None,
    ) -> None:
        self.root = Path(root)
        self.equities_subdir = equities_subdir
        self.indices_subdir = indices_subdir
        self.quality_policy = quality_policy or HistoricalDataQualityPolicy()

    def partition_path(
        self, symbol: str, year: int, asset_class: HistoricalAssetClass
    ) -> Path:
        subdir = self.equities_subdir if asset_class is HistoricalAssetClass.EQUITY else self.indices_subdir
        return self.root / subdir / f"symbol={symbol}" / f"year={year}" / "candles.parquet"

    def load_candles(
        self, instrument: Instrument, start_date: date, end_date: date | None = None
    ) -> list[Candle]:
        end = end_date or start_date
        if end < start_date:
            raise ValueError("end_date cannot precede start_date")
        asset_class = (
            HistoricalAssetClass.INDEX
            if (instrument.segment or "").upper() == "INDICES"
            else HistoricalAssetClass.EQUITY
        )
        rows: list[dict[str, object]] = []
        for year in range(start_date.year, end.year + 1):
            path = self.partition_path(instrument.tradingsymbol, year, asset_class)
            if not path.is_file():
                raise FileNotFoundError(f"historical partition not found: {path}")
            table = pq.ParquetFile(path).read(columns=sorted(REQUIRED_COLUMNS))
            missing = REQUIRED_COLUMNS.difference(table.column_names)
            if missing:
                raise ValueError(f"historical partition {path} lacks columns: {sorted(missing)}")
            rows.extend(table.to_pylist())

        selected: list[dict[str, object]] = []
        for row in rows:
            timestamp = row["timestamp"]
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
                raise ValueError("historical timestamps must be timezone-aware")
            local_timestamp = timestamp.astimezone(IST)
            if start_date <= local_timestamp.date() <= end:
                row["timestamp"] = local_timestamp
                selected.append(row)
        selected.sort(key=lambda row: row["timestamp"])
        self._validate_rows(selected, instrument, start_date, end)
        return [self._to_candle(row, instrument) for row in selected]

    def _validate_rows(
        self, rows: list[dict[str, object]], instrument: Instrument, start: date, end: date
    ) -> None:
        for row in rows:
            if row["symbol"] != instrument.tradingsymbol:
                raise ValueError(
                    f"symbol mismatch: requested {instrument.tradingsymbol!r}, found {row['symbol']!r}"
                )
            token = row["instrument_token"]
            if instrument.instrument_token is not None and int(token) != instrument.instrument_token:
                raise ValueError(
                    f"instrument token mismatch for {instrument.tradingsymbol}: {token}"
                )
            timestamp = row["timestamp"]
            if not start <= timestamp.date() <= end:  # type: ignore[union-attr]
                raise ValueError(f"timestamp outside requested date boundaries: {timestamp}")

        timestamps = [row["timestamp"] for row in rows]
        duplicate_count = len(timestamps) - len(set(timestamps))
        if duplicate_count:
            self._handle_quality(
                self.quality_policy.duplicate_timestamps,
                f"found {duplicate_count} duplicate timestamp(s) for {instrument.tradingsymbol}",
            )

        invalid_count = sum(not self._valid_ohlc(row) for row in rows)
        if invalid_count:
            self._handle_quality(
                self.quality_policy.invalid_ohlc,
                f"found {invalid_count} invalid OHLC row(s) for {instrument.tradingsymbol}",
            )

        gaps = sum(
            current.date() == previous.date() and current - previous > timedelta(minutes=1)
            for previous, current in zip(timestamps, timestamps[1:])
        )
        if gaps:
            self._handle_quality(
                self.quality_policy.missing_bars,
                f"found {gaps} intra-day timestamp gap(s) for {instrument.tradingsymbol}; "
                "this does not establish exchange-session completeness",
            )

    @staticmethod
    def _valid_ohlc(row: dict[str, object]) -> bool:
        open_, high, low, close = (float(row[name]) for name in ("open", "high", "low", "close"))
        return min(open_, high, low, close) > 0 and high >= max(open_, low, close) and low <= min(open_, high, close)

    def _to_candle(self, row: dict[str, object], instrument: Instrument) -> Candle:
        return Candle(
            instrument=instrument,
            interval="1minute",
            timestamp=row["timestamp"],  # type: ignore[arg-type]
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
            completed=True,
            validate_ohlc=self.quality_policy.invalid_ohlc is DataQualityAction.ERROR,
        )

    @staticmethod
    def _handle_quality(action: DataQualityAction, message: str) -> None:
        if action is DataQualityAction.ERROR:
            raise HistoricalDataQualityError(message)
        if action is DataQualityAction.WARN:
            warnings.warn(message, HistoricalDataQualityWarning, stacklevel=3)

