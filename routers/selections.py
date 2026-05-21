# routers/selections.py

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from db import get_engine
from routers.signals import VALID_EXCHANGES

router = APIRouter(prefix="/selections", tags=["selections"])


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


@router.get(
    "/latest",
    summary="Latest base ST-IM selection universe",
    description=(
        "Returns the latest base st_select universe for the most recent weekdate. "
        "This is not the strict published STIM Select filter unless the caller applies "
        "thresholds or uses /v1/selections/published/latest. "
        "Securities are ranked by prob13wk descending — the probability of exceeding the "
        "13-week base-period mean random return (2.19%), assuming a normal distribution. "
        "Use min_prob13wk to apply a custom probability threshold (default: no filter). "
        "Use include_data=true to add Stock Trends signal fields (trend, rsi, vol_tag, etc.) "
        "per symbol via a join to st_data. "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def selections_latest(
    request: Request,
    exchange: str | None = Query(default=None, description="Optional exchange filter: N,Q,A,B,T,I"),
    min_prob13wk: float | None = Query(default=None, description="Optional minimum prob13wk threshold"),
    limit: int = Query(default=2000, ge=1, le=20000, description="Safety limit"),
    include_data: bool = Query(default=False, description="Join st_data for context fields"),
    include_mast: bool = Query(default=False, description="Join st_mast for sector/industry and metadata fields"),
    cs_only: bool = Query(default=True, description="When include_data=true, filter st_data to CS"),
):
    """
    Latest ST-IM selection list from st_select for the most recent weekdate in the table.
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
        "Returns historical base st_select universe records. These are not the strict "
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
    include_data: bool = Query(default=False, description="Join st_data for context fields"),
    include_mast: bool = Query(default=False, description="Join st_mast for sector/industry and metadata fields"),
    cs_only: bool = Query(default=True, description="When include_data=true, filter st_data to CS"),
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
