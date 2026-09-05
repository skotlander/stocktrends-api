# routers/indicators.py

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from api.routing import pre_payment_semantic_validator
from db import get_engine
from utils.history_bounds import (
    LIMIT_SOURCE_CALLER,
    LIMIT_SOURCE_DEFAULT,
    WINDOW_SOURCE_CALLER,
    WINDOW_SOURCE_NOT_APPLIED,
    build_applied_bounds,
    history_default_limit,
    history_max_limit,
    probe_limit,
    split_probe_rows,
)

from routers.signals import VALID_EXCHANGES, parse_symbol_exchange

router = APIRouter(prefix="/indicators", tags=["indicators"])


# Row bounds for this endpoint, read from the shared table so the runtime
# Query, the OpenAPI schema derived from it, and the discovery registry cannot
# state different numbers. Existing values are unchanged.
HISTORY_PATH = "/v1/indicators/history"
HISTORY_DEFAULT_LIMIT = history_default_limit(HISTORY_PATH)
HISTORY_MAX_LIMIT = history_max_limit(HISTORY_PATH)


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
    # Strict to avoid ambiguity (same approach as prices)
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


def _validate_symbol_exchange_values(request: Request, values: dict) -> None:
    """
    Pre-payment adapter over `_resolve_symbol_exchange`.

    Same shape as prices and ST-IM: whether the caller named an instrument is
    request-only and moves ahead of payment; whether that instrument has
    indicator rows is discovered by the paid query and does not.

    The adapter maps solved values into the shared resolver and holds no rule of
    its own, so the 400 a client sees is byte-for-byte the resolver's.
    """
    _resolve_symbol_exchange(
        request=request,
        symbol_exchange=values.get("symbol_exchange"),
        symbol=values.get("symbol"),
        exchange=values.get("exchange"),
    )



@router.get("/latest")
@pre_payment_semantic_validator(_validate_symbol_exchange_values)
def indicators_latest(
    request: Request,
    symbol_exchange: str | None = Query(default=None, description="e.g., IBM-N"),
    symbol: str | None = Query(default=None, description="e.g., IBM"),
    exchange: str | None = Query(default=None, description="Exchange code: N,Q,A,B,T,I"),
    cs_only: bool = Query(default=True, description="Filter to Common Stocks only (type='CS')"),
):
    """
    Latest weekly Stock Trends indicators for a specific instrument.
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
            trend,
            trend_cnt,
            mt_cnt,
            prev_mtcnt,
            rsi,
            rsi_updn,
            vol_tag,
            rvol,
            atv,
            fpr_chg1,
            fpr_chg2,
            fpr_chg4,
            fpr_chg13,
            fpr_chg40,
            pr_chg13,
            pr_change,
            shortavg,
            longavg,
            yr_hi,
            yr_lo
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
                "error": "indicators_not_found",
                "symbol_exchange": f"{s}-{ex}",
            },
        )

    d = dict(row)
    d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'
    d["request_id"] = request.state.request_id
    return d


@router.get("/history")
@pre_payment_semantic_validator(_validate_symbol_exchange_values)
def indicators_history(
    request: Request,
    symbol_exchange: str | None = Query(default=None, description="e.g., IBM-N"),
    symbol: str | None = Query(default=None, description="e.g., IBM"),
    exchange: str | None = Query(default=None, description="Exchange code: N,Q,A,B,T,I"),
    cs_only: bool = Query(default=True, description="Filter to Common Stocks only (type='CS')"),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD (inclusive)"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD (inclusive)"),
    limit: int = Query(
        default=HISTORY_DEFAULT_LIMIT,
        ge=1,
        le=HISTORY_MAX_LIMIT,
        description=(
            "Max rows to return. This endpoint applies no default date window; the "
            "applied_bounds block on the response reports the limit that was used and "
            "whether the result was truncated."
        ),
    ),
):
    """
    Weekly Stock Trends indicator history for a specific instrument.
    Returns rows ascending by weekdate.
    """
    s, ex = _resolve_symbol_exchange(
        request=request,
        symbol_exchange=symbol_exchange,
        symbol=symbol,
        exchange=exchange,
    )

    where_dates = ""
    # One row beyond the limit, so truncation is observed rather than inferred
    # from a row count that happens to equal the limit. The probe row is dropped
    # before the response is built, so the caller never receives more than
    # `limit` rows and the existing ordering is untouched.
    params = {
        "symbol": s,
        "exchange": ex,
        "cs_only": 1 if cs_only else 0,
        "limit": probe_limit(limit),
    }

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
            trend,
            trend_cnt,
            mt_cnt,
            prev_mtcnt,
            rsi,
            rsi_updn,
            vol_tag,
            rvol,
            atv,
            fpr_chg1,
            fpr_chg2,
            fpr_chg4,
            fpr_chg13,
            fpr_chg40,
            pr_chg13,
            pr_change,
            shortavg,
            longavg,
            yr_hi,
            yr_lo
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

    bounded_rows, truncated_by_limit = split_probe_rows(list(rows), limit)
    data = [dict(r) for r in reversed(bounded_rows)]
    for d in data:
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'

    return {
        "request_id": request.state.request_id,
        "symbol_exchange": f"{s}-{ex}",
        "cs_only": cs_only,
        "start": start,
        "end": end,
        "applied_bounds": build_applied_bounds(
            start=start,
            end=end,
            window_source=(
                WINDOW_SOURCE_CALLER if (start or end) else WINDOW_SOURCE_NOT_APPLIED
            ),
            default_window_weeks=None,
            limit=limit,
            limit_source=(
                LIMIT_SOURCE_CALLER
                if "limit" in request.query_params
                else LIMIT_SOURCE_DEFAULT
            ),
            max_limit=HISTORY_MAX_LIMIT,
            rows_returned=len(data),
            truncated_by_limit=truncated_by_limit,
            widen_with=(
                "Supply start and/or end to select a range, and raise limit up to "
                f"{HISTORY_MAX_LIMIT} for more rows. This endpoint applies no default "
                "date window."
            ),
        ),
        "count": len(data),
        "data": data,
    }
