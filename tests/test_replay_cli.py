from datetime import datetime
from pathlib import Path
from subprocess import run
from sys import executable
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq


def test_replay_script_runs_against_fixture(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    parquet_path = data_root / "equities/symbol=RELIANCE/year=2026/candles.parquet"
    parquet_path.parent.mkdir(parents=True)
    rows = [
        {
            "timestamp": datetime(2026, 7, 31, 9, minute, tzinfo=ZoneInfo("Asia/Kolkata")),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 100,
            "symbol": "RELIANCE",
            "instrument_token": 738561,
        }
        for minute in (15, 16)
    ]
    pq.write_table(pa.Table.from_pylist(rows), parquet_path)
    master = data_root / "reference/instruments_nse.csv"
    master.parent.mkdir(parents=True)
    master.write_text(
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
        "tick_size,lot_size,instrument_type,segment,exchange\n"
        "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE\n"
    )
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text(
        f"""timezone: Asia/Kolkata
exchange: NSE
default_candle_interval: 1minute
database_path: state.db
logging_path: replay.log
operating_mode: historical_replay
historical_data:
  root: {data_root}
  equities_subdir: equities
  indices_subdir: indices
  instrument_master: {master}
  quality:
    duplicate_timestamps: error
    invalid_ohlc: error
    missing_bars: error
"""
    )
    (config / "risk.yaml").write_text(
        """starting_capital: 1000000
risk_per_trade_pct: 1
maximum_positions: 5
maximum_daily_loss_pct: 3
maximum_position_exposure_pct: 20
new_entry_cutoff: '15:00'
square_off_time: '15:20'
"""
    )

    project_root = Path(__file__).parents[1]
    result = run(
        [
            executable,
            str(project_root / "scripts/run_replay.py"),
            "--symbol",
            "RELIANCE",
            "--date",
            "2026-07-31",
            "--config-dir",
            str(config),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Candles loaded: 2" in result.stdout
    assert "First: 2026-07-31T09:15:00+05:30" in result.stdout
    assert "Last: 2026-07-31T09:16:00+05:30" in result.stdout
    assert "no order/fill simulation" in result.stdout
