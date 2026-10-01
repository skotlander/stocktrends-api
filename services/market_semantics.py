"""Pure canonical market-domain semantics shared by market-facing services."""

from __future__ import annotations

from math import isfinite
from typing import Any


CANONICAL_EQUITY_TYPES: tuple[str, str] = ("CS", "UN")
# These are reporting exchanges for canonical market aggregates, not a list
# of every exchange for which Stock Trends may retain computed data.
CANONICAL_REPORTING_EXCHANGES: tuple[str, str, str, str] = ("A", "N", "Q", "T")

BULLISH_TRENDS: frozenset[str] = frozenset({"^+", "^-", "v^"})
BEARISH_TRENDS: frozenset[str] = frozenset({"^v", "v+", "v-"})
CLASSIFIED_TRENDS: frozenset[str] = BULLISH_TRENDS | BEARISH_TRENDS
NEUTRAL_TRENDS: frozenset[str] = frozenset({"--", "="})

AGGREGATE_RSI_MAX_VALID: int = 10_000


def is_valid_aggregate_rsi(value: Any) -> bool:
    """Return whether a Python RSI value is valid for aggregate calculations."""
    if value is None:
        return False
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(numeric_value) and numeric_value <= AGGREGATE_RSI_MAX_VALID
