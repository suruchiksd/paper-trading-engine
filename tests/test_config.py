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
