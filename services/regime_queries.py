"""Read-only canonical market-regime queries shared by route consumers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from sqlalchemy import text

from services.market_semantics import (
    AGGREGATE_RSI_MAX_VALID,
    CANONICAL_EQUITY_TYPES,
    CANONICAL_REPORTING_EXCHANGES,
    CLASSIFIED_TRENDS,
)


def _bind_in_values(
    params: dict[str, Any], prefix: str, values: Iterable[Any]
) -> str:
    """Add deterministic named binds for a SQL IN predicate."""
    names: list[str] = []
    for index, value in enumerate(values):
        name = f"{prefix}{index}"
        params[name] = value
        names.append(f":{name}")
    return ", ".join(names)


def fetch_eligible_regime_weekdates(
    conn: Any,
    *,
    limit: int,
    start_date: date | None = None,
) -> list[date]:
    """Return newest weeks having at least one classified canonical-equity row."""
    params: dict[str, Any] = {"limit": int(limit)}
    type_placeholders = _bind_in_values(
        params, "equity_type_", CANONICAL_EQUITY_TYPES
    )
    exchange_placeholders = _bind_in_values(
        params, "reporting_exchange_", CANONICAL_REPORTING_EXCHANGES
    )
    trend_placeholders = _bind_in_values(
        params, "classified_trend_", tuple(sorted(CLASSIFIED_TRENDS))
    )
    start_clause = ""
    if start_date is not None:
        start_clause = " AND weekdate >= :start_date"
        params["start_date"] = start_date

    rows = conn.execute(
        text(
            f"""
            SELECT DISTINCT weekdate
            FROM st_data
            WHERE type IN ({type_placeholders})
              AND exchange IN ({exchange_placeholders})
              AND trend IN ({trend_placeholders})
              {start_clause}
            ORDER BY weekdate DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return [row["weekdate"] for row in rows if row["weekdate"]]


def fetch_regime_trend_aggregates(
    conn: Any,
    *,
    weekdates: Iterable[date],
) -> list[Any]:
    """Aggregate canonical-equity trend, RSI, and maturity inputs by week/state."""
    ordered_weekdates = tuple(weekdates)
    if not ordered_weekdates:
        return []

    params: dict[str, Any] = {"rsi_max_valid": AGGREGATE_RSI_MAX_VALID}
    week_placeholders = _bind_in_values(params, "weekdate_", ordered_weekdates)
    type_placeholders = _bind_in_values(
        params, "equity_type_", CANONICAL_EQUITY_TYPES
    )
    exchange_placeholders = _bind_in_values(
        params, "reporting_exchange_", CANONICAL_REPORTING_EXCHANGES
    )

    # MySQL numeric columns cannot persist NaN/±Inf; persisted non-null numeric
    # RSI values are finite, so aggregate validity at the SQL boundary is
    # enforced by the maximum check without imposing a lower bound.
    valid_rsi = (
        "rsi IS NOT NULL "
        "AND rsi <= :rsi_max_valid"
    )
    rows = conn.execute(
        text(
            f"""
            SELECT
                weekdate,
                trend,
                COUNT(*) AS cnt,
                SUM(CASE WHEN {valid_rsi} THEN rsi ELSE 0 END) AS valid_rsi_sum,
                SUM(CASE WHEN {valid_rsi} THEN 1 ELSE 0 END) AS valid_rsi_count,
                SUM(CASE WHEN mt_cnt IS NOT NULL THEN mt_cnt ELSE 0 END) AS mt_cnt_sum,
                SUM(CASE WHEN mt_cnt IS NOT NULL THEN 1 ELSE 0 END) AS mt_cnt_count
            FROM st_data
            WHERE weekdate IN ({week_placeholders})
              AND type IN ({type_placeholders})
              AND exchange IN ({exchange_placeholders})
            GROUP BY weekdate, trend
            ORDER BY weekdate DESC, trend
            """
        ),
        params,
    ).mappings().all()
    return list(rows)
