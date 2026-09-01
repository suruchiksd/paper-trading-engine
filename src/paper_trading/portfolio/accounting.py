"""In-memory weighted-average portfolio accounting driven exclusively by fills."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..models import Fill, PortfolioSnapshot, Position, Side, Trade, TradeIntent


@dataclass(slots=True)
class _PositionCampaign:
    quantity: int
    entry_timestamp: datetime
    entry_fill_ids: list[str] = field(default_factory=list)
    unallocated_entry_costs: float = 0.0
    signal_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    intent: TradeIntent = TradeIntent.INTRADAY


class Portfolio:
    """Cash and position ledger using signed quantity and weighted-average cost."""

    def __init__(self, starting_capital: float) -> None:
        if starting_capital <= 0:
            raise ValueError("starting_capital must be positive")
        self.starting_capital = float(starting_capital)
        self.cash = float(starting_capital)
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[Trade] = []
        self.gross_realized_pnl = 0.0
        self.realized_pnl = 0.0
        self.total_costs = 0.0
        self._campaigns: dict[str, _PositionCampaign] = {}

    @property
    def open_positions(self) -> tuple[Position, ...]:
        return tuple(position for position in self.positions.values() if position.quantity != 0)

    @property
    def market_value(self) -> float:
        return sum((position.last_price or position.average_price) * position.quantity for position in self.open_positions)

    @property
    def equity(self) -> float:
        return self.cash + self.market_value

    @property
    def unrealized_pnl(self) -> float:
        gross = sum(position.unrealized_pnl for position in self.open_positions)
        open_costs = sum(campaign.unallocated_entry_costs for campaign in self._campaigns.values())
        return gross - open_costs

    @property
    def total_pnl(self) -> float:
        return self.equity - self.starting_capital

    def process_fill(self, fill: Fill) -> tuple[Trade, ...]:
        key = fill.instrument.key
        position = self.positions.setdefault(key, Position(fill.instrument, intent=fill.intent))
        old_quantity = position.quantity
        signed_fill = fill.quantity if fill.side is Side.BUY else -fill.quantity
        self.cash -= signed_fill * fill.price + fill.fees
        self.total_costs += fill.fees
        closed: list[Trade] = []

        if old_quantity == 0 or (old_quantity > 0) == (signed_fill > 0):
            self._increase(position, fill, signed_fill)
            return ()

        closing_quantity = min(abs(old_quantity), fill.quantity)
        fill_cost_per_unit = fill.fees / fill.quantity
        campaign = self._campaigns[key]
        entry_cost_per_unit = campaign.unallocated_entry_costs / abs(old_quantity)
        gross = closing_quantity * (fill.price - position.average_price) * (1 if old_quantity > 0 else -1)
        costs = closing_quantity * (entry_cost_per_unit + fill_cost_per_unit)
        net = gross - costs
        trade = Trade(
            instrument=fill.instrument,
            side=Side.BUY if old_quantity > 0 else Side.SELL,
            quantity=closing_quantity,
            entry_price=position.average_price,
            entry_timestamp=campaign.entry_timestamp,
            entry_fill_ids=tuple(campaign.entry_fill_ids),
            signal_id=campaign.signal_id,
            strategy_id=campaign.strategy_id,
            strategy_version=campaign.strategy_version,
            exit_price=fill.price,
            exit_timestamp=fill.timestamp,
            exit_fill_ids=(fill.fill_id,),
            gross_realized_pnl=gross,
            costs=costs,
            realized_pnl=net,
            intent=campaign.intent,
        )
        self.closed_trades.append(trade)
        closed.append(trade)
        self.gross_realized_pnl += gross
        self.realized_pnl += net
        position.realized_pnl += net
        campaign.unallocated_entry_costs -= entry_cost_per_unit * closing_quantity

        remaining = old_quantity + signed_fill
        if remaining == 0:
            position.quantity = 0
            position.average_price = 0.0
            self._campaigns.pop(key)
        elif (remaining > 0) == (old_quantity > 0):
            position.quantity = remaining
            campaign.quantity = remaining
        else:
            position.quantity = remaining
            position.average_price = fill.price
            opening_quantity = abs(remaining)
            self._campaigns[key] = _PositionCampaign(
                quantity=remaining,
                entry_timestamp=fill.timestamp,
                entry_fill_ids=[fill.fill_id],
                unallocated_entry_costs=opening_quantity * fill_cost_per_unit,
                signal_id=fill.signal_id,
                strategy_id=fill.strategy_id,
                strategy_version=fill.strategy_version,
                intent=fill.intent,
            )
            position.intent = fill.intent
        return tuple(closed)

    def _increase(self, position: Position, fill: Fill, signed_fill: int) -> None:
        key = fill.instrument.key
        old_abs = abs(position.quantity)
        new_abs = old_abs + fill.quantity
        position.average_price = (
            position.average_price * old_abs + fill.price * fill.quantity
        ) / new_abs
        position.quantity += signed_fill
        campaign = self._campaigns.get(key)
        if campaign is None:
            self._campaigns[key] = _PositionCampaign(
                quantity=position.quantity,
                entry_timestamp=fill.timestamp,
                entry_fill_ids=[fill.fill_id],
                unallocated_entry_costs=fill.fees,
                signal_id=fill.signal_id,
                strategy_id=fill.strategy_id,
                strategy_version=fill.strategy_version,
                intent=fill.intent,
            )
            position.intent = fill.intent
        else:
            campaign.quantity = position.quantity
            campaign.entry_fill_ids.append(fill.fill_id)
            campaign.unallocated_entry_costs += fill.fees

    def mark(self, instrument_key: str, price: float) -> None:
        if price <= 0:
            raise ValueError("mark price must be positive")
        position = self.positions.get(instrument_key)
        if position is not None:
            position.last_price = price

    def snapshot(self, timestamp: datetime) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            timestamp=timestamp,
            cash=self.cash,
            equity=self.equity,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            positions={position.instrument.key: position.quantity for position in self.open_positions},
            costs=self.total_costs,
        )
