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

from db import get_engine
from routers.signals import VALID_EXCHANGES

router = APIRouter(prefix="/breadth", tags=["breadth"])

GroupLevel = Literal["sector", "industry_group", "industry"]


# --- Normalizers ------------------------------------------------------------

def _norm_exchange(ex: str) -> str:
    ex = ex.strip().upper()
    if ex not in VALID_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid exchange '{ex}'. Must be one of {sorted(VALID_EXCHANGES)}",
        )
    return ex


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
) -> bool:
    """True when st_sector_summary can satisfy the request without raw st_data aggregation."""
    return (
        level == "sector"
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
def breadth_sector_latest(
    request: Request,
    group_level: GroupLevel = Query(default="sector", description="Group by: sector | industry_group | industry"),
    exchange: str | None = Query(default=None, description="Optional exchange filter (N,Q,A,B,T,I). If omitted: all exchanges."),
    weekdate: str | None = Query(default=None, description="Override weekdate YYYY-MM-DD; default latest."),
    cs_only: bool = Query(default=True, description="Common Stocks only (recommended for breadth)."),
    include_unknown: bool = Query(default=False, description="Include rows where industry_id mapping is missing."),
    min_price: float | None = Query(default=None, description="Optional min price filter."),
    min_volume: int | None = Query(default=None, description="Optional min weekly volume filter (legacy: volume * 100)."),
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
    limit: int = Query(default=200000, ge=1, le=500000, description="Safety limit across all rows returned."),
):
    engine = get_engine()
    ex = _norm_exchange(exchange) if exchange else None

    if _use_sector_summary(
        level=group_level,
        cs_only=cs_only,
        include_unknown=include_unknown,
        min_price=min_price,
        min_volume=min_volume,
    ):
        sql_base, params = _breadth_summary_sql(
            start=start,
            end=end,
            exchange=ex,
        )
        order = " ORDER BY weekdate ASC, bullish_count DESC, avg_rsi DESC"
    else:
        sql_base, params = _breadth_sql(
            level=group_level,
            weekdate=None,
            start=start,
            end=end,
            exchange=ex,
            cs_only=cs_only,
            min_price=min_price,
            min_volume=min_volume,
            vol_scale=vol_scale,
            include_unknown=include_unknown,
        )
        order = " ORDER BY d.weekdate ASC, bullish_count DESC, avg_rsi DESC"

    sql = text(f"{sql_base}{order} LIMIT :limit")
    params["limit"] = int(limit)

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    flat = _postprocess([dict(r) for r in rows])

    if not group_by_week:
        return {
            "request_id": request.state.request_id,
            "group_level": group_level,
            "exchange": ex,
            "start": start,
            "end": end,
            "cs_only": cs_only,
            "include_unknown": include_unknown,
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
        "week_count": len(weeks),
        "count": len(flat),
        "weeks": weeks,
        "note": "Grouped by weekdate; each week sorted by bullish_count then avg_rsi.",
    }
