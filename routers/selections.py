# routers/selections.py

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from db import get_engine
from routers.signals import VALID_EXCHANGES
from services import stim_select_outcome_summary as outcome_summary_service
from services.stim_select_outcome_summary import (
    StimSelectOutcomeSummaryTableMissing,
    fetch_default_stim_select_outcome_summary,
)

logger = logging.getLogger("stocktrends_api.selections")
router = APIRouter(prefix="/selections", tags=["selections"])

STIM_SELECT_BASE_4WK = float(outcome_summary_service.STIM_SELECT_BASE_4WK)
STIM_SELECT_BASE_13WK = float(outcome_summary_service.STIM_SELECT_BASE_13WK)
STIM_SELECT_BASE_40WK = float(outcome_summary_service.STIM_SELECT_BASE_40WK)
STIM_SELECT_MIN_PRICE = float(outcome_summary_service.STIM_SELECT_MIN_PRICE)
STIM_SELECT_MIN_VOLUME = outcome_summary_service.STIM_SELECT_MIN_VOLUME
STIM_SELECT_OUTCOME_EXCHANGES = outcome_summary_service.STIM_SELECT_OUTCOME_EXCHANGES
STIM_SELECT_OUTCOME_DEFAULT_WINDOW_YEARS = (
    outcome_summary_service.STIM_SELECT_OUTCOME_DEFAULT_WINDOW_YEARS
)


class StimSelectOutcomeFilters(BaseModel):
    start_date: date | None = Field(
        default=None,
        description="Applied inclusive signal weekdate lower bound.",
    )
    end_date: date | None = Field(
        default=None,
        description="Applied inclusive signal weekdate upper bound.",
    )
    exchange: str | None = Field(default=None, description="Applied Stock Trends exchange filter.")
    limit_rank: int | None = Field(
        default=None,
        description=(
            "Applied per-week rank cutoff. Default no-date cache rows are seeded "
            "for omitted/null and limit_rank=10 unless additional rows are refreshed."
        ),
    )
    default_window_applied: bool = Field(
        ...,
        description=(
            "True when the endpoint applied its trailing 10-year default window "
            "because both start_date and end_date were omitted."
        ),
    )


class StimSelectOutcomeMetrics(BaseModel):
    horizon: str
    count: int
    first_weekdate: date | None
    latest_weekdate: date | None
    average_fpr_chg13: float | None
    median_fpr_chg13: float | None
    positive_return_count: int
    positive_return_rate: float
    outperform_base_count: int
    outperform_base_rate: float
    base_period_mean_13wk: float


class StimSelectOutcomeHorizonMetrics(BaseModel):
    horizon: str
    realized_return_field: str
    count: int
    first_weekdate: date | None
    latest_weekdate: date | None
    average_fpr_chg: float | None
    median_fpr_chg: float | None
    positive_return_count: int
    positive_return_rate: float
    outperform_base_count: int
    outperform_base_rate: float
    base_period_mean: float


class StimSelectOutcomesSummaryResponse(BaseModel):
    request_id: str
    signal: dict[str, Any]
    filters: StimSelectOutcomeFilters
    outcomes: StimSelectOutcomeMetrics
    outcomes_by_horizon: dict[str, StimSelectOutcomeHorizonMetrics] | None = Field(
        default=None,
        description=(
            "Multi-horizon realized outcome metrics from the persistent summary "
            "table for default no-date requests."
        ),
    )
    provenance: dict[str, Any]


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


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


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


def _subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, month=2, day=28)


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
        FROM stweekly.st_data a
        JOIN stweekly.st_returnmeans b
          ON a.weekdate = b.weekdate
         AND a.exchange = b.exchange
         AND a.symbol = b.symbol
        {where}
    """


def _stim_select_outcomes_latest_mature_date_sql(where: str):
    return text(f"""
        SELECT MAX(a.weekdate) AS latest_mature_outcome_date
        {_stim_select_outcomes_source_sql(where)}
    """)


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


def _latest_stim_select_mature_outcome_date(conn) -> date | None:
    params: dict[str, Any] = {}
    where = _stim_select_outcomes_base_where(
        ex=None,
        start_date=None,
        end_date=None,
        params=params,
    )
    row = conn.execute(
        _stim_select_outcomes_latest_mature_date_sql(where),
        params,
    ).mappings().first()
    return _to_date((row or {}).get("latest_mature_outcome_date"))


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
            "prob4wk_formula": "1 - normal_cdf((0 - x4wk) / x4wksd)",
            "prob13wk_formula": "1 - normal_cdf((2.19 - x13wk) / x13wksd)",
            "prob40wk_formula": "1 - normal_cdf((6.45 - x40wk) / x40wksd)",
            "ranking": "prob13wk_desc",
        },
        "base_period_mean_13wk": STIM_SELECT_BASE_13WK,
        "base_period_mean_4wk": STIM_SELECT_BASE_4WK,
        "base_period_mean_40wk": STIM_SELECT_BASE_40WK,
    }


def _stim_select_outcome_provenance(
    *,
    summary_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provenance = {
        "source": "stim_select_signal_outcome_summary",
        "realized_return_field": "fpr_chg13",
        "realized_return_fields": ["fpr_chg4", "fpr_chg13", "fpr_chg40"],
        "realized_return_horizon": "13 weeks",
        "realized_return_horizons": ["4 weeks", "13 weeks", "40 weeks"],
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
    if summary_record is not None:
        provenance["summary_table"] = {
            "served_from_summary_table": True,
            "table": outcome_summary_service.STIM_SELECT_OUTCOME_SUMMARY_TABLE,
            "summary_key": summary_record.get("summary_key"),
            "generated_at": _to_date_string(summary_record.get("generated_at")),
            "source_latest_mature_weekdate": _to_date_string(
                summary_record.get("source_latest_mature_weekdate")
            ),
            "refresh_command": "python -m maintenance.refresh_stim_select_outcome_summary_cache",
        }
    return provenance


def _summary_unavailable_detail(*, message: str) -> dict[str, Any]:
    return {
        "error": "outcome_summary_not_available",
        "message": message,
        "refresh_required": True,
        "supported_default_combinations": list(
            outcome_summary_service.STIM_SELECT_SUPPORTED_DEFAULT_SUMMARY_COMBINATIONS
        ),
        "custom_refresh_note": (
            "When start_date and end_date are omitted, the default refresh seeds "
            "all-exchange rows for limit_rank omitted/null and limit_rank=10. "
            "Other no-date exchange or limit_rank combinations require explicit "
            "date filters or a custom summary refresh."
        ),
    }


def _horizon_metrics_from_summary_record(
    record: dict[str, Any],
    *,
    horizon: str,
) -> dict[str, Any]:
    definition = outcome_summary_service.HORIZON_DEFINITIONS[horizon]
    field = definition["field"]
    suffix = definition["suffix"]
    base_column = definition["base_column"]
    count = _to_int_or_zero(
        record.get(f"count_{suffix}", record.get("outcome_count", record.get("count")))
    )
    positive_count = _to_int_or_zero(record.get(f"positive_return_count_{suffix}"))
    outperform_count = _to_int_or_zero(record.get(f"outperform_base_count_{suffix}"))
    return {
        "horizon": horizon,
        "realized_return_field": field,
        "count": count,
        "first_weekdate": _to_date_string(record.get("first_weekdate")),
        "latest_weekdate": _to_date_string(record.get("latest_weekdate")),
        "average_fpr_chg": _to_float(record.get(f"average_{field}")),
        "median_fpr_chg": _to_float(record.get(f"median_{field}")),
        "positive_return_count": positive_count,
        "positive_return_rate": _to_float(record.get(f"positive_return_rate_{suffix}")) or 0.0,
        "outperform_base_count": outperform_count,
        "outperform_base_rate": _to_float(record.get(f"outperform_base_rate_{suffix}")) or 0.0,
        "base_period_mean": _to_float(record.get(base_column)) or float(definition["base"]),
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
    response_model=StimSelectOutcomesSummaryResponse,
    summary="Public ST-IM Select signal outcome summary",
    description=(
        "Public aggregate historical outcome summary for observations meeting "
        "Stock Trends Inference Model Select criteria. Default no-date requests "
        "read from the persistent stweekly.stim_select_outcome_summary table "
        "and expose generated_at plus source_latest_mature_weekdate provenance. "
        "The legacy outcomes block remains the 13-week fpr_chg13 summary, while "
        "the summary-table response also includes 4-week, 13-week, and 40-week "
        "realized outcome metrics for fpr_chg4, fpr_chg13, and fpr_chg40. "
        "Optional start_date and end_date filters "
        "preserve the existing live 13-week aggregate behavior. "
        "Does not expose current selections, current matching stocks, or "
        "individual symbols. "
        "limit_rank applies a per-week rank cutoff ordered by the 13-week "
        "outperformance probability implied by the ST-IM normal-distribution "
        "model. When start_date and end_date are omitted, the default persistent "
        "summary refresh seeds all-exchange rows for limit_rank omitted/null and "
        "limit_rank=10; other no-date limit_rank values require explicit date "
        "filters or a custom summary refresh."
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
            "for example, 10 includes only the top 10 qualifying observations per week. "
            "When start_date and end_date are omitted, the persistent summary table "
            "currently supports the seeded default combinations limit_rank omitted/null "
            "and limit_rank=10. Other no-date limit_rank values require explicit date "
            "filters or a custom summary refresh."
        ),
    ),
):
    """
    Public aggregate evidence for the ST-IM Select signal-selection rule.

    This summarizes mature historical observations from stweekly.st_data joined
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
    default_window_applied = start_date is None and end_date is None
    applied_start_date = start_date
    applied_end_date = end_date

    engine = get_engine()
    summary_record: dict[str, Any] | None = None
    value_rows = []
    try:
        with engine.connect() as conn:
            if default_window_applied:
                try:
                    summary_record = fetch_default_stim_select_outcome_summary(
                        conn,
                        exchange=ex,
                        limit_rank=limit_rank,
                    )
                except StimSelectOutcomeSummaryTableMissing:
                    raise HTTPException(
                        status_code=503,
                        detail=_summary_unavailable_detail(
                            message="Historical summary table has not been created."
                        ),
                    )

                if summary_record is None:
                    raise HTTPException(
                        status_code=503,
                        detail=_summary_unavailable_detail(
                            message="Historical summary table has not been populated."
                        ),
                    )

                applied_start_date = _to_date(summary_record.get("start_date"))
                applied_end_date = _to_date(summary_record.get("end_date"))
                if applied_start_date is None or applied_end_date is None:
                    raise HTTPException(
                        status_code=503,
                        detail=_summary_unavailable_detail(
                            message="Historical summary table row is missing its applied date window."
                        ),
                    )
                aggregate_row = summary_record
            else:
                params: dict[str, Any] = {}
                where = _stim_select_outcomes_base_where(
                    ex=ex,
                    start_date=applied_start_date,
                    end_date=applied_end_date,
                    params=params,
                )
                if limit_rank is not None:
                    params["limit_rank"] = int(limit_rank)

                aggregate_sql = _stim_select_outcomes_aggregate_sql(where=where, limit_rank=limit_rank)
                values_sql = _stim_select_outcomes_values_sql(where=where, limit_rank=limit_rank)
                aggregate_row = conn.execute(aggregate_sql, params).mappings().first()
                value_rows = conn.execute(values_sql, params).mappings().all()
    except HTTPException:
        raise
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
    count = _to_int_or_zero(aggregate.get("outcome_count", aggregate.get("count")))
    positive_count = _to_int_or_zero(
        aggregate.get("positive_return_count_13wk", aggregate.get("positive_return_count"))
    )
    outperform_count = _to_int_or_zero(
        aggregate.get("outperform_base_count_13wk", aggregate.get("outperform_base_count"))
    )
    if summary_record is not None:
        median_fpr_chg13 = _to_float(aggregate.get("median_fpr_chg13"))
        positive_return_rate = _to_float(aggregate.get("positive_return_rate_13wk"))
        outperform_base_rate = _to_float(aggregate.get("outperform_base_rate_13wk"))
        outcomes_by_horizon = {
            horizon: _horizon_metrics_from_summary_record(aggregate, horizon=horizon)
            for horizon in ("4w", "13w", "40w")
        }
    else:
        outcome_values = [
            float(value)
            for row in value_rows
            if (value := _to_decimal(dict(row).get("fpr_chg13"))) is not None
        ]
        median_fpr_chg13 = float(median(outcome_values)) if outcome_values else None
        positive_return_rate = _rate(positive_count, count)
        outperform_base_rate = _rate(outperform_count, count)
        outcomes_by_horizon = None

    return {
        "request_id": request.state.request_id,
        "signal": _stim_select_signal_metadata(),
        "filters": {
            "start_date": applied_start_date.isoformat() if applied_start_date else None,
            "end_date": applied_end_date.isoformat() if applied_end_date else None,
            "exchange": ex,
            "limit_rank": limit_rank,
            "default_window_applied": default_window_applied,
        },
        "outcomes": {
            "horizon": "13w",
            "count": count,
            "first_weekdate": _to_date_string(aggregate.get("first_weekdate")),
            "latest_weekdate": _to_date_string(aggregate.get("latest_weekdate")),
            "average_fpr_chg13": _to_float(aggregate.get("average_fpr_chg13")),
            "median_fpr_chg13": median_fpr_chg13,
            "positive_return_count": positive_count,
            "positive_return_rate": positive_return_rate if positive_return_rate is not None else 0.0,
            "outperform_base_count": outperform_count,
            "outperform_base_rate": outperform_base_rate if outperform_base_rate is not None else 0.0,
            "base_period_mean_13wk": STIM_SELECT_BASE_13WK,
        },
        "outcomes_by_horizon": outcomes_by_horizon,
        "provenance": _stim_select_outcome_provenance(summary_record=summary_record),
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
