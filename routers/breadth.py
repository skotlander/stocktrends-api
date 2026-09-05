# routers/breadth.py
#
# Sector / Industry breadth endpoints
# - Uses st_data.industry_id joined to st_listsectorsandindustries.industry_code
# - Computes bullish/bearish breadth + maturity (trend_cnt, mt_cnt) + RSI strength
#
# Endpoints:
#   GET /v1/breadth/sector/latest
#   GET /v1/breadth/sector/history
#
# Notes:
# - Defaults to CS-only because ETFs duplicate underlying breadth.
# - Volume in st_data is legacy-scaled in your rules (volume * 100); keep vol_scale knob.
# - Caching for /v1/breadth/sector/latest is handled at nginx, not in app memory.

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from api.routing import pre_payment_semantic_validator
from db import get_engine
from routers.signals import VALID_EXCHANGES
from utils.history_bounds import (
    DEFAULT_HISTORY_WINDOW_WEEKS,
    LIMIT_SOURCE_CALLER,
    LIMIT_SOURCE_DEFAULT,
    build_applied_bounds,
    history_default_limit,
    history_max_limit,
    probe_limit,
    resolve_history_window,
    split_probe_rows,
)

router = APIRouter(prefix="/breadth", tags=["breadth"])

GroupLevel = Literal["sector", "industry_group", "industry"]

# Bounds for /breadth/sector/history.  A bare request previously ran the full
# multi-decade series through a 200000-row ceiling and returned ~48 MB; these
# are the values that make the default slice a deliberate research window.
# Read from the shared bounds table so the runtime Query below, the OpenAPI
# schema derived from it, and the discovery registry all state the same numbers.
# The pre-existing explicit ceiling is retained so that any caller who already
# raises `limit` deliberately keeps working unchanged.
HISTORY_PATH = "/v1/breadth/sector/history"
HISTORY_DEFAULT_LIMIT = history_default_limit(HISTORY_PATH)
HISTORY_MAX_LIMIT = history_max_limit(HISTORY_PATH)
HISTORY_WIDEN_HINT = (
    "Supply start and/or end to select a different range, and raise limit up to "
    f"{HISTORY_MAX_LIMIT} for more rows. When start and end are both omitted, a "
    f"trailing {DEFAULT_HISTORY_WINDOW_WEEKS}-week window ending at the latest "
    "available weekdate is applied."
)


# --- Normalizers ------------------------------------------------------------

def _norm_exchange(ex: str) -> str:
    ex = ex.strip().upper()
    if ex not in VALID_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid exchange '{ex}'. Must be one of {sorted(VALID_EXCHANGES)}",
        )
    return ex


def _validate_exchange_values(request: Request, values: dict) -> None:
    """
    Pre-payment adapter over `_norm_exchange`.

    The optional exchange filter is a fixed vocabulary decided by the query
    string alone, so an exchange code that does not exist is refused before any
    payment rail is touched.  Whether sector breadth aggregates for the requested week exist is a data question and stays
    behind the payment gate.

    Calls the same `_norm_exchange` the endpoint calls, so the 400 is unchanged.
    """
    exchange = values.get("exchange")
    if exchange:
        _norm_exchange(exchange)



def _latest_weekdate(engine, exchange: str | None) -> Any:
    if exchange:
        sql = text("SELECT MAX(weekdate) AS weekdate FROM st_data WHERE exchange = :exchange")
        params = {"exchange": exchange}
    else:
        sql = text("SELECT MAX(weekdate) AS weekdate FROM st_data")
        params = {}
    with engine.connect() as conn:
        row = conn.execute(sql, params).mappings().first()
    return row["weekdate"] if row else None


def _group_cols(level: GroupLevel) -> tuple[str, str]:
    """
    Returns:
      (select_group_cols, group_by_cols)
    """
    if level == "sector":
        sel = "s.sector_code, s.sector_name"
        grp = "s.sector_code, s.sector_name"
        return sel, grp
    if level == "industry_group":
        sel = "s.industry_group_code, s.industry_group_name"
        grp = "s.industry_group_code, s.industry_group_name"
        return sel, grp
    if level == "industry":
        sel = "s.industry_code, s.industry_name"
        grp = "s.industry_code, s.industry_name"
        return sel, grp
    raise ValueError("Invalid group_level")


def _use_sector_summary(
    *,
    level: GroupLevel,
    cs_only: bool,
    include_unknown: bool,
    min_price: float | None,
    min_volume: int | None,
    exchange: str | None,
) -> bool:
    """
    True when st_sector_summary can satisfy the request without raw st_data aggregation.

    `st_sector_summary` is aggregated per (weekdate, sector, exchange, type).  It
    can therefore answer a *single-exchange* request directly: the stored row is
    already the aggregate over exactly the population the caller asked for.

    It cannot answer an all-exchange request by itself.  Selecting without an
    exchange filter returns one row per exchange for the same
    (weekdate, sector_code) — and the projection does not even carry
    `ss.exchange`, so those rows reach the caller as unlabelled duplicates.
    Recombining them would need a weighted merge whose exactness depends on each
    stored average having been computed over the same row count as `total`,
    which the summary table does not record.

    So an all-exchange request falls through to `_breadth_sql`, which computes
    COUNT/SUM/AVG/MAX directly over the full (weekdate, sector) row population
    in one pass.  That is the definitional aggregate — there is no intermediate
    per-exchange mean to re-weight — and it is the identical aggregation
    `/breadth/sector/latest` already performs, so the two endpoints now agree on
    what all-exchange sector breadth means.
    """
    return (
        exchange is not None
        and level == "sector"
        and cs_only is True
        and include_unknown is False
        and min_price is None
        and min_volume is None
    )


def _breadth_summary_sql(
    *,
    start: str | None,
    end: str | None,
    exchange: str | None,
) -> tuple[str, dict[str, Any]]:
    """Build SQL against st_sector_summary for default sector breadth history requests."""
    params: dict[str, Any] = {}
    where = "WHERE ss.type = 'CS'"

    if exchange:
        where += " AND ss.exchange = :exchange"
        params["exchange"] = exchange

    if start:
        where += " AND ss.weekdate >= :start"
        params["start"] = start

    if end:
        where += " AND ss.weekdate <= :end"
        params["end"] = end

    sql = f"""
        SELECT
            ss.weekdate,
            ss.sector_code,
            ss.sector_name,
            ss.total,
            ss.bullish_count,
            ss.bearish_count,
            ss.neutral_count,
            ss.avg_trend_cnt,
            ss.avg_trend_cnt_bullish,
            ss.avg_trend_cnt_bearish,
            ss.max_trend_cnt,
            ss.avg_mt_cnt,
            ss.avg_mt_cnt_bullish,
            ss.avg_mt_cnt_bearish,
            ss.max_mt_cnt,
            ss.avg_rsi,
            ss.rsi_ge_110_count,
            ss.rsi_ge_120_count,
            ss.young_bullish_count,
            ss.mature_bullish_count
        FROM st_sector_summary ss
        {where}
    """
    return sql, params


def _where_clause(
    *,
    params: dict[str, Any],
    weekdate: str | None,
    start: str | None,
    end: str | None,
    exchange: str | None,
    cs_only: bool,
    min_price: float | None,
    min_volume: int | None,
    vol_scale: int,
    include_unknown: bool,
) -> str:
    where = "WHERE 1=1"

    if exchange:
        where += " AND d.exchange = :exchange"
        params["exchange"] = exchange

    if weekdate:
        where += " AND d.weekdate = :weekdate"
        params["weekdate"] = weekdate
    else:
        if start:
            where += " AND d.weekdate >= :start"
            params["start"] = start
        if end:
            where += " AND d.weekdate <= :end"
            params["end"] = end

    if cs_only:
        where += " AND d.type = 'CS'"

    if min_price is not None:
        where += " AND d.price >= :min_price"
        params["min_price"] = float(min_price)

    if min_volume is not None:
        # legacy scaling (volume * 100) in your rules
        where += " AND d.volume * :vol_scale >= :min_volume"
        params["vol_scale"] = int(vol_scale)
        params["min_volume"] = int(min_volume)

    if not include_unknown:
        where += " AND s.sector_code IS NOT NULL"

    return where


# --- SQL builders -----------------------------------------------------------

def _breadth_sql(
    *,
    level: GroupLevel,
    weekdate: str | None,
    start: str | None,
    end: str | None,
    exchange: str | None,
    cs_only: bool,
    min_price: float | None,
    min_volume: int | None,
    vol_scale: int,
    include_unknown: bool,
) -> tuple[str, dict[str, Any]]:
    sel_group, grp_group = _group_cols(level)

    params: dict[str, Any] = {}
    where = _where_clause(
        params=params,
        weekdate=weekdate,
        start=start,
        end=end,
        exchange=exchange,
        cs_only=cs_only,
        min_price=min_price,
        min_volume=min_volume,
        vol_scale=vol_scale,
        include_unknown=include_unknown,
    )

    bullish_set = "('^+','^-','v^')"
    bearish_set = "('v-','v+','^v')"
    neutral_set = "('--','=')"

    sql = f"""
        SELECT
            d.weekdate,
            {sel_group},

            COUNT(*) AS total,

            SUM(d.trend IN {bullish_set}) AS bullish_count,
            SUM(d.trend IN {bearish_set}) AS bearish_count,
            SUM(d.trend IN {neutral_set}) AS neutral_count,

            AVG(d.trend_cnt) AS avg_trend_cnt,
            AVG(CASE WHEN d.trend IN {bullish_set} THEN d.trend_cnt END) AS avg_trend_cnt_bullish,
            AVG(CASE WHEN d.trend IN {bearish_set} THEN d.trend_cnt END) AS avg_trend_cnt_bearish,
            MAX(d.trend_cnt) AS max_trend_cnt,

            AVG(d.mt_cnt) AS avg_mt_cnt,
            AVG(CASE WHEN d.trend IN {bullish_set} THEN d.mt_cnt END) AS avg_mt_cnt_bullish,
            AVG(CASE WHEN d.trend IN {bearish_set} THEN d.mt_cnt END) AS avg_mt_cnt_bearish,
            MAX(d.mt_cnt) AS max_mt_cnt,

            AVG(d.rsi) AS avg_rsi,
            SUM(d.rsi >= 110) AS rsi_ge_110_count,
            SUM(d.rsi >= 120) AS rsi_ge_120_count,

            SUM(d.trend IN {bullish_set} AND d.trend_cnt <= 4) AS young_bullish_count,
            SUM(d.trend IN {bullish_set} AND d.trend_cnt >= 20) AS mature_bullish_count

        FROM st_data d
        LEFT JOIN st_listsectorsandindustries s
          ON s.industry_code = d.industry_id

        {where}

        GROUP BY d.weekdate, {grp_group}
    """
    return sql, params


def _postprocess(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        total = int(r.get("total") or 0)
        bullish = int(r.get("bullish_count") or 0)
        bearish = int(r.get("bearish_count") or 0)
        neutral = int(r.get("neutral_count") or 0)

        rsi110 = int(r.get("rsi_ge_110_count") or 0)
        rsi120 = int(r.get("rsi_ge_120_count") or 0)

        young_bull = int(r.get("young_bullish_count") or 0)
        mature_bull = int(r.get("mature_bullish_count") or 0)

        def pct(x: int) -> float:
            return (x / total) if total else 0.0

        r["bullish_pct"] = pct(bullish)
        r["bearish_pct"] = pct(bearish)
        r["neutral_pct"] = pct(neutral)
        r["net_breadth"] = bullish - bearish

        r["rsi_ge_110_pct"] = pct(rsi110)
        r["rsi_ge_120_pct"] = pct(rsi120)

        r["young_bullish_pct"] = pct(young_bull)
        r["mature_bullish_pct"] = pct(mature_bull)

        out.append(r)
    return out


def _sort_key_for_level(level: GroupLevel) -> str:
    return " ORDER BY bullish_count DESC, avg_rsi DESC"


# --- Endpoints --------------------------------------------------------------

@router.get("/sector/latest")
@pre_payment_semantic_validator(_validate_exchange_values)
def breadth_sector_latest(
    request: Request,
    group_level: GroupLevel = Query(default="sector", description="Group by: sector | industry_group | industry"),
    exchange: str | None = Query(default=None, description="Optional exchange filter (N,Q,A,B,T,I). If omitted: all exchanges."),
    weekdate: str | None = Query(default=None, description="Override weekdate YYYY-MM-DD; default latest."),
    cs_only: bool = Query(default=True, description="Common Stocks only (recommended for breadth)."),
    include_unknown: bool = Query(default=False, description="Include rows where industry_id mapping is missing."),
    min_price: float | None = Query(default=None, description="Optional min price filter."),
    min_volume: int | None = Query(default=None, description="Optional min weekly volume filter in actual shares traded (e.g., 100000 = 100,000 shares)."),
    vol_scale: int = Query(default=100, description="Legacy volume scaling multiplier used in historical rules."),
    limit: int = Query(default=5000, ge=1, le=50000, description="Safety limit on number of groups returned."),
):
    engine = get_engine()

    ex = _norm_exchange(exchange) if exchange else None

    wd = weekdate
    if wd is None:
        latest = _latest_weekdate(engine, ex)
        if not latest:
            raise HTTPException(
                status_code=404,
                detail={"request_id": request.state.request_id, "error": "no_data", "message": "No Stock Trends data available."},
            )
        wd = str(latest)

    sql_base, params = _breadth_sql(
        level=group_level,
        weekdate=wd,
        start=None,
        end=None,
        exchange=ex,
        cs_only=cs_only,
        min_price=min_price,
        min_volume=min_volume,
        vol_scale=vol_scale,
        include_unknown=include_unknown,
    )

    sql = text(f"{sql_base}{_sort_key_for_level(group_level)} LIMIT :limit")
    params["limit"] = int(limit)

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    data = _postprocess([dict(r) for r in rows])

    return {
        "request_id": request.state.request_id,
        "group_level": group_level,
        "exchange": ex,
        "weekdate": wd,
        "cs_only": cs_only,
        "include_unknown": include_unknown,
        "count": len(data),
        "data": data,
        "hint": "Use /breadth/sector/history for time series. Defaults are tuned for bot efficiency.",
    }


@router.get("/sector/history")
@pre_payment_semantic_validator(_validate_exchange_values)
def breadth_sector_history(
    request: Request,
    group_level: GroupLevel = Query(default="sector", description="Group by: sector | industry_group | industry"),
    exchange: str | None = Query(default=None, description="Optional exchange filter (N,Q,A,B,T,I). If omitted: all exchanges."),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD (inclusive)"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD (inclusive)"),
    group_by_week: bool = Query(default=True, description="Group results by weekdate"),
    cs_only: bool = Query(default=True, description="Common Stocks only (recommended)."),
    include_unknown: bool = Query(default=False),
    min_price: float | None = Query(default=None),
    min_volume: int | None = Query(default=None),
    vol_scale: int = Query(default=100),
    limit: int = Query(
        default=HISTORY_DEFAULT_LIMIT,
        ge=1,
        le=HISTORY_MAX_LIMIT,
        description=(
            "Safety limit across all rows returned. When start and end are both "
            f"omitted, a trailing {DEFAULT_HISTORY_WINDOW_WEEKS}-week window is also applied."
        ),
    ),
):
    engine = get_engine()
    ex = _norm_exchange(exchange) if exchange else None

    # Bounding runs here, inside paid execution, rather than in the registered
    # pre-payment validator: it shapes the work performed, it does not decide
    # whether the request was answerable.
    effective_start, effective_end, window_source = resolve_history_window(
        start=start,
        end=end,
        anchor_weekdate=lambda: _latest_weekdate(engine, ex),
    )
    limit_source = (
        LIMIT_SOURCE_CALLER if "limit" in request.query_params else LIMIT_SOURCE_DEFAULT
    )

    if _use_sector_summary(
        level=group_level,
        cs_only=cs_only,
        include_unknown=include_unknown,
        min_price=min_price,
        min_volume=min_volume,
        exchange=ex,
    ):
        sql_base, params = _breadth_summary_sql(
            start=effective_start,
            end=effective_end,
            exchange=ex,
        )
        order = " ORDER BY weekdate ASC, bullish_count DESC, avg_rsi DESC"
    else:
        sql_base, params = _breadth_sql(
            level=group_level,
            weekdate=None,
            start=effective_start,
            end=effective_end,
            exchange=ex,
            cs_only=cs_only,
            min_price=min_price,
            min_volume=min_volume,
            vol_scale=vol_scale,
            include_unknown=include_unknown,
        )
        order = " ORDER BY d.weekdate ASC, bullish_count DESC, avg_rsi DESC"

    sql = text(f"{sql_base}{order} LIMIT :limit")
    params["limit"] = probe_limit(limit)

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    bounded_rows, truncated_by_limit = split_probe_rows(list(rows), limit)
    flat = _postprocess([dict(r) for r in bounded_rows])

    applied_bounds = build_applied_bounds(
        start=effective_start,
        end=effective_end,
        window_source=window_source,
        limit=limit,
        limit_source=limit_source,
        max_limit=HISTORY_MAX_LIMIT,
        rows_returned=len(flat),
        truncated_by_limit=truncated_by_limit,
        widen_with=HISTORY_WIDEN_HINT,
    )

    if not group_by_week:
        return {
            "request_id": request.state.request_id,
            "group_level": group_level,
            "exchange": ex,
            "start": start,
            "end": end,
            "cs_only": cs_only,
            "include_unknown": include_unknown,
            "applied_bounds": applied_bounds,
            "count": len(flat),
            "data": flat,
        }

    weeks: list[dict[str, Any]] = []
    current = None
    bucket: list[dict[str, Any]] = []

    for row in flat:
        wk = str(row["weekdate"])
        if current is None:
            current = wk
        if wk != current:
            weeks.append({"weekdate": current, "count": len(bucket), "data": bucket})
            current = wk
            bucket = []
        bucket.append(row)

    if current is not None:
        weeks.append({"weekdate": current, "count": len(bucket), "data": bucket})

    return {
        "request_id": request.state.request_id,
        "group_level": group_level,
        "exchange": ex,
        "start": start,
        "end": end,
        "cs_only": cs_only,
        "include_unknown": include_unknown,
        "applied_bounds": applied_bounds,
        "week_count": len(weeks),
        "count": len(flat),
        "weeks": weeks,
        "note": "Grouped by weekdate; each week sorted by bullish_count then avg_rsi.",
    }
