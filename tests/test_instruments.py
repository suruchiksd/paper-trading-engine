from pathlib import Path

import pytest

from paper_trading.market_data import (
    AmbiguousInstrumentError,
    CsvInstrumentRepository,
    InstrumentNotFoundError,
)


HEADER = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
    "tick_size,lot_size,instrument_type,segment,exchange\n"
)


def test_resolves_equity_and_index(tmp_path: Path) -> None:
    path = tmp_path / "instruments.csv"
    path.write_text(
        HEADER
        + "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE\n"
        + "256265,1001,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
    )
    repository = CsvInstrumentRepository(path)
    assert repository.resolve("reliance").instrument_token == 738561
    index = repository.resolve("NIFTY 50", segment="indices")
    assert index.segment == "INDICES"
    assert index.lot_size == 0


def test_missing_instrument_has_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "instruments.csv"
    path.write_text(HEADER)
    with pytest.raises(InstrumentNotFoundError, match="MISSING"):
        CsvInstrumentRepository(path).resolve("MISSING")


def test_ambiguous_instrument_has_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "instruments.csv"
    path.write_text(
        HEADER
        + "1,1,DUP,DUP,0,,0,0.05,1,EQ,NSE,NSE\n"
        + "2,2,DUP,DUP,0,,0,0,0,EQ,INDICES,NSE\n"
    )
    with pytest.raises(AmbiguousInstrumentError, match="ambiguous"):
        CsvInstrumentRepository(path).resolve("DUP")

