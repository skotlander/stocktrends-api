# routers/leadership.py

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from api.routing import pre_payment_semantic_validator
from db import get_engine
from routers.signals import VALID_EXCHANGES
from services.market_semantics import CANONICAL_REPORTING_EXCHANGES
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

router = APIRouter(prefix="/leadership", tags=["leadership"])
logger = logging.getLogger("stocktrends_api.leadership")

BULLISH_TRENDS = ("^+", "^-", "v^")

# Bounds for /leadership/rotation/history.  This endpoint previously had no
# `limit` parameter and emitted no LIMIT clause at all, so its result size was
# governed only by `top_k` multiplied by however many weeks exist.
ROTATION_HISTORY_PATH = "/v1/leadership/rotation/history"
ROTATION_HISTORY_DEFAULT_LIMIT = history_default_limit(ROTATION_HISTORY_PATH)
ROTATION_HISTORY_MAX_LIMIT = history_max_limit(ROTATION_HISTORY_PATH)
ROTATION_HISTORY_WIDEN_HINT = (
    "Supply start and/or end to select a different range, and raise limit up to "
    f"{ROTATION_HISTORY_MAX_LIMIT} for more rows. When start and end are both "
    f"omitted, a trailing {DEFAULT_HISTORY_WINDOW_WEEKS}-week window ending at "
    "the latest available weekdate is applied."
)


def _norm_exchange(ex: str) -> str:
    ex = ex.strip().upper()
    if ex != "*" and ex not in VALID_EXCHANGES:
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
    payment rail is touched.  Whether the leadership rankings for the requested week exist is a data question and stays
    behind the payment gate.

    Calls the same `_norm_exchange` the endpoint calls, so the 400 is unchanged.
    """
    exchange = values.get("exchange")
    if exchange:
        _norm_exchange(exchange)


def _validate_summary_exchange_values(request: Request, values: dict) -> None:
    exchange = values.get("exchange")
    if exchange and _norm_exchange(exchange) == "*":
        raise HTTPException(
            status_code=400,
            detail=f"Invalid exchange '{exchange}'. Must be one of {sorted(VALID_EXCHANGES)}",
        )
    type_ = values.get("type")
    if type_ is not None and type_.strip().upper() == "EQ":
        raise HTTPException(
            status_code=400,
            detail="Invalid type 'EQ'. leadership summary supports its CS-compatible types only.",
        )



def _norm_rotation_type(type_: str) -> str:
    return type_.strip().upper()


def _rotation_raw_scope(type_: str, exchange: str | None) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    if type_ == "EQ":
        scope = "d.type IN ('CS','UN')"
    else:
        scope = "d.type = :type"
        params["type"] = type_
    if exchange == "*":
        scope += " AND d.exchange IN ('A','N','Q','T')"
    elif exchange:
        scope += " AND d.exchange = :exchange"
        params["exchange"] = exchange
    return scope, params


def _latest_weekdate(engine, exchange: str | None, type_: str) -> Any | None:
    type_ = _norm_rotation_type(type_)
    scope, params = _rotation_raw_scope(type_, exchange)
    sql = f"SELECT MAX(weekdate) AS wd FROM st_data d WHERE {scope}"
    with engine.connect() as conn:
        row = conn.execute(text(sql), params).mappings().first()
    return row["wd"] if row else None


def _use_rotation_summary(type_: str, exchange: str | None) -> bool:
    return _norm_rotation_type(type_) in {"CS", "EQ"} and (
        exchange is None or exchange == "*" or exchange in CANONICAL_REPORTING_EXCHANGES
    )


def _latest_rotation_weekdate(engine, exchange: str | None, type_: str) -> Any | None:
    """Return the published shadow high-water week for canonical rotation."""
    type_ = _norm_rotation_type(type_)
    if not _use_rotation_summary(type_, exchange):
        return _latest_weekdate(engine, exchange, type_)
    sql = "SELECT MAX(weekdate) AS wd FROM st_sector_summary_shadow WHERE type = :type AND exchange = :exchange"
    params = {"type": type_, "exchange": exchange or "*"}
    with engine.connect() as conn:
        row = conn.execute(text(sql), params).mappings().first()
    return row["wd"] if row else None


class _RotationShadowServingUnavailable(Exception):
    """The published canonical rotation serving representation is unavailable."""


def _current_rotation_summary_anchor(
    engine, exchange: str | None, type_: str
) -> Any | None:
    """Return the published shadow anchor for canonical rotation requests."""
    if not _use_rotation_summary(type_, exchange):
        return None
    try:
        shadow_weekdate = _latest_rotation_weekdate(engine, exchange, type_)
    except DBAPIError as exc:
        logger.exception("Published sector rotation shadow probe failed")
        raise _RotationShadowServingUnavailable from exc
    if not shadow_weekdate:
        raise _RotationShadowServingUnavailable
    return shadow_weekdate


def _rotation_shadow_unavailable_error(request: Request) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "request_id": request.state.request_id,
            "error": "sector_summary_shadow_unavailable",
            "message": "Canonical sector rotation data is temporarily unavailable.",
        },
    )


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
    limit: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Build the complete rotation/history SQL against canonical shadow aggregates.

    Preserves the MySQL 5.7 user-variable ranking pattern; applies it to
    the pre-aggregated summary table instead of raw st_data.

    `limit` emits a real LIMIT clause after the final ordering, so the row cap
    is enforced by the database rather than assumed from `top_k`.
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

    # The stored '*' row is a direct source aggregation over A/N/Q/T.  Do not
    # reconstruct it by averaging per-exchange summary rows.
    summary_exchange = exchange or "*"
    if summary_exchange:
        where += " AND ss.exchange = :exchange"
        params["exchange"] = summary_exchange

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
                FROM st_sector_summary_shadow ss
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

    if limit is not None:
        params["limit"] = int(limit)
        sql += " LIMIT :limit "

    return sql, params


def _rotation_raw_sql(
    *, type_: str, exchange: str | None, start: str | None, end: str | None,
    min_constituents: int, top_k: int | None, limit: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Legacy non-shadow rotation path for types/exchanges shadow cannot represent."""
    type_ = _norm_rotation_type(type_)
    scope, params = _rotation_raw_scope(type_, exchange)
    params["min_constituents"] = int(min_constituents)
    where = f"WHERE {scope} AND s.sector_name IS NOT NULL"
    if start:
        where += " AND d.weekdate >= :start"
        params["start"] = start
    if end:
        where += " AND d.weekdate <= :end"
        params["end"] = end
    bull = "('^+','^-','v^')"
    bear = "('v-','v+','^v')"
    sql = f"""
        SELECT * FROM (
          SELECT a.*, @r := IF(@wk = a.weekdate, @r + 1, 1) AS rank_in_week,
                 @wk := a.weekdate AS _wk_set
          FROM (
            SELECT d.weekdate, s.sector_code, s.sector_name,
                   COUNT(*) AS n,
                   SUM(d.trend IN {bull}) AS bull_n,
                   SUM(d.trend IN {bull}) / NULLIF(SUM(d.trend IN {bull}) + SUM(d.trend IN {bear}), 0) AS bull_pct,
                   AVG(CASE WHEN d.trend IN ('^+','^-','v^','v-','v+','^v')
                            AND d.rsi IS NOT NULL AND d.rsi <= 10000 THEN d.rsi END) AS avg_rsi,
                   AVG(CASE WHEN d.trend IN ('^+','^-','v^','v-','v+','^v') THEN d.mt_cnt END) AS avg_mt_cnt,
                   AVG(CASE WHEN d.trend IN ('^+','^-','v^','v-','v+','^v') THEN d.trend_cnt END) AS avg_trend_cnt,
                   AVG(CASE WHEN d.trend IN {bull} AND d.rsi IS NOT NULL AND d.rsi <= 10000 THEN d.rsi END) AS bull_avg_rsi,
                   (AVG(CASE WHEN d.trend IN ('^+','^-','v^','v-','v+','^v')
                              AND d.rsi IS NOT NULL AND d.rsi <= 10000 THEN d.rsi END)
                    * (SUM(d.trend IN {bull}) / NULLIF(SUM(d.trend IN {bull}) + SUM(d.trend IN {bear}), 0))
                    + AVG(CASE WHEN d.trend IN ('^+','^-','v^','v-','v+','^v') THEN d.mt_cnt END) * 0.25) AS leadership_score
            FROM st_data d
            INNER JOIN st_listsectorsandindustries s ON s.industry_code = d.industry_id
            {where}
            GROUP BY d.weekdate, s.sector_code, s.sector_name
            HAVING COUNT(*) >= :min_constituents
            ORDER BY d.weekdate ASC, leadership_score DESC, s.sector_name ASC
          ) a CROSS JOIN (SELECT @wk := NULL, @r := 0) vars
          ORDER BY a.weekdate ASC, a.leadership_score DESC, a.sector_name ASC
        ) ranked
    """
    if top_k is not None:
        params["top_k"] = int(top_k)
        sql += " WHERE ranked.rank_in_week <= :top_k "
    sql += " ORDER BY ranked.weekdate ASC, ranked.rank_in_week ASC, ranked.sector_name ASC "
    if limit is not None:
        params["limit"] = int(limit)
        sql += " LIMIT :limit "
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
        "taxonomy_source": "Stock Trends sector and industry taxonomy",
        "taxonomy_levels": ["sector", "industry_group", "industry"],
        "notes": {
            "bullish_trends": list(BULLISH_TRENDS),
            "ranking": "summary/latest uses RSI desc (then mt_cnt desc). rotation/history ranks by leadership_score.",
            "mysql_compatibility": "Queries avoid window functions and CTEs to support MySQL 5.7 production.",
        },
    }


@router.get("/summary/latest")
@pre_payment_semantic_validator(_validate_summary_exchange_values)
def leadership_summary_latest(
    request: Request,
    exchange: str | None = Query(default=None, description="Optional exchange filter: N,Q,A,B,T,I"),
    weekdate: str | None = Query(default=None, description="Override weekdate YYYY-MM-DD; default latest for exchange/type"),
    type: str = Query(
        default="CS",
        description="CS-compatible instrument type filter (default CS); EQ is not supported on this endpoint.",
    ),
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
@pre_payment_semantic_validator(_validate_exchange_values)
def leadership_rotation_history(
    request: Request,
    exchange: str | None = Query(default=None, description="Optional exchange filter: N,Q,A,B,T,I, or * for canonical A/N/Q/T aggregate"),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD (inclusive)"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD (inclusive)"),
    type: str = Query(default="EQ", description="Canonical rotation population: EQ (CS+UN) by default; CS remains available for compatibility."),
    top_k: int | None = Query(default=5, ge=1, le=50, description="Top K sectors per week (omit for all)"),
    min_constituents: int = Query(default=25, ge=1, le=5000, description="Min # instruments in sector/week"),
    group_by_week: bool = Query(default=True, description="Group results by weekdate"),
    limit: int = Query(
        default=ROTATION_HISTORY_DEFAULT_LIMIT,
        ge=1,
        le=ROTATION_HISTORY_MAX_LIMIT,
        description=(
            "Safety limit across all rows returned. When start and end are both "
            f"omitted, a trailing {DEFAULT_HISTORY_WINDOW_WEEKS}-week window is also applied."
        ),
    ),
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
    normalized_type = _norm_rotation_type(type)
    # The shadow's omitted-exchange row is the stored canonical A/N/Q/T
    # aggregate. Preserve that effective scope for canonical serving.
    effective_exchange = (
        "*" if ex is None and _use_rotation_summary(normalized_type, ex) else ex
    )
    try:
        summary_anchor = _current_rotation_summary_anchor(
            engine, effective_exchange, normalized_type
        )
    except _RotationShadowServingUnavailable as exc:
        raise _rotation_shadow_unavailable_error(request) from exc
    use_summary = summary_anchor is not None

    # Service shaping, applied behind the payment boundary alongside the query
    # it bounds — not in the pre-payment validator, which only rejects requests
    # the query string alone already makes unanswerable.
    effective_start, effective_end, window_source = resolve_history_window(
        start=start,
        end=end,
        anchor_weekdate=lambda: (
            summary_anchor
            if use_summary
            else _latest_weekdate(engine, effective_exchange, normalized_type)
        ),
    )
    limit_source = (
        LIMIT_SOURCE_CALLER if "limit" in request.query_params else LIMIT_SOURCE_DEFAULT
    )

    sql_builder = _rotation_summary_sql if use_summary else _rotation_raw_sql
    sql, params = sql_builder(
        type_=normalized_type, exchange=effective_exchange, start=effective_start, end=effective_end,
        min_constituents=min_constituents, top_k=top_k, limit=probe_limit(limit),
    )

    try:
        with engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    bounded_rows, truncated_by_limit = split_probe_rows(list(rows), limit)
    flat = [dict(r) for r in bounded_rows]
    # clean up internal variable helper columns if present
    for d in flat:
        d.pop("_wk_set", None)

    applied_bounds = build_applied_bounds(
        start=effective_start,
        end=effective_end,
        window_source=window_source,
        limit=limit,
        limit_source=limit_source,
        max_limit=ROTATION_HISTORY_MAX_LIMIT,
        rows_returned=len(flat),
        truncated_by_limit=truncated_by_limit,
        widen_with=ROTATION_HISTORY_WIDEN_HINT,
    )

    if not group_by_week:
        return {
            "request_id": request.state.request_id,
            "exchange": effective_exchange,
            "start": start,
            "end": end,
            "filters": {"type": normalized_type, "min_constituents": min_constituents, "top_k": top_k},
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
        "exchange": effective_exchange,
        "start": start,
        "end": end,
        "filters": {"type": normalized_type, "min_constituents": min_constituents, "top_k": top_k},
        "applied_bounds": applied_bounds,
        "week_count": len(weeks),
        "count": len(flat),
        "weeks": weeks,
        "note": "Ranking computed with MySQL user variables for MySQL 5.7 compatibility. Taxonomy uses Stock Trends sector and industry classifications.",
    }
