"""Broker boundary shared by simulated and future external execution."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

from ..models import Candle, Fill, Order


@dataclass(frozen=True, slots=True)
class BrokerUpdate:
    fills: tuple[Fill, ...] = ()
    cancelled_order_ids: tuple[str, ...] = ()


class Broker(Protocol):
    def submit_order(self, order: Order) -> None: ...

    def restore_order(self, order: Order) -> None: ...

    def cancel_order(self, order_id: str) -> None: ...

    def process_candle(self, candle: Candle) -> BrokerUpdate: ...

    def active_orders(self) -> Sequence[Order]: ...
