from pathlib import Path

from paper_trading.config import load_config


def test_example_configuration_loads() -> None:
    project_root = Path(__file__).parents[1]
    config = load_config(project_root / "config")
    assert config.settings.timezone == "Asia/Kolkata"
    assert config.settings.exchange == "NSE"
    assert config.risk.starting_capital == 1_000_000.0
    assert config.risk.maximum_positions == 5
    assert config.historical_data.equities_subdir == "equities"
    assert config.historical_data.quality.invalid_ohlc == "ERROR"
    assert config.paper_broker.intrabar_ambiguity == "CONSERVATIVE"
    assert config.paper_broker.slippage.market_bps == 2.0
    assert config.persistence.enabled
    assert config.persistence.database_path == project_root / "data/state/paper_trading.sqlite"
    assert config.live_market_data.provider == "zerodha"
    assert config.live_market_data.mode == "full"
    assert config.live_market_data.symbols == ("RELIANCE",)
