# routers/instruments.py

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text
from db import get_market_engine


router = APIRouter(prefix="/instruments", tags=["instruments"])

# If these live in your signals module, import them instead:
from routers.signals import VALID_EXCHANGES, parse_symbol_exchange

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



@router.get("/lookup")
def instrument_lookup(
    request: Request,
    symbol: str = Query(..., description="Ticker symbol, e.g. ABC"),
    cs_only: bool = Query(default=True, description="Filter to Common Stocks only (type='CS')"),
    limit: int = Query(default=50, ge=1, le=500, description="Safety limit"),
    details: bool = Query(default=False, description="Include extra fields (industry names, website, location, etc.)"),
):
    """
    Lookup all instruments that match a symbol across exchanges.
    Uses instrument metadata. Returns symbol_exchange keys like IBM-N.
    """
    s = _norm_symbol(symbol)
    engine = get_market_engine()

    if not details:
        sql = text("""
            SELECT
                symbol,
                exchange,
                type,
                currency,
                name,
                shortname,
                gm_industry_id,
                x_sector_name
            FROM st_mast
            WHERE symbol = :symbol
              AND (:cs_only = 0 OR type = 'CS')
            ORDER BY exchange
            LIMIT :limit
        """)
    else:
        # Richer fields only when explicitly requested
        sql = text("""
            SELECT
                symbol,
                exchange,
                type,
                currency,
                name,
                shortname,
                gm_industry_id,
                x_sector_name,
                x_industry_group_name,
                x_industry_name,
                website,
                location,
                status,
                intrlstd,
                shares_os,
                dt_listed,
                dt_delisted
            FROM st_mast
            WHERE symbol = :symbol
              AND (:cs_only = 0 OR type = 'CS')
            ORDER BY exchange
            LIMIT :limit
        """)

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sql,
                {"symbol": s, "cs_only": 1 if cs_only else 0, "limit": limit},
            ).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "request_id": request.state.request_id,
                "error": "db_query_failed",
                "message": str(e),
            },
        )

    if not rows:
        raise HTTPException(
            status_code=404,
            detail={
                "request_id": request.state.request_id,
                "error": "symbol_not_found",
                "symbol": s,
            },
        )

    data = []
    for r in rows:
        d = dict(r)
        # Stable alias for clients/bots (optional but fine)
        d["industry_id"] = d.get("gm_industry_id")
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'
        data.append(d)

    return {
        "request_id": request.state.request_id,
        "symbol": s,
        "cs_only": cs_only,
        "details": details,
        "count": len(data),
        "data": data,
        "hint": "Use symbol_exchange (e.g. IBM-N) or symbol+exchange for /signals endpoints.",
    }


@router.get("/resolve")
def instrument_resolve(
    request: Request,
    symbol_exchange: str | None = Query(default=None, description="e.g., IBM-N"),
    symbol: str | None = Query(default=None, description="e.g., IBM"),
    exchange: str | None = Query(default=None, description="Exchange code: N,Q,A,B,T,I"),
    prefer_exchange: str = Query(default="N", description="Preferred exchange if symbol-only is ambiguous"),
    cs_only: bool = Query(default=True),
    details: bool = Query(default=False, description="Include extra fields (same as /lookup details)"),
):
    """
    Returns exactly one instrument if it can be resolved safely.
    If ambiguous, returns 409 with matches.
    """
    if not symbol_exchange and not symbol:
        raise HTTPException(
            status_code=400,
            detail={
                "request_id": request.state.request_id,
                "error": "missing_required_param",
                "message": "Provide symbol_exchange or symbol.",
            },
        )

    engine = get_market_engine()

    # Choose SQL based on details flag
    if not details:
        sql_one = text("""
            SELECT
                symbol, exchange, type, currency, name, shortname, gm_industry_id, x_sector_name
            FROM st_mast
            WHERE symbol = :symbol AND exchange = :exchange
              AND (:cs_only = 0 OR type = 'CS')
            LIMIT 1
        """)
        sql_many = text("""
            SELECT
                symbol, exchange, type, currency, name, shortname, gm_industry_id, x_sector_name
            FROM st_mast
            WHERE symbol = :symbol
              AND (:cs_only = 0 OR type = 'CS')
            ORDER BY exchange
        """)
    else:
        sql_one = text("""
            SELECT
                symbol, exchange, type, currency, name, shortname, gm_industry_id,
                x_sector_name, x_industry_group_name, x_industry_name, website, location,
                status, intrlstd, shares_os, dt_listed, dt_delisted
            FROM st_mast
            WHERE symbol = :symbol AND exchange = :exchange
              AND (:cs_only = 0 OR type = 'CS')
            LIMIT 1
        """)
        sql_many = text("""
            SELECT
                symbol, exchange, type, currency, name, shortname, gm_industry_id,
                x_sector_name, x_industry_group_name, x_industry_name, website, location,
                status, intrlstd, shares_os, dt_listed, dt_delisted
            FROM st_mast
            WHERE symbol = :symbol
              AND (:cs_only = 0 OR type = 'CS')
            ORDER BY exchange
        """)

    # 1) Resolve from symbol_exchange if provided
    if symbol_exchange:
        try:
            # expected to return (symbol, exchange)
            s, ex = parse_symbol_exchange(symbol_exchange)
        except ValueError as ve:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request.state.request_id,
                    "error": "invalid_symbol_exchange",
                    "message": str(ve),
                },
            )
        except Exception:
            # Fallback if parse_symbol_exchange behaves unexpectedly / isn't available
            if "-" not in symbol_exchange:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "request_id": request.state.request_id,
                        "error": "invalid_symbol_exchange",
                        "message": "symbol_exchange must look like 'IBM-N'",
                    },
                )
            s, ex = symbol_exchange.rsplit("-", 1)
            s = _norm_symbol(s)
            ex = _norm_exchange(ex)

        try:
            with engine.connect() as conn:
                row = conn.execute(
                    sql_one,
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
                    "error": "instrument_not_found",
                    "symbol_exchange": f"{s}-{ex}",
                },
            )

        d = dict(row)
        d["industry_id"] = d.get("gm_industry_id")
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'
        d["request_id"] = request.state.request_id
        return d

    # 2) Otherwise resolve by symbol (+ optional exchange)
    s = _norm_symbol(symbol)  # type: ignore[arg-type]
    ex = _norm_exchange(exchange) if exchange else None

    if ex:
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    sql_one,
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
                    "error": "instrument_not_found",
                    "symbol_exchange": f"{s}-{ex}",
                },
            )

        d = dict(row)
        d["industry_id"] = d.get("gm_industry_id")
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'
        d["request_id"] = request.state.request_id
        return d

    # 3) Symbol-only: check matches
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sql_many,
                {"symbol": s, "cs_only": 1 if cs_only else 0},
            ).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "request_id": request.state.request_id,
                "error": "db_query_failed",
                "message": str(e),
            },
        )

    if not rows:
        raise HTTPException(
            status_code=404,
            detail={
                "request_id": request.state.request_id,
                "error": "symbol_not_found",
                "symbol": s,
            },
        )

    # If unique, return it
    if len(rows) == 1:
        d = dict(rows[0])
        d["industry_id"] = d.get("gm_industry_id")
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'
        d["request_id"] = request.state.request_id
        return d

    # If ambiguous, try prefer_exchange only if it yields a single match
    pref = _norm_exchange(prefer_exchange)
    preferred = [r for r in rows if r["exchange"] == pref]
    if len(preferred) == 1:
        d = dict(preferred[0])
        d["industry_id"] = d.get("gm_industry_id")
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'
        d["resolved_by"] = "prefer_exchange"
        d["prefer_exchange"] = pref
        d["request_id"] = request.state.request_id
        return d

    matches = []
    for r in rows:
        d = dict(r)
        d["industry_id"] = d.get("gm_industry_id")
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'
        matches.append(d)

    raise HTTPException(
        status_code=409,
        detail={
            "request_id": request.state.request_id,
            "error": "ambiguous_symbol",
            "symbol": s,
            "matches": [{"symbol_exchange": m["symbol_exchange"], "exchange": m["exchange"], "type": m["type"]} for m in matches],
            "hint": "Specify exchange=... or use symbol_exchange=... to resolve uniquely.",
        },
    )
