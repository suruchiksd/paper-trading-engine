"""Replaceable deterministic transaction-cost models."""

from dataclasses import dataclass
from typing import Protocol

from ..models import Side


class TransactionCostModel(Protocol):
    def calculate(self, side: Side, quantity: int, price: float) -> float: ...


@dataclass(frozen=True, slots=True)
class NoTransactionCosts:
    def calculate(self, side: Side, quantity: int, price: float) -> float:
        return 0.0


@dataclass(frozen=True, slots=True)
class BasisPointsTransactionCosts:
    basis_points: float

    def __post_init__(self) -> None:
        if self.basis_points < 0:
            raise ValueError("basis_points cannot be negative")

    def calculate(self, side: Side, quantity: int, price: float) -> float:
        return quantity * price * self.basis_points / 10_000

