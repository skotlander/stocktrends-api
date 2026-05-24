# routers/prices.py

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text
from db import get_engine

# Reuse exchange validation + parsing you already have
from routers.signals import VALID_EXCHANGES, parse_symbol_exchange

router = APIRouter(prefix="/prices", tags=["prices"])


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


def _resolve_symbol_exchange(
    *,
    request: Request,
    symbol_exchange: str | None,
    symbol: str | None,
    exchange: str | None,
):
    """
    Resolve to (symbol, exchange). Requires either:
      - symbol_exchange, or
      - symbol + exchange
    (We keep it strict for prices endpoints to avoid ambiguity.)
    """
    if symbol_exchange:
        try:
            s, ex = parse_symbol_exchange(symbol_exchange)
            return _norm_symbol(s), _norm_exchange(ex)
        except ValueError as ve:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request.state.request_id,
                    "error": "invalid_symbol_exchange",
                    "message": str(ve),
                },
            )

    if not symbol or not exchange:
        raise HTTPException(
            status_code=400,
            detail={
                "request_id": request.state.request_id,
                "error": "missing_required_param",
                "message": "Provide symbol_exchange or (symbol and exchange).",
            },
        )

    return _norm_symbol(symbol), _norm_exchange(exchange)


@router.get("/latest")
def prices_latest(
    request: Request,
    symbol_exchange: str | None = Query(default=None, description="e.g., IBM-N"),
    symbol: str | None = Query(default=None, description="e.g., IBM"),
    exchange: str | None = Query(default=None, description="Exchange code: N,Q,A,B,T,I"),
    cs_only: bool = Query(default=True, description="Filter to Common Stocks only (type='CS')"),
):
    """
    Latest weekly price row for a specific instrument.
    """
    s, ex = _resolve_symbol_exchange(
        request=request,
        symbol_exchange=symbol_exchange,
        symbol=symbol,
        exchange=exchange,
    )

    sql = text("""
        SELECT
            weekdate,
            exchange,
            symbol,
            type,
            currency_code,
            price,
            adj_close,
            pr_week_hi,
            pr_week_lo,
            volume,
            trades,
            split_fact,
            pr_change
        FROM st_data
        WHERE symbol = :symbol
          AND exchange = :exchange
          AND (:cs_only = 0 OR type = 'CS')
        ORDER BY weekdate DESC
        LIMIT 1
    """)

    engine = get_engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sql,
                {"symbol": s, "exchange": ex, "cs_only": 1 if cs_only else 0},
            ).mappings().first()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "request_id": request.state.request_id,
                "error": "db_query_failed",
                "message": str(e),
            },
        )

    if not row:
        raise HTTPException(
            status_code=404,
            detail={
                "request_id": request.state.request_id,
                "error": "price_not_found",
                "symbol_exchange": f"{s}-{ex}",
            },
        )

    d = dict(row)
    d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'
    d["request_id"] = request.state.request_id
    return d


@router.get("/history")
def prices_history(
    request: Request,
    symbol_exchange: str | None = Query(default=None, description="e.g., IBM-N"),
    symbol: str | None = Query(default=None, description="e.g., IBM"),
    exchange: str | None = Query(default=None, description="Exchange code: N,Q,A,B,T,I"),
    cs_only: bool = Query(default=True, description="Filter to Common Stocks only (type='CS')"),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD (inclusive)"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD (inclusive)"),
    limit: int = Query(default=260, ge=1, le=2600, description="Max rows to return (260 ~ 5 years weekly)"),
):
    """
    Weekly price history for a specific instrument.
    Returns rows ascending by weekdate.
    """
    s, ex = _resolve_symbol_exchange(
        request=request,
        symbol_exchange=symbol_exchange,
        symbol=symbol,
        exchange=exchange,
    )

    # Optional date filters (string dates are fine for MySQL DATE comparisons)
    where_dates = ""
    params = {"symbol": s, "exchange": ex, "cs_only": 1 if cs_only else 0, "limit": limit}

    if start:
        where_dates += " AND weekdate >= :start"
        params["start"] = start
    if end:
        where_dates += " AND weekdate <= :end"
        params["end"] = end

    sql = text(f"""
        SELECT
            weekdate,
            exchange,
            symbol,
            type,
            currency_code,
            price,
            adj_close,
            pr_week_hi,
            pr_week_lo,
            volume,
            trades,
            split_fact,
            pr_change
        FROM st_data
        WHERE symbol = :symbol
          AND exchange = :exchange
          AND (:cs_only = 0 OR type = 'CS')
          {where_dates}
        ORDER BY weekdate DESC
        LIMIT :limit
    """)

    engine = get_engine()
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

    # Return ascending for client friendliness
    data = [dict(r) for r in reversed(rows)]
    for d in data:
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'

    return {
        "request_id": request.state.request_id,
        "symbol_exchange": f"{s}-{ex}",
        "cs_only": cs_only,
        "start": start,
        "end": end,
        "count": len(data),
        "data": data,
    }
