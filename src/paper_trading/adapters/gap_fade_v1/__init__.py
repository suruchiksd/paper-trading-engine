"""Frozen Phase-7B positive-gap fade paper-observation adapter."""

from .adapter import (
    GapFadeConfig,
    GapFadeDecision,
    OpeningWindow,
    PreviousClose,
    evaluate_symbol,
    load_gap_fade_config,
    position_size,
    select_portfolio_candidates,
    verify_frozen_research_identity,
)

__all__ = [
    "GapFadeConfig",
    "GapFadeDecision",
    "OpeningWindow",
    "PreviousClose",
    "evaluate_symbol",
    "load_gap_fade_config",
    "position_size",
    "select_portfolio_candidates",
    "verify_frozen_research_identity",
]
