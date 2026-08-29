"""Read-only instrument-master access."""

from __future__ import annotations

import csv
from pathlib import Path

from ..models import Instrument


class InstrumentNotFoundError(LookupError):
    pass


class AmbiguousInstrumentError(LookupError):
    pass


class CsvInstrumentRepository:
    """Resolve instruments from a Zerodha-format CSV without modifying it."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def resolve(self, tradingsymbol: str, *, segment: str | None = None) -> Instrument:
        wanted_symbol = tradingsymbol.strip().upper()
        wanted_segment = segment.strip().upper() if segment else None
        matches: list[dict[str, str]] = []
        try:
            with self.path.open(newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    if row.get("tradingsymbol", "").strip().upper() != wanted_symbol:
                        continue
                    if wanted_segment and row.get("segment", "").strip().upper() != wanted_segment:
                        continue
                    matches.append(row)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"instrument master not found: {self.path}") from exc

        qualifier = f" in segment {wanted_segment}" if wanted_segment else ""
        if not matches:
            raise InstrumentNotFoundError(f"instrument {tradingsymbol!r}{qualifier} was not found")
        if len(matches) > 1:
            segments = sorted({row.get("segment", "") for row in matches})
            raise AmbiguousInstrumentError(
                f"instrument {tradingsymbol!r} is ambiguous across records/segments: {segments}"
            )
        row = matches[0]
        try:
            return Instrument(
                exchange=row["exchange"],
                tradingsymbol=row["tradingsymbol"],
                instrument_token=int(row["instrument_token"]),
                instrument_type=row.get("instrument_type", "EQ") or "EQ",
                tick_size=float(row.get("tick_size", 0.05) or 0.05),
                lot_size=int(float(row.get("lot_size", 1) or 1)),
                segment=row.get("segment") or None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid instrument row for {tradingsymbol!r}: {exc}") from exc

