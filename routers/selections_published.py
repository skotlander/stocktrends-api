# routers/selections_published.py

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from db import get_engine
from routers.signals import VALID_EXCHANGES

router = APIRouter(prefix="/selections/published", tags=["selections_published"])

# Published Select definition thresholds
BASE_4WK = 0.00
BASE_13WK = 2.19
BASE_40WK = 6.45


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


def _published_where(
    *,
    ex: str | None,
    start: str | None,
    end: str | None,
    min_prob13wk: float | None,
    min_x4wk1: float,
    min_x13wk1: float,
    min_x40wk1: float,
    symbol: str | None,
    params: dict[str, Any],
) -> str:
    where = """
        WHERE r.x4wk1 > :min_x4wk1
          AND r.x13wk1 > :min_x13wk1
          AND r.x40wk1 > :min_x40wk1
    """
    params["min_x4wk1"] = float(min_x4wk1)
    params["min_x13wk1"] = float(min_x13wk1)
    params["min_x40wk1"] = float(min_x40wk1)

    if ex:
        where += " AND s.exchange = :exchange"
        params["exchange"] = ex

    if symbol:
        where += " AND s.symbol = :symbol"
        params["symbol"] = symbol

    if start:
        where += " AND s.weekdate >= :start"
        params["start"] = start

    if end:
        where += " AND s.weekdate <= :end"
        params["end"] = end

    if min_prob13wk is not None:
        where += " AND s.prob13wk >= :min_prob13wk"
        params["min_prob13wk"] = float(min_prob13wk)

    return where


@router.get(
    "/latest",
    summary="Latest published STIM Select list",
    description=(
        "Returns the latest published STIM Select (Stock Trends Inference Model Select) stock list. "
        "Selection criteria: the lower bound of the mean return confidence interval must exceed "
        "the base-period mean random return for all three ST-IM horizons simultaneously — "
        "4-week: x4wk1 > 0% (default), 13-week: x13wk1 > 2.19% (default), "
        "40-week: x40wk1 > 6.45% (default). "
        "Default probability threshold: prob13wk >= 55% (probability of exceeding the "
        "13-week base-period mean return, assuming normal distribution). "
        "Results are ranked by prob13wk descending. "
        "Each result includes full ST-IM distribution fields (x4wk, x13wk, x40wk series). "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def selections_published_latest(
    request: Request,
    exchange: str | None = Query(default=None, description="Optional exchange filter: N,Q,A,B,T,I"),
    min_prob13wk: float = Query(default=0.55, description="Minimum probability threshold"),
    min_x4wk1: float = Query(default=BASE_4WK, description="Minimum lower confidence bound for 4-week return"),
    min_x13wk1: float = Query(default=BASE_13WK, description="Minimum lower confidence bound for 13-week return"),
    min_x40wk1: float = Query(default=BASE_40WK, description="Minimum lower confidence bound for 40-week return"),
    limit: int = Query(default=2000, ge=1, le=20000, description="Safety limit"),
    include_data: bool = Query(default=False, description="Include Stock Trends signal context fields"),
    include_mast: bool = Query(default=False, description="Include sector, industry, and instrument metadata fields"),
    cs_only: bool = Query(default=True, description="When include_data=true, filter to common stocks"),
):
    """
    Latest published Select list:
    Base ST-IM selection universe filtered to the published definition.
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

    params: dict[str, Any] = {"limit": limit}
    where = _published_where(
        ex=ex,
        start=str(latest_week),
        end=str(latest_week),
        min_prob13wk=min_prob13wk,
        min_x4wk1=min_x4wk1,
        min_x13wk1=min_x13wk1,
        min_x40wk1=min_x40wk1,
        symbol=None,
        params=params,
    )

    if not include_data:
        sql = text(f"""
            SELECT
                s.weekdate,
                s.exchange,
                s.symbol,
                s.prob13wk,
                r.x4wk1,
                r.x4wk,
                r.x4wk2,
                r.x4wksd,
                r.x13wk1,
                r.x13wk,
                r.x13wk2,
                r.x13wksd,
                r.x40wk1,
                r.x40wk,
                r.x40wk2,
                r.x40wksd
                {_mast_select(include_mast)}
            FROM st_select s
            JOIN st_returnmeans r
              ON r.weekdate = s.weekdate
             AND r.exchange = s.exchange
             AND r.symbol = s.symbol
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
                r.x4wk1,
                r.x4wk,
                r.x4wk2,
                r.x4wksd,
                r.x13wk1,
                r.x13wk,
                r.x13wk2,
                r.x13wksd,
                r.x40wk1,
                r.x40wk,
                r.x40wk2,
                r.x40wksd,
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
            JOIN st_returnmeans r
              ON r.weekdate = s.weekdate
             AND r.exchange = s.exchange
             AND r.symbol = s.symbol
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
        "min_x4wk1": min_x4wk1,
        "min_x13wk1": min_x13wk1,
        "min_x40wk1": min_x40wk1,
        "include_data": include_data,
        "include_mast": include_mast,
        "cs_only": (cs_only if include_data else None),
        "count": len(data),
        "data": data,
    }


@router.get(
    "/history",
    summary="Historical published STIM Select records",
    description=(
        "Returns historical published STIM Select (Stock Trends Inference Model Select) records. "
        "Applies the same three-horizon confidence interval filter as /selections/published/latest "
        "(x4wk1, x13wk1, x40wk1 thresholds) plus prob13wk threshold. "
        "Filter by symbol_exchange, symbol, exchange, or date range. "
        "Each result includes full ST-IM distribution fields (x4wk, x13wk, x40wk series). "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def selections_published_history(
    request: Request,
    symbol_exchange: str | None = Query(default=None, description="e.g., IBM-N"),
    symbol: str | None = Query(default=None, description="e.g., IBM"),
    exchange: str | None = Query(default=None, description="Optional exchange filter: N,Q,A,B,T,I"),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD (inclusive)"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD (inclusive)"),
    min_prob13wk: float = Query(default=0.55, description="Minimum probability threshold"),
    min_x4wk1: float = Query(default=BASE_4WK, description="Minimum lower confidence bound for 4-week return"),
    min_x13wk1: float = Query(default=BASE_13WK, description="Minimum lower confidence bound for 13-week return"),
    min_x40wk1: float = Query(default=BASE_40WK, description="Minimum lower confidence bound for 40-week return"),
    limit: int = Query(default=5200, ge=1, le=50000, description="Safety limit"),
    include_data: bool = Query(default=False, description="Include Stock Trends signal context fields"),
    include_mast: bool = Query(default=False, description="Include sector, industry, and instrument metadata fields"),
    cs_only: bool = Query(default=True, description="When include_data=true, filter to common stocks"),
):
    """
    Published Select history:
    Base ST-IM selection universe filtered to the published definition.
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
    where = _published_where(
        ex=ex,
        start=start,
        end=end,
        min_prob13wk=min_prob13wk,
        min_x4wk1=min_x4wk1,
        min_x13wk1=min_x13wk1,
        min_x40wk1=min_x40wk1,
        symbol=s,
        params=params,
    )

    if not include_data:
        sql = text(f"""
            SELECT
                s.weekdate,
                s.exchange,
                s.symbol,
                s.prob13wk,
                r.x4wk1,
                r.x4wk,
                r.x4wk2,
                r.x4wksd,
                r.x13wk1,
                r.x13wk,
                r.x13wk2,
                r.x13wksd,
                r.x40wk1,
                r.x40wk,
                r.x40wk2,
                r.x40wksd
                {_mast_select(include_mast)}
            FROM st_select s
            JOIN st_returnmeans r
              ON r.weekdate = s.weekdate
             AND r.exchange = s.exchange
             AND r.symbol = s.symbol
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
                r.x4wk1,
                r.x4wk,
                r.x4wk2,
                r.x4wksd,
                r.x13wk1,
                r.x13wk,
                r.x13wk2,
                r.x13wksd,
                r.x40wk1,
                r.x40wk,
                r.x40wk2,
                r.x40wksd,
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
            JOIN st_returnmeans r
              ON r.weekdate = s.weekdate
             AND r.exchange = s.exchange
             AND r.symbol = s.symbol
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
        "min_x4wk1": min_x4wk1,
        "min_x13wk1": min_x13wk1,
        "min_x40wk1": min_x40wk1,
        "include_data": include_data,
        "include_mast": include_mast,
        "cs_only": (cs_only if include_data else None),
        "count": len(data),
        "data": data,
    }
