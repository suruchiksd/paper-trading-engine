from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from paper_trading.market_data import (
    DataQualityAction,
    HistoricalAssetClass,
    HistoricalDataQualityPolicy,
    ParquetHistoricalCandleRepository,
)
from paper_trading.market_data.quality import HistoricalDataQualityError, HistoricalDataQualityWarning
from paper_trading.models import Instrument

IST = ZoneInfo("Asia/Kolkata")


def _write_partition(root: Path, symbol: str, year: int, rows: list[dict[str, object]]) -> None:
    path = root / "equities" / f"symbol={symbol}" / f"year={year}" / "candles.parquet"
    path.parent.mkdir(parents=True)
    schema = pa.schema(
        [
            ("timestamp", pa.timestamp("ns", tz="Asia/Kolkata")),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("volume", pa.int64()),
            ("symbol", pa.string()),
            ("instrument_token", pa.int64()),
        ]
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def _row(timestamp: datetime, symbol: str = "RELIANCE", **prices: float) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "open": prices.get("open", 100),
        "high": prices.get("high", 102),
        "low": prices.get("low", 99),
        "close": prices.get("close", 101),
        "volume": 1000,
        "symbol": symbol,
        "instrument_token": 738561,
    }


@pytest.fixture
def reliance() -> Instrument:
    return Instrument("NSE", "RELIANCE", 738561, segment="NSE")


def test_canonical_path_resolution(tmp_path: Path) -> None:
    repository = ParquetHistoricalCandleRepository(tmp_path)
    assert repository.partition_path("RELIANCE", 2025, HistoricalAssetClass.EQUITY) == (
        tmp_path / "equities/symbol=RELIANCE/year=2025/candles.parquet"
    )
    assert repository.partition_path("NIFTY 50", 2025, HistoricalAssetClass.INDEX) == (
        tmp_path / "indices/symbol=NIFTY 50/year=2025/candles.parquet"
    )


def test_load_filters_sorts_converts_and_preserves_timezone(
    tmp_path: Path, reliance: Instrument
) -> None:
    _write_partition(
        tmp_path,
        "RELIANCE",
        2026,
        [
            _row(datetime(2026, 7, 31, 9, 16, tzinfo=IST)),
            _row(datetime(2026, 7, 30, 9, 15, tzinfo=IST)),
            _row(datetime(2026, 7, 31, 9, 15, tzinfo=IST)),
        ],
    )
    candles = ParquetHistoricalCandleRepository(tmp_path).load_candles(
        reliance, date(2026, 7, 31)
    )
    assert [c.timestamp.minute for c in candles] == [15, 16]
    assert candles[0].instrument is reliance
    assert candles[0].interval == "1minute"
    assert candles[0].timestamp.tzinfo is not None
    assert candles[0].timestamp.utcoffset() == IST.utcoffset(candles[0].timestamp)


def test_cross_year_range_loads_each_partition(tmp_path: Path, reliance: Instrument) -> None:
    _write_partition(tmp_path, "RELIANCE", 2025, [_row(datetime(2025, 12, 31, 15, 29, tzinfo=IST))])
    _write_partition(tmp_path, "RELIANCE", 2026, [_row(datetime(2026, 1, 1, 9, 15, tzinfo=IST))])
    policy = HistoricalDataQualityPolicy(missing_bars=DataQualityAction.ALLOW)
    candles = ParquetHistoricalCandleRepository(tmp_path, quality_policy=policy).load_candles(
        reliance, date(2025, 12, 31), date(2026, 1, 1)
    )
    assert [c.timestamp.year for c in candles] == [2025, 2026]


def test_duplicate_policy_errors_by_default(tmp_path: Path, reliance: Instrument) -> None:
    timestamp = datetime(2026, 7, 31, 9, 15, tzinfo=IST)
    _write_partition(tmp_path, "RELIANCE", 2026, [_row(timestamp), _row(timestamp)])
    with pytest.raises(HistoricalDataQualityError, match="duplicate"):
        ParquetHistoricalCandleRepository(tmp_path).load_candles(reliance, timestamp.date())


def test_invalid_ohlc_warn_policy_retains_original_row(
    tmp_path: Path, reliance: Instrument
) -> None:
    timestamp = datetime(2026, 7, 31, 9, 15, tzinfo=IST)
    _write_partition(tmp_path, "RELIANCE", 2026, [_row(timestamp, high=98, close=101)])
    policy = HistoricalDataQualityPolicy(
        invalid_ohlc=DataQualityAction.WARN, missing_bars=DataQualityAction.ALLOW
    )
    with pytest.warns(HistoricalDataQualityWarning, match="invalid OHLC"):
        candles = ParquetHistoricalCandleRepository(tmp_path, quality_policy=policy).load_candles(
            reliance, timestamp.date()
        )
    assert candles[0].high == 98
    assert candles[0].close == 101


def test_missing_bar_policy_detects_but_does_not_fill(tmp_path: Path, reliance: Instrument) -> None:
    _write_partition(
        tmp_path,
        "RELIANCE",
        2026,
        [
            _row(datetime(2026, 7, 31, 9, 15, tzinfo=IST)),
            _row(datetime(2026, 7, 31, 9, 17, tzinfo=IST)),
        ],
    )
    policy = HistoricalDataQualityPolicy(missing_bars=DataQualityAction.WARN)
    with pytest.warns(HistoricalDataQualityWarning, match="does not establish"):
        candles = ParquetHistoricalCandleRepository(tmp_path, quality_policy=policy).load_candles(
            reliance, date(2026, 7, 31)
        )
    assert len(candles) == 2

