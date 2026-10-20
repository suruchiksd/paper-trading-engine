"""Current NSE/Zerodha cash-intraday cost schedule for paper accounting."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from ..models import Side

_CRORE = Decimal("10000000")
_PAISE = Decimal("0.01")
_RUPEE = Decimal("1")


def _d(value: float | int | str | Decimal) -> Decimal:
    return Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class ZerodhaCashCostBreakdown:
    brokerage: float
    stt: float
    nse_transaction_charge: float
    sebi_charge: float
    stamp_duty: float
    gst: float
    ipft: float

    @property
    def statutory_broker_cost(self) -> float:
        return round(sum((self.brokerage, self.stt, self.nse_transaction_charge,
                          self.sebi_charge, self.stamp_duty, self.gst, self.ipft)), 2)


class ZerodhaNseCashIntradayCosts:
    """Zerodha/NSE intraday schedule effective 2026-03-01."""

    brokerage_rate = Decimal("0.0003")
    brokerage_cap = Decimal("20")
    stt_sell_rate = Decimal("0.00025")
    nse_per_crore = Decimal("306.99")
    sebi_per_crore = Decimal("10")
    stamp_buy_rate = Decimal("0.00003")
    gst_rate = Decimal("0.18")
    ipft_per_crore = Decimal("0.01")

    def calculate(self, side: Side, quantity: int, price: float) -> float:
        value = _d(quantity) * _d(price)
        brokerage = _money(min(value * self.brokerage_rate, self.brokerage_cap))
        stt = ((value * self.stt_sell_rate).quantize(_RUPEE, rounding=ROUND_HALF_UP)
               if side is Side.SELL else Decimal("0"))
        exchange = _money(value * self.nse_per_crore / _CRORE)
        sebi = _money(value * self.sebi_per_crore / _CRORE)
        stamp = _money(value * self.stamp_buy_rate) if side is Side.BUY else Decimal("0")
        ipft = _money(value * self.ipft_per_crore / _CRORE)
        gst = _money((brokerage + exchange + sebi + ipft) * self.gst_rate)
        return float(_money(brokerage + stt + exchange + sebi + stamp + gst + ipft))

    def round_trip(self, *, side: str, quantity: int, entry_price: float,
                   exit_price: float) -> ZerodhaCashCostBreakdown:
        entry = _d(quantity) * _d(entry_price)
        exit_ = _d(quantity) * _d(exit_price)
        buy, sell = (entry, exit_) if side == "LONG" else (exit_, entry)
        turnover = buy + sell
        brokerage = _money(min(entry * self.brokerage_rate, self.brokerage_cap)
                           + min(exit_ * self.brokerage_rate, self.brokerage_cap))
        stt = (sell * self.stt_sell_rate).quantize(_RUPEE, rounding=ROUND_HALF_UP)
        exchange = _money(turnover * self.nse_per_crore / _CRORE)
        sebi = _money(turnover * self.sebi_per_crore / _CRORE)
        stamp = _money(buy * self.stamp_buy_rate)
        ipft = _money(turnover * self.ipft_per_crore / _CRORE)
        gst = _money((brokerage + exchange + sebi + ipft) * self.gst_rate)
        return ZerodhaCashCostBreakdown(float(brokerage), float(stt), float(exchange),
                                        float(sebi), float(stamp), float(gst), float(ipft))
