# routers/selections.py

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from db import get_engine
from routers.signals import VALID_EXCHANGES

logger = logging.getLogger("stocktrends_api.selections")
router = APIRouter(prefix="/selections", tags=["selections"])

STIM_SELECT_BASE_4WK = 0.0
STIM_SELECT_BASE_13WK = 2.19
STIM_SELECT_BASE_40WK = 6.45
STIM_SELECT_MIN_PRICE = 2.0
STIM_SELECT_MIN_VOLUME = 1000
STIM_SELECT_OUTCOME_EXCHANGES = {"N", "Q", "A", "B", "T"}


def _norm_symbol(s: str) -> str:
    return s.strip().upper()


def _norm_exchange(ex: str) -> str:
    ex = ex.strip().upper()
    if ex not in VALID_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid exchange '{ex}'. Must be one of {sorted(VALID_EXCHANGES)}",
        )
    return ex


def _norm_outcome_exchange(ex: str) -> str:
    ex = ex.strip().upper()
    if ex not in STIM_SELECT_OUTCOME_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid exchange '{ex}'. Must be one of {sorted(STIM_SELECT_OUTCOME_EXCHANGES)}",
        )
    return ex


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _to_int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _to_date_string(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _stim_select_outcomes_base_where(
    *,
    ex: str | None,
    start_date: date | None,
    end_date: date | None,
    params: dict[str, Any],
) -> str:
    params.update(
        {
            "base_4wk": STIM_SELECT_BASE_4WK,
            "base_13wk": STIM_SELECT_BASE_13WK,
            "base_40wk": STIM_SELECT_BASE_40WK,
            "min_price": STIM_SELECT_MIN_PRICE,
            "min_volume": STIM_SELECT_MIN_VOLUME,
        }
    )
    where = """
        WHERE b.x4wk1 > :base_4wk
          AND b.x13wk1 > :base_13wk
          AND b.x40wk1 > :base_40wk
          AND a.price >= :min_price
          AND a.volume > :min_volume
          AND a.fpr_chg13 IS NOT NULL
    """

    if start_date is not None:
        where += " AND a.weekdate >= :start_date"
        params["start_date"] = start_date
    if end_date is not None:
        where += " AND a.weekdate <= :end_date"
        params["end_date"] = end_date
    if ex is not None:
        where += " AND a.exchange = :exchange"
        params["exchange"] = ex

    return where


def _stim_select_outcomes_source_sql(where: str) -> str:
    return f"""
        FROM stdata.st_data a
        JOIN stweekly.st_returnmeans b
          ON a.weekdate = b.weekdate
         AND a.exchange = b.exchange
         AND a.symbol = b.symbol
        {where}
    """


def _stim_select_outcomes_ranked_cte(where: str) -> str:
    # Ranking by this z-score is equivalent to prob13wk DESC for positive
    # standard deviations, without requiring a database-specific normal CDF.
    return f"""
        WITH qualifying AS (
            SELECT
                a.weekdate,
                a.exchange,
                a.symbol,
                a.fpr_chg13,
                ROW_NUMBER() OVER (
                    PARTITION BY a.weekdate
                    ORDER BY
                        CASE
                            WHEN b.x13wksd IS NULL OR b.x13wksd = 0 THEN -999999999999
                            ELSE ((b.x13wk - :base_13wk) / b.x13wksd)
                        END DESC,
                        b.x13wk DESC,
                        a.exchange ASC,
                        a.symbol ASC
                ) AS rank_13wk_probability
            {_stim_select_outcomes_source_sql(where)}
        )
    """


def _stim_select_outcomes_aggregate_sql(*, where: str, limit_rank: int | None):
    if limit_rank is None:
        return text(f"""
            SELECT
                COUNT(*) AS outcome_count,
                MIN(a.weekdate) AS first_weekdate,
                MAX(a.weekdate) AS latest_weekdate,
                AVG(a.fpr_chg13) AS average_fpr_chg13,
                SUM(CASE WHEN a.fpr_chg13 > 0 THEN 1 ELSE 0 END) AS positive_return_count,
                SUM(CASE WHEN a.fpr_chg13 > :base_13wk THEN 1 ELSE 0 END) AS outperform_base_count
            {_stim_select_outcomes_source_sql(where)}
        """)

    return text(f"""
        {_stim_select_outcomes_ranked_cte(where)}
        SELECT
            COUNT(*) AS outcome_count,
            MIN(weekdate) AS first_weekdate,
            MAX(weekdate) AS latest_weekdate,
            AVG(fpr_chg13) AS average_fpr_chg13,
            SUM(CASE WHEN fpr_chg13 > 0 THEN 1 ELSE 0 END) AS positive_return_count,
            SUM(CASE WHEN fpr_chg13 > :base_13wk THEN 1 ELSE 0 END) AS outperform_base_count
        FROM qualifying
        WHERE rank_13wk_probability <= :limit_rank
    """)


def _stim_select_outcomes_values_sql(*, where: str, limit_rank: int | None):
    if limit_rank is None:
        return text(f"""
            SELECT a.fpr_chg13
            {_stim_select_outcomes_source_sql(where)}
        """)

    return text(f"""
        {_stim_select_outcomes_ranked_cte(where)}
        SELECT fpr_chg13
        FROM qualifying
        WHERE rank_13wk_probability <= :limit_rank
    """)


def _stim_select_signal_metadata() -> dict[str, Any]:
    return {
        "signal_id": "stim_select",
        "name": "ST-IM Select",
        "description": "Historical observations meeting Stock Trends Inference Model Select criteria.",
        "criteria": {
            "x4wk1_gt": STIM_SELECT_BASE_4WK,
            "x13wk1_gt": STIM_SELECT_BASE_13WK,
            "x40wk1_gt": STIM_SELECT_BASE_40WK,
            "price_gte": STIM_SELECT_MIN_PRICE,
            "volume_gt": STIM_SELECT_MIN_VOLUME,
            "avgmean_formula": "(x4wk + x13wk + x40wk) / 3",
            "prob4wk_formula": "1 - normal_cdf((0 - x4wk) / x4wksd)",
            "prob13wk_formula": "1 - normal_cdf((2.19 - x13wk) / x13wksd)",
            "prob40wk_formula": "1 - normal_cdf((6.45 - x40wk) / x40wksd)",
            "ranking": "prob13wk_desc",
        },
        "base_period_mean_13wk": STIM_SELECT_BASE_13WK,
    }


def _stim_select_outcome_provenance() -> dict[str, Any]:
    return {
        "source": "stim_select_signal_outcome_summary",
        "realized_return_field": "fpr_chg13",
        "realized_return_horizon": "13 weeks",
        "uses_mature_outcomes_only": True,
        "published_report_limited": False,
        "current_live_selections_excluded": True,
        "current_matching_symbols_excluded": True,
        "related_endpoints": [
            "/v1/meta/stim",
            "/v1/stim/latest",
            "/v1/selections/history",
            "/v1/selections/published/history",
        ],
    }


def _mast_select(include_mast: bool) -> str:
    if not include_mast:
        return ""

    return """
        ,
        m.name AS mast_name,
        m.shortname AS mast_shortname,
        m.type,
        m.gm_industry_id,
        m.x_sector_name,
        m.x_industry_group_name,
        m.x_industry_name,
        m.website,
        m.location
    """


def _mast_join(include_mast: bool) -> str:
    if not include_mast:
        return ""

    return """
        LEFT JOIN st_mast m
          ON m.exchange = s.exchange
         AND m.symbol = s.symbol
    """


@router.get(
    "/stim-select/outcomes/summary",
    summary="Public ST-IM Select signal outcome summary",
    description=(
        "Public aggregate historical outcome summary for observations meeting "
        "Stock Trends Inference Model Select criteria. Uses mature realized "
        "13-week forward returns from fpr_chg13. Does not expose current "
        "selections, current matching stocks, or individual symbols. Optional "
        "start_date and end_date filters apply to the signal weekdate. "
        "limit_rank applies a per-week rank cutoff ordered by the 13-week "
        "outperformance probability implied by the ST-IM normal-distribution "
        "model."
    ),
)
def stim_select_outcomes_summary(
    request: Request,
    start_date: date | None = Query(
        default=None,
        description="Inclusive signal weekdate filter in YYYY-MM-DD format.",
    ),
    end_date: date | None = Query(
        default=None,
        description="Inclusive signal weekdate filter in YYYY-MM-DD format.",
    ),
    exchange: str | None = Query(
        default=None,
        description="Optional exchange filter: N,Q,A,B,T.",
    ),
    limit_rank: int | None = Query(
        default=None,
        ge=1,
        le=5000,
        description=(
            "Optional per-week rank cutoff. Ranking is by prob13wk descending; "
            "for example, 10 includes only the top 10 qualifying observations per week."
        ),
    ),
):
    """
    Public aggregate evidence for the ST-IM Select signal-selection rule.

    This summarizes mature historical observations from stdata.st_data joined
    to stweekly.st_returnmeans. It intentionally returns aggregate outcomes only.
    """
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail={
                "request_id": request.state.request_id,
                "error": "invalid_date_range",
                "message": "start_date must be before or equal to end_date.",
            },
        )

    ex = _norm_outcome_exchange(exchange) if exchange else None
    params: dict[str, Any] = {}
    where = _stim_select_outcomes_base_where(
        ex=ex,
        start_date=start_date,
        end_date=end_date,
        params=params,
    )
    if limit_rank is not None:
        params["limit_rank"] = int(limit_rank)

    aggregate_sql = _stim_select_outcomes_aggregate_sql(where=where, limit_rank=limit_rank)
    values_sql = _stim_select_outcomes_values_sql(where=where, limit_rank=limit_rank)

    engine = get_engine()
    try:
        with engine.connect() as conn:
            aggregate_row = conn.execute(aggregate_sql, params).mappings().first()
            value_rows = conn.execute(values_sql, params).mappings().all()
    except Exception as exc:
        logger.exception(
            "ST-IM Select outcome summary query failed; request_id=%s",
            request.state.request_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "request_id": request.state.request_id,
                "error": "db_query_failed",
                "message": "Database query failed.",
            },
        )

    aggregate = dict(aggregate_row or {})
    count = _to_int_or_zero(aggregate.get("outcome_count"))
    positive_count = _to_int_or_zero(aggregate.get("positive_return_count"))
    outperform_count = _to_int_or_zero(aggregate.get("outperform_base_count"))
    outcome_values = [
        float(value)
        for row in value_rows
        if (value := _to_decimal(dict(row).get("fpr_chg13"))) is not None
    ]
    median_fpr_chg13 = float(median(outcome_values)) if outcome_values else None

    return {
        "request_id": request.state.request_id,
        "signal": _stim_select_signal_metadata(),
        "filters": {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "exchange": ex,
            "limit_rank": limit_rank,
        },
        "outcomes": {
            "horizon": "13w",
            "count": count,
            "first_weekdate": _to_date_string(aggregate.get("first_weekdate")),
            "latest_weekdate": _to_date_string(aggregate.get("latest_weekdate")),
            "average_fpr_chg13": _to_float(aggregate.get("average_fpr_chg13")),
            "median_fpr_chg13": median_fpr_chg13,
            "positive_return_count": positive_count,
            "positive_return_rate": _rate(positive_count, count),
            "outperform_base_count": outperform_count,
            "outperform_base_rate": _rate(outperform_count, count),
            "base_period_mean_13wk": STIM_SELECT_BASE_13WK,
        },
        "provenance": _stim_select_outcome_provenance(),
    }


@router.get(
    "/latest",
    summary="Latest base ST-IM selection universe",
    description=(
        "Returns the latest base ST-IM selection universe for the most recent weekdate. "
        "This is not the strict published STIM Select filter unless the caller applies "
        "thresholds or uses /v1/selections/published/latest. "
        "Securities are ranked by prob13wk descending — the probability of exceeding the "
        "13-week base-period mean random return (2.19%), assuming a normal distribution. "
        "Use min_prob13wk to apply a custom probability threshold (default: no filter). "
        "Use include_data=true to add Stock Trends signal fields (trend, rsi, vol_tag, etc.) "
        "per symbol. "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def selections_latest(
    request: Request,
    exchange: str | None = Query(default=None, description="Optional exchange filter: N,Q,A,B,T,I"),
    min_prob13wk: float | None = Query(default=None, description="Optional minimum prob13wk threshold"),
    limit: int = Query(default=2000, ge=1, le=20000, description="Safety limit"),
    include_data: bool = Query(default=False, description="Include Stock Trends signal context fields"),
    include_mast: bool = Query(default=False, description="Include sector, industry, and instrument metadata fields"),
    cs_only: bool = Query(default=True, description="When include_data=true, filter to common stocks"),
):
    """
    Latest ST-IM selection list for the most recent weekdate.
    """
    ex = _norm_exchange(exchange) if exchange else None
    engine = get_engine()

    sql_latest_week = text("SELECT MAX(weekdate) AS weekdate FROM st_select")

    try:
        with engine.connect() as conn:
            latest = conn.execute(sql_latest_week).mappings().first()
            latest_week = latest["weekdate"] if latest else None
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "request_id": request.state.request_id,
                "error": "db_query_failed",
                "message": str(e),
            },
        )

    if not latest_week:
        raise HTTPException(
            status_code=404,
            detail={"request_id": request.state.request_id, "error": "no_selection_data"},
        )

    params: dict[str, Any] = {"weekdate": latest_week, "limit": limit}
    where = "WHERE s.weekdate = :weekdate"

    if ex:
        where += " AND s.exchange = :exchange"
        params["exchange"] = ex

    if min_prob13wk is not None:
        where += " AND s.prob13wk >= :min_prob13wk"
        params["min_prob13wk"] = float(min_prob13wk)

    if not include_data:
        sql = text(f"""
            SELECT
                s.weekdate,
                s.exchange,
                s.symbol,
                s.prob13wk
                {_mast_select(include_mast)}
            FROM st_select s
            {_mast_join(include_mast)}
            {where}
            ORDER BY s.prob13wk DESC
            LIMIT :limit
        """)
    else:
        sql = text(f"""
            SELECT
                s.weekdate,
                s.exchange,
                s.symbol,
                s.prob13wk,
                d.type,
                d.currency_code,
                d.fullname,
                d.shortname,
                d.industry_id,
                d.trend,
                d.trend_cnt,
                d.mt_cnt,
                d.rsi,
                d.rsi_updn,
                d.vol_tag,
                d.price,
                d.adj_close,
                d.pr_change,
                d.pr_chg13
                {_mast_select(include_mast)}
            FROM st_select s
            LEFT JOIN st_data d
              ON d.weekdate = s.weekdate
             AND d.exchange = s.exchange
             AND d.symbol = s.symbol
             AND (:cs_only = 0 OR d.type = 'CS')
            {_mast_join(include_mast)}
            {where}
            ORDER BY s.prob13wk DESC
            LIMIT :limit
        """)
        params["cs_only"] = 1 if cs_only else 0

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "request_id": request.state.request_id,
                "error": "db_query_failed",
                "message": str(e),
            },
        )

    data = [dict(r) for r in rows]
    for d in data:
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'

    return {
        "request_id": request.state.request_id,
        "weekdate": str(latest_week),
        "exchange": ex,
        "min_prob13wk": min_prob13wk,
        "include_data": include_data,
        "include_mast": include_mast,
        "cs_only": (cs_only if include_data else None),
        "count": len(data),
        "data": data,
    }


@router.get(
    "/history",
    summary="Historical base ST-IM selection universe records",
    description=(
        "Returns historical base ST-IM selection universe records. These are not the strict "
        "published STIM Select records unless published thresholds are applied via "
        "/v1/selections/published/history. "
        "Filter by symbol_exchange, symbol, exchange, or date range. "
        "Each entry includes prob13wk — probability of exceeding the 13-week base-period "
        "mean random return (2.19%), assuming normal distribution. "
        "Use include_data=true to add Stock Trends signal fields per record. "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def selections_history(
    request: Request,
    symbol_exchange: str | None = Query(default=None, description="e.g., IBM-N"),
    symbol: str | None = Query(default=None, description="e.g., IBM"),
    exchange: str | None = Query(default=None, description="Optional exchange filter: N,Q,A,B,T,I"),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD (inclusive)"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD (inclusive)"),
    min_prob13wk: float | None = Query(default=None, description="Optional minimum prob13wk threshold"),
    limit: int = Query(default=520, ge=1, le=5200, description="Safety limit"),
    include_data: bool = Query(default=False, description="Include Stock Trends signal context fields"),
    include_mast: bool = Query(default=False, description="Include sector, industry, and instrument metadata fields"),
    cs_only: bool = Query(default=True, description="When include_data=true, filter to common stocks"),
):
    """
    Selection history.

    Supported filter modes:
      - symbol_exchange=IBM-N  (filters by symbol+exchange)
      - symbol=IBM&exchange=N  (filters by symbol+exchange)
      - symbol=IBM             (filters by symbol across all exchanges)
      - exchange=N             (filters by exchange across all symbols)
      - start/end only         (filters by date range across all selections)  [use with care]
    """
    engine = get_engine()

    s = None
    ex = None

    if symbol_exchange:
        if "-" not in symbol_exchange:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request.state.request_id,
                    "error": "invalid_symbol_exchange",
                    "message": "Use like 'IBM-N'",
                },
            )
        s_part, ex_part = symbol_exchange.rsplit("-", 1)
        s = _norm_symbol(s_part)
        ex = _norm_exchange(ex_part)

    elif symbol:
        s = _norm_symbol(symbol)

    if exchange:
        ex = _norm_exchange(exchange)

    params: dict[str, Any] = {"limit": limit}
    where = "WHERE 1=1"

    if s:
        where += " AND s.symbol = :symbol"
        params["symbol"] = s

    if ex:
        where += " AND s.exchange = :exchange"
        params["exchange"] = ex

    if start:
        where += " AND s.weekdate >= :start"
        params["start"] = start

    if end:
        where += " AND s.weekdate <= :end"
        params["end"] = end

    if min_prob13wk is not None:
        where += " AND s.prob13wk >= :min_prob13wk"
        params["min_prob13wk"] = float(min_prob13wk)

    if not include_data:
        sql = text(f"""
            SELECT
                s.weekdate,
                s.exchange,
                s.symbol,
                s.prob13wk
                {_mast_select(include_mast)}
            FROM st_select s
            {_mast_join(include_mast)}
            {where}
            ORDER BY s.weekdate DESC, s.prob13wk DESC
            LIMIT :limit
        """)
    else:
        sql = text(f"""
            SELECT
                s.weekdate,
                s.exchange,
                s.symbol,
                s.prob13wk,
                d.type,
                d.currency_code,
                d.fullname,
                d.shortname,
                d.industry_id,
                d.trend,
                d.trend_cnt,
                d.mt_cnt,
                d.rsi,
                d.rsi_updn,
                d.vol_tag,
                d.price,
                d.adj_close,
                d.pr_change,
                d.pr_chg13
                {_mast_select(include_mast)}
            FROM st_select s
            LEFT JOIN st_data d
              ON d.weekdate = s.weekdate
             AND d.exchange = s.exchange
             AND d.symbol = s.symbol
             AND (:cs_only = 0 OR d.type = 'CS')
            {_mast_join(include_mast)}
            {where}
            ORDER BY s.weekdate DESC, s.prob13wk DESC
            LIMIT :limit
        """)
        params["cs_only"] = 1 if cs_only else 0

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "request_id": request.state.request_id,
                "error": "db_query_failed",
                "message": str(e),
            },
        )

    data_desc = [dict(r) for r in rows]
    for d in data_desc:
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'

    if s and ex:
        data = list(reversed(data_desc))
    else:
        data = data_desc

    return {
        "request_id": request.state.request_id,
        "symbol": s,
        "exchange": ex,
        "symbol_exchange": f"{s}-{ex}" if (s and ex) else None,
        "start": start,
        "end": end,
        "min_prob13wk": min_prob13wk,
        "include_data": include_data,
        "include_mast": include_mast,
        "cs_only": (cs_only if include_data else None),
        "count": len(data),
        "data": data,
    }
