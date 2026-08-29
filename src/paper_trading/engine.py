"""Top-level orchestration boundary for the future event-driven engine."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Iterable, Sequence

from .events import CandleEvent, FillEvent, OrderEvent, RiskEvent, SignalEvent, TickEvent
from .execution import OrderManager
from .market_data.interfaces import MarketDataFeed
from .models import Candle, Order, OrderRequest, Signal, Tick
from .portfolio import Portfolio
from .risk import RiskDecision, RiskManager
from .strategies import BaseStrategy

if TYPE_CHECKING:
    from .storage import SessionRecorder


@dataclass(slots=True)
class TradingEngine:
    """Synchronous composition root for replay and optional paper execution.

    Intended flow:
    market data -> tick/candle -> strategy -> signal -> risk -> order -> broker
    -> fill -> portfolio.

    For every completed candle, already-open orders execute first, fills update the
    portfolio, the close marks positions, and only then do strategies/manual controls
    observe the candle. Consequently their orders cannot execute on that candle.
    """

    strategies: Sequence[BaseStrategy] = field(default_factory=tuple)
    risk_manager: RiskManager | None = None
    order_manager: OrderManager | None = None
    portfolio: Portfolio | None = None
    persistence: SessionRecorder | None = None
    emitted_signals: list[SignalEvent] = field(default_factory=list, init=False)
    risk_events: list[RiskEvent] = field(default_factory=list, init=False)
    order_events: list[OrderEvent] = field(default_factory=list, init=False)
    fill_events: list[FillEvent] = field(default_factory=list, init=False)
    last_candle: Candle | None = field(default=None, init=False)

    def run(self) -> None:
        raise NotImplementedError("engine orchestration is planned for a later phase")

    def replay(
        self,
        feed: MarketDataFeed,
        *,
        after_candle: Callable[[Candle, "TradingEngine"], None] | None = None,
    ) -> int:
        """Synchronously dispatch replay, optionally accepting manual controls."""
        self.emitted_signals.clear()
        self.risk_events.clear()
        self.order_events.clear()
        self.fill_events.clear()
        dispatched = 0
        feed.start()
        try:
            start = getattr(feed, "session_start", None)
            end = getattr(feed, "session_end", None)
            if start is None or end is None:
                return 0
            self._collect(strategy.on_session_start(start) for strategy in self.strategies)
            for event in feed.events():
                if isinstance(event, CandleEvent):
                    dispatched += 1
                    self.process_candle(event.candle)
                    if after_candle is not None:
                        after_candle(event.candle, self)
            self._collect(strategy.on_session_end(end) for strategy in self.strategies)
            return dispatched
        finally:
            feed.stop()

    def process_candle(self, candle: Candle) -> None:
        """Apply execution, accounting, marks, then expose a completed candle."""
        fills = ()
        trades = []
        if self.order_manager is not None:
            fills = self.order_manager.process_candle(candle)
            for fill in fills:
                self.fill_events.append(FillEvent(fill))
                if self.portfolio is not None:
                    trades.extend(self.portfolio.process_fill(fill))
        if self.portfolio is not None:
            self.portfolio.mark(candle.instrument.key, candle.close)
        if self.persistence is not None and self.portfolio is not None:
            if fills:
                assert self.order_manager is not None
                self.persistence.record_execution(
                    self.order_manager.orders.values(), fills, trades, self.portfolio, candle.timestamp
                )
            elif self.portfolio.open_positions:
                self.persistence.checkpoint_positions(self.portfolio, candle.timestamp)
        for fill in fills:
            self._collect(strategy.on_fill(fill) for strategy in self.strategies)
        self.last_candle = candle
        self._collect(strategy.on_candle(candle) for strategy in self.strategies)

    def process_tick(self, tick: Tick) -> None:
        """Dispatch a normalized live tick without invoking execution components."""
        self._collect(strategy.on_tick(tick) for strategy in self.strategies)

    def dispatch_market_event(self, event: TickEvent | CandleEvent) -> None:
        if isinstance(event, TickEvent):
            self.process_tick(event.tick)
        else:
            self.process_candle(event.candle)

    def submit_order(self, request: OrderRequest) -> tuple[RiskDecision, Order]:
        """Submit manual/synthetic intent through the same risk and OMS path."""
        if self.risk_manager is None or self.order_manager is None or self.portfolio is None:
            raise RuntimeError("risk_manager, order_manager, and portfolio are required")
        decision = self.risk_manager.evaluate(
            request, self.portfolio, self.order_manager.broker.active_orders()
        )
        self.risk_events.append(
            RiskEvent(request.timestamp, decision.approved, decision.reason, request.signal_id)
        )
        if decision.approved:
            approved = replace(request, quantity=decision.approved_quantity)
            order = self.order_manager.submit(approved)
        else:
            order = self.order_manager.register_rejection(request, decision.reason)
        self.order_events.append(OrderEvent(order))
        if self.persistence is not None:
            self.persistence.record_order_decision(order, decision, request.timestamp)
        return decision, order

    def cancel_order(self, order_id: str, timestamp: datetime, reason: str = "cancelled by user") -> Order:
        if self.order_manager is None:
            raise RuntimeError("order_manager is required")
        order = self.order_manager.cancel(order_id, timestamp, reason)
        if self.persistence is not None:
            self.persistence.record_order_update(order, timestamp)
        return order

    def _collect(self, groups: Iterable[Iterable[Signal]]) -> None:
        for signals in groups:
            for signal in signals:
                self.emitted_signals.append(SignalEvent(signal))
