"""Manual live-paper command parsing and safe local order gateway."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Callable, Sequence

from .engine import TradingEngine
from .events import CandleEvent, TickEvent
from .market_data.health import FeedHealthSnapshot, FeedState
from .market_data.quotes import LiveQuoteStore
from .models import Instrument, Order, OrderRequest, OrderType, Side
from .risk import RiskDecision


class ManualCommandError(ValueError):
    pass


class ManualAction(StrEnum):
    HELP = "HELP"
    STATUS = "STATUS"
    QUOTE = "QUOTE"
    BUY = "BUY"
    SELL = "SELL"
    ORDERS = "ORDERS"
    POSITIONS = "POSITIONS"
    TRADES = "TRADES"
    PNL = "PNL"
    CANCEL = "CANCEL"
    HEALTH = "HEALTH"
    QUIT = "QUIT"


@dataclass(frozen=True, slots=True)
class ManualCommand:
    action: ManualAction
    symbol: str | None = None
    quantity: int | None = None
    order_type: OrderType | None = None
    price: float | None = None
    order_id: str | None = None


def parse_manual_command(text: str) -> ManualCommand:
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        raise ManualCommandError(str(exc)) from exc
    if not parts:
        raise ManualCommandError("empty command")
    action_text = parts[0].upper()
    try:
        action = ManualAction(action_text)
    except ValueError as exc:
        raise ManualCommandError(f"unknown command: {parts[0]}") from exc
    if action in {ManualAction.BUY, ManualAction.SELL}:
        if len(parts) not in {4, 5}:
            raise ManualCommandError("usage: buy|sell SYMBOL QUANTITY market|limit|stop [PRICE]")
        symbol = parts[1].upper()
        try:
            quantity = int(parts[2])
        except ValueError as exc:
            raise ManualCommandError("quantity must be an integer") from exc
        if quantity <= 0:
            raise ManualCommandError("quantity must be positive")
        kind = parts[3].lower()
        types = {"market": OrderType.MARKET, "limit": OrderType.LIMIT, "stop": OrderType.STOP_MARKET}
        if kind not in types:
            raise ManualCommandError("order type must be market, limit, or stop")
        order_type = types[kind]
        if order_type is OrderType.MARKET and len(parts) != 4:
            raise ManualCommandError("MARKET orders do not take a price")
        if order_type is not OrderType.MARKET and len(parts) != 5:
            raise ManualCommandError("LIMIT/STOP orders require a price")
        price = None
        if len(parts) == 5:
            try:
                price = float(parts[4])
            except ValueError as exc:
                raise ManualCommandError("price must be numeric") from exc
            if price <= 0:
                raise ManualCommandError("price must be positive")
        return ManualCommand(action, symbol, quantity, order_type, price)
    if action is ManualAction.QUOTE:
        if len(parts) != 2:
            raise ManualCommandError("usage: quote SYMBOL")
        return ManualCommand(action, symbol=parts[1].upper())
    if action is ManualAction.CANCEL:
        if len(parts) != 2:
            raise ManualCommandError("usage: cancel ORDER_ID")
        return ManualCommand(action, order_id=parts[1])
    if len(parts) != 1:
        raise ManualCommandError(f"{parts[0]} takes no arguments")
    return ManualCommand(action)


class LiveOrderGate:
    def __init__(
        self,
        subscribed: Sequence[Instrument],
        quote_store: LiveQuoteStore,
        health_provider: Callable[[], FeedHealthSnapshot],
        clock: Callable[[], datetime],
        max_quote_age_seconds: float,
    ) -> None:
        self.subscribed_keys = {instrument.key for instrument in subscribed}
        self.quote_store = quote_store
        self.health_provider = health_provider
        self.clock = clock
        self.max_quote_age_seconds = max_quote_age_seconds

    def check(self, request: OrderRequest) -> RiskDecision | None:
        if request.instrument.key not in self.subscribed_keys:
            return RiskDecision(False, "SYMBOL_NOT_SUBSCRIBED", 0)
        health = self.health_provider()
        if not health.connected or health.state is FeedState.DISCONNECTED:
            return RiskDecision(False, "LIVE_FEED_DISCONNECTED", 0)
        if health.state is FeedState.STALE:
            return RiskDecision(False, "LIVE_FEED_STALE", 0)
        if health.state is not FeedState.HEALTHY:
            return RiskDecision(False, f"LIVE_FEED_{health.state}", 0)
        quote = self.quote_store.get(request.instrument.key)
        if quote is None:
            return RiskDecision(False, "LIVE_QUOTE_MISSING", 0)
        if (self.clock() - quote.received_timestamp).total_seconds() > self.max_quote_age_seconds:
            return RiskDecision(False, "LIVE_QUOTE_STALE", 0)
        return None


class ManualLivePaperRuntime:
    """Single-owner facade for market events and manual local-paper commands."""

    def __init__(
        self,
        engine: TradingEngine,
        quote_store: LiveQuoteStore,
        instruments: Sequence[Instrument],
        clock: Callable[[], datetime],
    ) -> None:
        self.engine = engine
        self.quote_store = quote_store
        self.instruments = {instrument.tradingsymbol.upper(): instrument for instrument in instruments}
        self.clock = clock

    def handle_market_event(self, event: TickEvent | CandleEvent) -> None:
        if isinstance(event, TickEvent):
            self.quote_store.update(event.tick)
            self.engine.process_live_tick(event.tick)
        else:
            self.engine.process_candle(event.candle)

    def submit(self, command: ManualCommand) -> tuple[RiskDecision, Order]:
        if command.action not in {ManualAction.BUY, ManualAction.SELL}:
            raise ManualCommandError("command is not an order")
        instrument = self.instruments.get(command.symbol or "")
        if instrument is None:
            raise ManualCommandError("SYMBOL_NOT_SUBSCRIBED")
        quote = self.quote_store.get(instrument.key)
        reference = quote.last_price if quote else command.price
        request = OrderRequest(
            instrument=instrument,
            side=Side.BUY if command.action is ManualAction.BUY else Side.SELL,
            order_type=command.order_type or OrderType.MARKET,
            quantity=command.quantity or 0,
            timestamp=self.clock(),
            reference_price=reference,
            limit_price=command.price if command.order_type is OrderType.LIMIT else None,
            stop_price=command.price if command.order_type is OrderType.STOP_MARKET else None,
            metadata={"source": "MANUAL_LIVE_PAPER"},
        )
        return self.engine.submit_order(request)
