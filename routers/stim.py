# routers/stim.py

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

router = APIRouter(prefix="/stim", tags=["stim"])


# Row bounds read from the shared table so runtime, OpenAPI and discovery
# cannot state different numbers. Existing values are unchanged.
HISTORY_PATH = "/v1/stim/history"
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

    Only the request-only half of this endpoint's validity moves forward: whether
    the caller named an instrument at all, and whether the exchange code names a
    real exchange.  Everything the answer depends on — whether an ST-IM estimate
    exists for the instrument, and whether it is stale against the latest market
    week — is discovered by the paid query and stays behind the payment gate.

    The adapter maps solved values into the shared resolver and holds no rule of
    its own, so the 400 a client sees is byte-for-byte the resolver's.
    """
    _resolve_symbol_exchange(
        request=request,
        symbol_exchange=values.get("symbol_exchange"),
        symbol=values.get("symbol"),
        exchange=values.get("exchange"),
    )


def _fetch_latest_weekdate_st_data(engine, symbol: str, exchange: str):
    """
    Latest weekdate present in st_data for this instrument.
    Used to determine whether ST-IM means are stale/missing for the latest market week.
    """
    sql = text("""
        SELECT MAX(weekdate) AS weekdate
        FROM st_data
        WHERE symbol = :symbol
          AND exchange = :exchange
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"symbol": symbol, "exchange": exchange}).mappings().first()
    return row["weekdate"] if row else None


@router.get(
    "/latest",
    summary="Latest ST-IM return distributions for a symbol",
    description=(
        "Returns the latest Stock Trends Inference Model (ST-IM) outputs for a given symbol. "
        "ST-IM is the current baseline inference provider, not the final intelligence layer. "
        "Outputs are forward return expectations and statistical distributions across 4-week, "
        "13-week, and 40-week horizons. Fields: xNwk1 = lower percentile/confidence bound, "
        "xNwk2 = upper percentile/confidence bound, xNwk = expected return (mean), "
        "xNwksd = standard deviation. Interpret probabilities as conditional historical "
        "tendencies under uncertainty, not guarantees, price targets, or direct buy/sell commands. "
        "Includes staleness detection (is_stale, missing_reason) for weeks where insufficient "
        "sample data prevented ST-IM estimation. "
        "Fetch /v1/meta/inference and /v1/meta/stim before interpretation. "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
@pre_payment_semantic_validator(_validate_symbol_exchange_values)
def stim_latest(
    request: Request,
    symbol_exchange: str | None = Query(default=None, description="e.g., IBM-N"),
    symbol: str | None = Query(default=None, description="e.g., IBM"),
    exchange: str | None = Query(default=None, description="Exchange code: N,Q,A,B,T,I"),
):
    s, ex = _resolve_symbol_exchange(
        request=request,
        symbol_exchange=symbol_exchange,
        symbol=symbol,
        exchange=exchange,
    )

    sql_latest_means = text("""
        SELECT
            weekdate,
            exchange,
            symbol,
            x4wk1, x4wk2, x4wk, x4wksd,
            x13wk1, x13wk2, x13wk, x13wksd,
            x40wk1, x40wk2, x40wk, x40wksd
        FROM st_returnmeans
        WHERE symbol = :symbol
          AND exchange = :exchange
        ORDER BY weekdate DESC
        LIMIT 1
    """)

    engine = get_engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(sql_latest_means, {"symbol": s, "exchange": ex}).mappings().first()

        # Compare with latest market weekdate for this instrument in st_data
        latest_data_week = _fetch_latest_weekdate_st_data(engine, s, ex)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    if not row:
        # No ST-IM means exist at all for this instrument
        raise HTTPException(
            status_code=404,
            detail={
                "request_id": request.state.request_id,
                "error": "stim_not_found",
                "symbol_exchange": f"{s}-{ex}",
                "message": "No ST-IM return distribution record exists for this instrument.",
            },
        )

    d = dict(row)
    d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'
    d["request_id"] = request.state.request_id

    # Staleness detection: if latest market week exists and is newer than returned ST-IM week
    d_week = d.get("weekdate")
    d["latest_data_weekdate"] = (str(latest_data_week) if latest_data_week else None)

    is_stale = False
    if latest_data_week and d_week and d_week < latest_data_week:
        is_stale = True

    d["is_stale"] = is_stale
    if is_stale:
        d["missing_reason"] = "insufficient_sample"  # denotes missing ST-IM estimate for latest week
        d["missing_weekdate"] = str(latest_data_week)
    else:
        d["missing_reason"] = None
        d["missing_weekdate"] = None

    return d


@router.get(
    "/history",
    summary="Historical ST-IM return distribution series for a symbol",
    description=(
        "Returns a historical series of Stock Trends Inference Model (ST-IM) forward return "
        "distribution records for a given symbol/exchange. ST-IM is the current baseline "
        "inference provider and should be interpreted through the provider-agnostic "
        "/v1/meta/inference contract plus the /v1/meta/stim provider profile. Each record "
        "contains expected returns and standard deviations for 4-week, 13-week, and 40-week "
        "horizons (xNwk, xNwksd, xNwk1, xNwk2). Rows returned ascending by weekdate. "
        "Set include_gaps=true to identify weeks where ST-IM estimates are absent "
        "(insufficient sample versus the latest available market week). "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
@pre_payment_semantic_validator(_validate_symbol_exchange_values)
def stim_history(
    request: Request,
    symbol_exchange: str | None = Query(default=None, description="e.g., IBM-N"),
    symbol: str | None = Query(default=None, description="e.g., IBM"),
    exchange: str | None = Query(default=None, description="Exchange code: N,Q,A,B,T,I"),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD (inclusive)"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD (inclusive)"),
    limit: int = Query(
        default=HISTORY_DEFAULT_LIMIT,
        ge=1,
        le=HISTORY_MAX_LIMIT,
        description=(
            "Safety limit. This endpoint applies no default date window; the "
            "applied_bounds block on the response reports the limit that was used and "
            "whether the result was truncated."
        ),
    ),
    include_gaps: bool = Query(
        default=False,
        description="If true, include missing weekdates versus the available market weeks within start/end (may be slower).",
    ),
):
    s, ex = _resolve_symbol_exchange(
        request=request,
        symbol_exchange=symbol_exchange,
        symbol=symbol,
        exchange=exchange,
    )

    where_dates = ""
    # One row beyond the limit so truncation is observed, not inferred. The probe
    # row is dropped before the response — and before gap inference below — so the
    # caller never sees more than `limit` rows.
    params: dict = {"symbol": s, "exchange": ex, "limit": probe_limit(limit)}

    if start:
        where_dates += " AND weekdate >= :start"
        params["start"] = start
    if end:
        where_dates += " AND weekdate <= :end"
        params["end"] = end

    sql_hist = text(f"""
        SELECT
            weekdate,
            exchange,
            symbol,
            x4wk1, x4wk2, x4wk, x4wksd,
            x13wk1, x13wk2, x13wk, x13wksd,
            x40wk1, x40wk2, x40wk, x40wksd
        FROM st_returnmeans
        WHERE symbol = :symbol
          AND exchange = :exchange
          {where_dates}
        ORDER BY weekdate DESC
        LIMIT :limit
    """)

    engine = get_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql_hist, params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    bounded_rows, truncated_by_limit = split_probe_rows(list(rows), limit)
    data = [dict(r) for r in reversed(bounded_rows)]
    for d in data:
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'

    gaps = None
    if include_gaps:
        # Determine comparison window: use requested start/end if given, else infer from returned rows.
        # If no rows, we can still compute gaps from st_data, but that might be large; keep it safe.
        inferred_start = start
        inferred_end = end

        if not inferred_start and data:
            inferred_start = str(data[0]["weekdate"])
        if not inferred_end and data:
            inferred_end = str(data[-1]["weekdate"])

        # If we still can't bound it, refuse gap computation to avoid heavy scans.
        if not inferred_start or not inferred_end:
            gaps = []
        else:
            try:
                sql_data_weeks = text("""
                    SELECT DISTINCT weekdate
                    FROM st_data
                    WHERE symbol = :symbol
                      AND exchange = :exchange
                      AND weekdate >= :start
                      AND weekdate <= :end
                    ORDER BY weekdate ASC
                """)
                sql_means_weeks = text("""
                    SELECT DISTINCT weekdate
                    FROM st_returnmeans
                    WHERE symbol = :symbol
                      AND exchange = :exchange
                      AND weekdate >= :start
                      AND weekdate <= :end
                    ORDER BY weekdate ASC
                """)

                with engine.connect() as conn:
                    data_weeks = conn.execute(
                        sql_data_weeks,
                        {"symbol": s, "exchange": ex, "start": inferred_start, "end": inferred_end},
                    ).scalars().all()
                    means_weeks = conn.execute(
                        sql_means_weeks,
                        {"symbol": s, "exchange": ex, "start": inferred_start, "end": inferred_end},
                    ).scalars().all()

                data_set = set(data_weeks)
                means_set = set(means_weeks)
                missing = sorted(list(data_set - means_set))

                gaps = [str(w) for w in missing]
            except Exception as e:
                # Don't fail the whole request—just report that gaps couldn't be computed
                gaps = []
                # Optionally, you could include a warning field; keeping minimal here.

    return {
        "request_id": request.state.request_id,
        "symbol_exchange": f"{s}-{ex}",
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
        "include_gaps": include_gaps,
        "gaps": gaps,
    }
