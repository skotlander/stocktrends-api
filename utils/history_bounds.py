# utils/history_bounds.py
#
# Shared bounding vocabulary for paid historical endpoints.
#
# Why this exists
# ---------------
# A historical endpoint called with no query string used to return whatever the
# safety limit allowed — for `/v1/breadth/sector/history` that was a ~48 MB
# body an autonomous client had not asked for and could not detect as
# truncated.  The rule this module encodes is:
#
#     A bare request to a paid historical endpoint is bounded on purpose, and
#     the bounds it received are reported back to the caller as machine-readable
#     values.
#
# The bounding is *service shaping*, not validation.  It decides what slice of
# already-purchased work to perform, so it belongs behind the payment execution
# boundary with the rest of paid execution — never in a pre-payment semantic
# validator, which exists only to reject what the request alone already makes
# unanswerable.
#
# Two knobs, one definition each:
#   - the default trailing window, applied only when the caller supplied
#     neither `start` nor `end`;
#   - the default row limit, which the caller may still raise to the endpoint's
#     existing maximum.
#
# Explicit caller bounds always win.  Supplying `start`, `end` or `limit`
# reproduces the endpoint's prior behaviour exactly.

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, TypedDict

# A substantial multi-year weekly research window.  Deliberately stated as a
# size, not as a claim about what market conditions it happens to contain.
DEFAULT_HISTORY_WINDOW_WEEKS = 104

WINDOW_SOURCE_CALLER = "caller_supplied"
WINDOW_SOURCE_DEFAULT = "default_trailing_window"
WINDOW_SOURCE_UNAVAILABLE = "unbounded_no_anchor_weekdate"
# Endpoints whose row limit already bounded them safely keep their retrieval
# semantics unchanged and gain only the disclosure block, so they report that no
# default window exists rather than implying one was applied.
WINDOW_SOURCE_NOT_APPLIED = "no_default_window"

LIMIT_SOURCE_CALLER = "caller_supplied"
LIMIT_SOURCE_DEFAULT = "default"


# ---------------------------------------------------------------------------
# Per-endpoint bounds, defined once.
#
# Three surfaces have to agree about these numbers: the runtime `Query(...)`
# declaration, the OpenAPI schema FastAPI derives from it, and the discovery
# metadata in `discovery/endpoint_metadata.py` that feeds /v1/ai/tools and the
# x402 preview.  When each transcribed its own copy they drifted — the static
# manifest advertised a 500-row default for a route whose runtime default was
# 200000, and a 52-row default for two routes whose runtime default was 260.
#
# Both the routers and the discovery registry read the values from here, so the
# published numbers are the enforced numbers by construction.  This module
# imports nothing from routers, pricing, payments or the database, so either
# side can import it without a cycle.
#
# `default_window_weeks` is None where the endpoint applies no default date
# window: its row limit already bounds it, and inventing a window there would
# change retrieval semantics for no safety gain.
# ---------------------------------------------------------------------------
class HistoryBounds(TypedDict):
    """
    The published bounds for one history endpoint.

    `default_limit` and `max_limit` always exist; only `default_window_weeks` is
    optional, and it is None precisely where the endpoint applies no default
    date window.
    """

    default_limit: int
    max_limit: int
    default_window_weeks: int | None


HISTORY_ENDPOINT_BOUNDS: dict[str, HistoryBounds] = {
    "/v1/breadth/sector/history": {
        "default_limit": 5000,
        "max_limit": 500000,
        "default_window_weeks": DEFAULT_HISTORY_WINDOW_WEEKS,
    },
    "/v1/stwr/reports/history": {
        "default_limit": 500,
        "max_limit": 500000,
        "default_window_weeks": DEFAULT_HISTORY_WINDOW_WEEKS,
    },
    "/v1/leadership/rotation/history": {
        "default_limit": 2000,
        "max_limit": 50000,
        "default_window_weeks": DEFAULT_HISTORY_WINDOW_WEEKS,
    },
    "/v1/selections/history": {
        "default_limit": 520,
        "max_limit": 5200,
        "default_window_weeks": None,
    },
    "/v1/selections/published/history": {
        "default_limit": 5200,
        "max_limit": 50000,
        "default_window_weeks": None,
    },
    "/v1/indicators/history": {
        "default_limit": 260,
        "max_limit": 2600,
        "default_window_weeks": None,
    },
    "/v1/prices/history": {
        "default_limit": 260,
        "max_limit": 2600,
        "default_window_weeks": None,
    },
    "/v1/stim/history": {
        "default_limit": 260,
        "max_limit": 2600,
        "default_window_weeks": None,
    },
    "/v1/market/regime/history": {
        "default_limit": 12,
        "max_limit": 52,
        "default_window_weeks": None,
    },
}


def history_default_limit(path: str) -> int:
    return HISTORY_ENDPOINT_BOUNDS[path]["default_limit"]


def history_max_limit(path: str) -> int:
    return HISTORY_ENDPOINT_BOUNDS[path]["max_limit"]


def history_default_window_weeks(path: str) -> int | None:
    return HISTORY_ENDPOINT_BOUNDS[path]["default_window_weeks"]


def _as_date(value: Any) -> date | None:
    """Coerce a weekdate from the database (date, datetime or string) to a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def resolve_history_window(
    *,
    start: str | None,
    end: str | None,
    anchor_weekdate: Any,
    window_weeks: int = DEFAULT_HISTORY_WINDOW_WEEKS,
) -> tuple[str | None, str | None, str]:
    """
    Resolve the effective (start, end) date bounds and record their source.

    The default trailing window is applied only when the caller supplied
    neither bound, so any explicitly bounded request keeps the exact range it
    asked for.  The window is anchored on the most recent weekdate actually
    available to the endpoint rather than on wall-clock today, so the returned
    slice is a real data window and not a range that runs past the data.

    `anchor_weekdate` may be a callable.  Resolving the anchor costs a
    `MAX(weekdate)` query, and a request that already carries its own bounds
    needs no anchor at all — so callers pass a thunk and it is invoked only on
    the path that actually uses it.  An explicitly bounded request therefore
    issues exactly the queries it issued before this bounding existed.

    Returns (effective_start, effective_end, window_source).
    """
    if start or end:
        return start, end, WINDOW_SOURCE_CALLER

    anchor = _as_date(anchor_weekdate() if callable(anchor_weekdate) else anchor_weekdate)
    if anchor is None:
        # No data to anchor on.  The row limit is then the only bound, which is
        # correct: there is nothing to window.
        return None, None, WINDOW_SOURCE_UNAVAILABLE

    # Inclusive window: `window_weeks` weekly observations ending at the anchor.
    window_start = anchor - timedelta(weeks=max(window_weeks, 1) - 1)
    return window_start.isoformat(), anchor.isoformat(), WINDOW_SOURCE_DEFAULT


def probe_limit(limit: int) -> int:
    """
    Row count to request so truncation can be *observed* rather than inferred.

    Asking for one row beyond the limit distinguishes "the result happens to be
    exactly `limit` rows" from "the result was cut off at `limit`".  A caller
    cannot tell those apart from a row count alone, and requirement 6 of this
    work is that truncation must not be silent.
    """
    return int(limit) + 1


def split_probe_rows(rows: list[Any], limit: int) -> tuple[list[Any], bool]:
    """Trim the probe row off `rows` and report whether it was there."""
    if len(rows) > limit:
        return rows[:limit], True
    return rows, False


def build_applied_bounds(
    *,
    start: str | None,
    end: str | None,
    window_source: str,
    limit: int,
    limit_source: str,
    max_limit: int,
    rows_returned: int,
    truncated_by_limit: bool,
    default_window_weeks: int | None = DEFAULT_HISTORY_WINDOW_WEEKS,
    widen_with: str | None = None,
) -> dict[str, Any]:
    """
    Machine-readable statement of the bounds this response was produced under.

    Every value a caller needs in order to widen the request deliberately is a
    field.  `widen_with` is prose for a human reading the body and must never be
    the only place a bound appears.
    """
    bounds: dict[str, Any] = {
        "start": start,
        "end": end,
        "window_source": window_source,
        "default_window_weeks": (
            int(default_window_weeks) if default_window_weeks is not None else None
        ),
        "limit": int(limit),
        "limit_source": limit_source,
        "max_limit": int(max_limit),
        "rows_returned": int(rows_returned),
        "truncated_by_limit": bool(truncated_by_limit),
    }
    if widen_with:
        bounds["widen_with"] = widen_with
    return bounds
