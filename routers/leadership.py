# routers/leadership.py

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from db import get_engine
from routers.signals import VALID_EXCHANGES

router = APIRouter(prefix="/leadership", tags=["leadership"])

BULLISH_TRENDS = ("^+", "^-", "v^")


def _norm_exchange(ex: str) -> str:
    ex = ex.strip().upper()
    if ex not in VALID_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid exchange '{ex}'. Must be one of {sorted(VALID_EXCHANGES)}",
        )
    return ex


def _latest_weekdate(engine, exchange: str | None, type_: str) -> Any | None:
    sql = "SELECT MAX(weekdate) AS wd FROM st_data WHERE type = :type"
    params: dict[str, Any] = {"type": type_}
    if exchange:
        sql += " AND exchange = :exchange"
        params["exchange"] = exchange
    with engine.connect() as conn:
        row = conn.execute(text(sql), params).mappings().first()
    return row["wd"] if row else None


def _where_date_clause(params: dict[str, Any], start: str | None, end: str | None) -> str:
    w = ""
    if start:
        w += " AND d.weekdate >= :start"
        params["start"] = start
    if end:
        w += " AND d.weekdate <= :end"
        params["end"] = end
    return w


def _rotation_summary_sql(
    *,
    type_: str,
    exchange: str | None,
    start: str | None,
    end: str | None,
    min_constituents: int,
    top_k: int | None,
) -> tuple[str, dict[str, Any]]:
    """
    Build the complete rotation/history SQL against st_sector_summary.

    Preserves the MySQL 5.7 user-variable ranking pattern; applies it to
    the pre-aggregated summary table instead of raw st_data.
    """
    params: dict[str, Any] = {
        "type": type_,
        "min_constituents": int(min_constituents),
    }

    where = (
        "WHERE ss.type = :type"
        " AND ss.sector_name IS NOT NULL"
        " AND ss.total >= :min_constituents"
    )

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
        SELECT *
        FROM (
            SELECT
                a.weekdate,
                a.sector_code,
                a.sector_name,
                a.n,
                a.bull_n,
                a.bull_pct,
                a.avg_rsi,
                a.avg_mt_cnt,
                a.avg_trend_cnt,
                a.bull_avg_rsi,
                a.leadership_score,
                @r := IF(@wk = a.weekdate, @r + 1, 1) AS rank_in_week,
                @wk := a.weekdate AS _wk_set
            FROM (
                SELECT
                    ss.weekdate,
                    ss.sector_code,
                    ss.sector_name,
                    ss.total AS n,
                    ss.bullish_count AS bull_n,
                    ss.bull_pct,
                    ss.avg_rsi,
                    ss.avg_mt_cnt,
                    ss.avg_trend_cnt,
                    ss.bull_avg_rsi,
                    ss.leadership_score
                FROM st_sector_summary ss
                {where}
                ORDER BY ss.weekdate ASC, ss.leadership_score DESC, ss.sector_name ASC
            ) a
            CROSS JOIN (SELECT @wk := NULL, @r := 0) vars
            ORDER BY a.weekdate ASC, a.leadership_score DESC, a.sector_name ASC
        ) ranked
    """

    if top_k is not None:
        params["top_k"] = int(top_k)
        sql += " WHERE ranked.rank_in_week <= :top_k "

    sql += " ORDER BY ranked.weekdate ASC, ranked.rank_in_week ASC, ranked.sector_name ASC "

    return sql, params


@router.get("/definitions")
def leadership_definitions():
    return {
        "concept": "Stock Trends leadership screens identify instruments with strong relative strength and trend alignment.",
        "indicators": {
            "rsi": "Relative strength vs benchmark. Values above 100 indicate outperformance.",
            "trend": "Stock Trends trend state (^+, ^-, v^, v+, v-, ^v).",
            "trend_cnt": "Weeks in the current specific trend state.",
            "mt_cnt": "Weeks in the current major trend classification (bullish or bearish).",
            "rsi_updn": "Weekly change in relative strength: + improving, - weakening, 0 flat.",
        },
        "taxonomy_source": "st_listsectorsandindustries",
        "taxonomy_levels": ["sector", "industry_group", "industry"],
        "notes": {
            "bullish_trends": list(BULLISH_TRENDS),
            "ranking": "summary/latest uses RSI desc (then mt_cnt desc). rotation/history ranks by leadership_score.",
            "mysql_compatibility": "Queries avoid window functions and CTEs to support MySQL 5.7 production.",
        },
    }


@router.get("/summary/latest")
def leadership_summary_latest(
    request: Request,
    exchange: str | None = Query(default=None, description="Optional exchange filter: N,Q,A,B,T,I"),
    weekdate: str | None = Query(default=None, description="Override weekdate YYYY-MM-DD; default latest for exchange/type"),
    type: str = Query(default="CS", description="Instrument type filter (default CS)"),
    min_rsi: int = Query(default=110, ge=0, le=500, description="Minimum RSI threshold"),
    min_mt_cnt: int = Query(default=4, ge=0, le=500, description="Minimum mt_cnt threshold"),
    limit_overall: int = Query(default=50, ge=1, le=1000, description="Overall leaders limit"),
    limit_bucket: int = Query(default=20, ge=1, le=200, description="Per-sector / per-industry-group limit"),
):
    """
    MySQL 5.7-safe leadership snapshots:
      - overall leaders (top RSI)
      - top leaders per sector (ranked by RSI, mt_cnt)
      - top leaders per industry group (ranked by RSI, mt_cnt)

    Taxonomy uses Stock Trends sector and industry classifications.
    """
    engine = get_engine()

    ex = _norm_exchange(exchange) if exchange else None

    if not weekdate:
        wd = _latest_weekdate(engine, ex, type)
        if not wd:
            raise HTTPException(
                status_code=404,
                detail={"request_id": request.state.request_id, "error": "no_data"},
            )
        weekdate = str(wd)

    params: dict[str, Any] = {
        "weekdate": weekdate,
        "type": type,
        "min_rsi": int(min_rsi),
        "min_mt_cnt": int(min_mt_cnt),
        "limit_overall": int(limit_overall),
        "limit_bucket": int(limit_bucket),
    }

    exch_clause = ""
    if ex:
        exch_clause = " AND d.exchange = :exchange "
        params["exchange"] = ex

    # ------------------------------------------------
    # Overall leaders
    # ------------------------------------------------
    overall_sql = text(f"""
        SELECT
            d.symbol,
            d.exchange,
            d.rsi,
            d.mt_cnt,
            d.trend,
            d.trend_cnt,
            d.rsi_updn,
            s.sector_name,
            s.industry_group_name,
            s.industry_name
        FROM st_data d
        LEFT JOIN st_listsectorsandindustries s
          ON s.industry_code = d.industry_id
        WHERE d.weekdate = :weekdate
          AND d.type = :type
          {exch_clause}
          AND d.rsi >= :min_rsi
          AND d.mt_cnt >= :min_mt_cnt
          AND s.sector_name IS NOT NULL
        ORDER BY d.rsi DESC, d.mt_cnt DESC, d.symbol ASC
        LIMIT :limit_overall
    """)

    # ------------------------------------------------
    # Sector leaders (top N per sector) using user vars
    # ------------------------------------------------
    sector_sql = text(f"""
        SELECT *
        FROM (
            SELECT
                t.*,
                @rn_s := IF(@sector = t.sector_name, @rn_s + 1, 1) AS rn,
                @sector := t.sector_name AS _sector_set
            FROM (
                SELECT
                    d.symbol,
                    d.exchange,
                    d.rsi,
                    d.mt_cnt,
                    d.trend,
                    d.trend_cnt,
                    s.sector_name
                FROM st_data d
                LEFT JOIN st_listsectorsandindustries s
                  ON s.industry_code = d.industry_id
                WHERE d.weekdate = :weekdate
                  AND d.type = :type
                  {exch_clause}
                  AND d.rsi >= :min_rsi
                  AND d.mt_cnt >= :min_mt_cnt
                  AND s.sector_name IS NOT NULL
                ORDER BY s.sector_name ASC, d.rsi DESC, d.mt_cnt DESC, d.symbol ASC
            ) t
            CROSS JOIN (SELECT @sector := '', @rn_s := 0) vars
        ) ranked
        WHERE ranked.rn <= :limit_bucket
        ORDER BY ranked.sector_name ASC, ranked.rsi DESC, ranked.mt_cnt DESC, ranked.symbol ASC
    """)

    # ------------------------------------------------
    # Industry-group leaders (top N per group) using user vars
    # ------------------------------------------------
    group_sql = text(f"""
        SELECT *
        FROM (
            SELECT
                t.*,
                @rn_g := IF(@grp = t.industry_group_name, @rn_g + 1, 1) AS rn,
                @grp := t.industry_group_name AS _grp_set
            FROM (
                SELECT
                    d.symbol,
                    d.exchange,
                    d.rsi,
                    d.mt_cnt,
                    d.trend,
                    d.trend_cnt,
                    s.industry_group_name
                FROM st_data d
                LEFT JOIN st_listsectorsandindustries s
                  ON s.industry_code = d.industry_id
                WHERE d.weekdate = :weekdate
                  AND d.type = :type
                  {exch_clause}
                  AND d.rsi >= :min_rsi
                  AND d.mt_cnt >= :min_mt_cnt
                  AND s.industry_group_name IS NOT NULL
                ORDER BY s.industry_group_name ASC, d.rsi DESC, d.mt_cnt DESC, d.symbol ASC
            ) t
            CROSS JOIN (SELECT @grp := '', @rn_g := 0) vars
        ) ranked
        WHERE ranked.rn <= :limit_bucket
        ORDER BY ranked.industry_group_name ASC, ranked.rsi DESC, ranked.mt_cnt DESC, ranked.symbol ASC
    """)

    try:
        with engine.connect() as conn:
            overall = conn.execute(overall_sql, params).mappings().all()
            sectors = conn.execute(sector_sql, params).mappings().all()
            groups = conn.execute(group_sql, params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    return {
        "request_id": request.state.request_id,
        "weekdate": weekdate,
        "exchange": ex,
        "filters": {"type": type, "min_rsi": min_rsi, "min_mt_cnt": min_mt_cnt},
        "overall_leaders": overall,
        "sector_leaders": sectors,
        "industry_group_leaders": groups,
        "note": "Rank-per-bucket implemented with MySQL user variables for MySQL 5.7 compatibility.",
    }


@router.get("/rotation/history")
def leadership_rotation_history(
    request: Request,
    exchange: str | None = Query(default=None, description="Optional exchange filter: N,Q,A,B,T,I"),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD (inclusive)"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD (inclusive)"),
    type: str = Query(default="CS", description="Instrument type filter (default CS)"),
    top_k: int | None = Query(default=5, ge=1, le=50, description="Top K sectors per week (omit for all)"),
    min_constituents: int = Query(default=25, ge=1, le=5000, description="Min # instruments in sector/week"),
    group_by_week: bool = Query(default=True, description="Group results by weekdate"),
):
    """
    Sector leadership rotation over time (weekly), MySQL 5.7 compatible.

    Aggregates per (weekdate, sector):
      - n, bull_n, bull_pct
      - avg_rsi
      - avg_mt_cnt, avg_trend_cnt
      - leadership_score: (avg_rsi * bull_pct) + (avg_mt_cnt * 0.25)
      - rank_in_week: computed with user variables after sorting by weekdate + score
    """
    engine = get_engine()
    ex = _norm_exchange(exchange) if exchange else None

    sql, params = _rotation_summary_sql(
        type_=type,
        exchange=ex,
        start=start,
        end=end,
        min_constituents=min_constituents,
        top_k=top_k,
    )

    try:
        with engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    flat = [dict(r) for r in rows]
    # clean up internal variable helper columns if present
    for d in flat:
        d.pop("_wk_set", None)

    if not group_by_week:
        return {
            "request_id": request.state.request_id,
            "exchange": ex,
            "start": start,
            "end": end,
            "filters": {"type": type, "min_constituents": min_constituents, "top_k": top_k},
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
        "exchange": ex,
        "start": start,
        "end": end,
        "filters": {"type": type, "min_constituents": min_constituents, "top_k": top_k},
        "week_count": len(weeks),
        "count": len(flat),
        "weeks": weeks,
        "note": "Ranking computed with MySQL user variables for MySQL 5.7 compatibility. Taxonomy from st_listsectorsandindustries.",
    }
