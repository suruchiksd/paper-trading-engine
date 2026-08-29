"""Explicit policies for known historical-data imperfections."""

from dataclasses import dataclass
from enum import StrEnum


class DataQualityAction(StrEnum):
    ERROR = "ERROR"
    WARN = "WARN"
    ALLOW = "ALLOW"


@dataclass(frozen=True, slots=True)
class HistoricalDataQualityPolicy:
    """Controls reporting only; historical values are never repaired or fabricated."""

    duplicate_timestamps: DataQualityAction = DataQualityAction.ERROR
    invalid_ohlc: DataQualityAction = DataQualityAction.ERROR
    missing_bars: DataQualityAction = DataQualityAction.ERROR


class HistoricalDataQualityError(ValueError):
    """Raised when historical input violates an ERROR quality policy."""


class HistoricalDataQualityWarning(UserWarning):
    """Warning emitted when historical input violates a WARN quality policy."""

