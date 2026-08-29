from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from paper_trading.market_data import FeedHealth, FeedState

IST = ZoneInfo("Asia/Kolkata")


def test_health_connection_tick_staleness_and_counters() -> None:
    now = [datetime(2026, 8, 31, 9, 15, tzinfo=IST)]
    health = FeedHealth(10, lambda: now[0])
    assert health.snapshot().state is FeedState.DISCONNECTED
    health.mark_connected()
    assert health.snapshot().state is FeedState.HEALTHY
    health.record_tick(now[0], now[0] - timedelta(milliseconds=100))
    health.record_reconnect()
    health.increment("malformed_ticks")
    health.increment("dropped_ticks")
    now[0] += timedelta(seconds=11)
    snapshot = health.snapshot()
    assert snapshot.state is FeedState.STALE
    assert snapshot.ticks_received == 1
    assert snapshot.reconnect_count == 1
    assert snapshot.malformed_ticks == 1
    assert snapshot.dropped_ticks == 1

