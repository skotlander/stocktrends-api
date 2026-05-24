# routers/decision.py

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

from db import get_engine
from routers.signals import VALID_EXCHANGES, parse_symbol_exchange
from services import decision_service, regime_service

router = APIRouter(prefix="/decision", tags=["decision"])

_FORECAST_LOOKBACK = 5


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class EvaluateSymbolRequest(BaseModel):
    """
    Accepts either:
      - symbol_exchange: "AAPL-Q"
      - symbol + exchange: "AAPL", "Q"
    """
    symbol_exchange: str | None = None
    symbol: str | None = None
    exchange: str | None = None


def _signal_notes(
    rsi_updn: str | None,
    vol_tag: str | None,
    trend_cnt: int,
    mt_cnt: int,
) -> list[str]:
    notes: list[str] = []
    if rsi_updn == "U":
        notes.append("RSI improving week-over-week")
    elif rsi_updn == "D":
        notes.append("RSI weakening week-over-week")
    if vol_tag == "HV":
        notes.append("High volume signal present")
    elif vol_tag == "LV":
        notes.append("Low volume — reduced conviction")
    if trend_cnt >= 8:
        notes.append(f"Mature trend state ({trend_cnt} weeks)")
    elif trend_cnt == 1:
        notes.append("New trend state — first week")
    if mt_cnt >= 12:
        notes.append(f"Long-standing major trend ({mt_cnt} weeks)")
    return notes


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/evaluate-symbol",
    summary="Symbol-level decision evaluation",
    description=(
        "Evaluates a single symbol's current trend context against the live market regime "
        "to produce a synthesized bias, confidence score, and decision_score (0–1). "
        "Fully deterministic — no ML. Uses the same regime logic as /market/regime/forecast. "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def evaluate_symbol(body: EvaluateSymbolRequest, request: Request):
    request_id = getattr(request.state, "request_id", None)

    # --- Resolve symbol + exchange ---
    try:
        if body.symbol_exchange:
            s, ex = parse_symbol_exchange(body.symbol_exchange)
        elif body.symbol and body.exchange:
            s = body.symbol.strip().upper()
            ex = body.exchange.strip().upper()
            if ex not in VALID_EXCHANGES:
                raise ValueError(
                    f"Invalid exchange '{ex}'. Must be one of {sorted(VALID_EXCHANGES)}"
                )
            if not s:
                raise ValueError("symbol is empty")
        elif body.symbol:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request_id,
                    "error": "missing_exchange",
                    "message": "Provide exchange alongside symbol, or use symbol_exchange (e.g. 'AAPL-Q').",
                },
            )
        else:
            raise HTTPException(
                status_code=422,
                detail={
                    "request_id": request_id,
                    "error": "missing_symbol",
                    "message": "Provide symbol_exchange (e.g. 'AAPL-Q') or symbol + exchange.",
                },
            )
    except ValueError as ve:
        raise HTTPException(
            status_code=400,
            detail={"request_id": request_id, "error": "invalid_input", "message": str(ve)},
        )

    engine = get_engine()

    with engine.connect() as conn:

        # --- Step 1: Resolve most recent N weekdates for regime + latest_wd for symbol ---
        weekdate_rows = conn.execute(
            text(
                """
                SELECT DISTINCT weekdate
                FROM st_data
                WHERE type = 'CS'
                ORDER BY weekdate DESC
                LIMIT :limit
                """
            ),
            {"limit": _FORECAST_LOOKBACK},
        ).mappings().all()

        weekdates = [r["weekdate"] for r in weekdate_rows if r["weekdate"]]
        if not weekdates:
            raise HTTPException(
                status_code=503,
                detail={
                    "request_id": request_id,
                    "error": "no_signal_data",
                    "message": "No Stock Trends weekdates available.",
                },
            )

        latest_wd = weekdates[0]

        # --- Step 2: Symbol lookup for the latest weekdate ---
        sym_row = conn.execute(
            text(
                """
                SELECT
                    symbol,
                    exchange,
                    trend,
                    trend_cnt,
                    mt_cnt,
                    rsi,
                    rsi_updn,
                    vol_tag,
                    weekdate
                FROM st_data
                WHERE symbol   = :symbol
                  AND exchange = :exchange
                  AND weekdate = :weekdate
                  AND type     = 'CS'
                LIMIT 1
                """
            ),
            {"symbol": s, "exchange": ex, "weekdate": latest_wd},
        ).mappings().first()

        if sym_row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "request_id": request_id,
                    "error": "symbol_not_found",
                    "message": (
                        f"No signal found for {s}-{ex} on weekdate {latest_wd}. "
                        "Verify symbol and exchange are correct."
                    ),
                },
            )

        # --- Step 3: Regime aggregation for all lookback weekdates ---
        week_binds = {f"w{i}": wd for i, wd in enumerate(weekdates)}
        placeholders = ", ".join(f":w{i}" for i in range(len(weekdates)))
        agg_rows = conn.execute(
            text(
                f"""
                SELECT
                    weekdate,
                    trend,
                    COUNT(*) AS cnt
                FROM st_data
                WHERE weekdate IN ({placeholders})
                  AND type = 'CS'
                GROUP BY weekdate, trend
                ORDER BY weekdate DESC, trend
                """
            ),
            week_binds,
        ).mappings().all()

    # --- Compute regime context ---
    scores_by_week = regime_service.compute_scores_by_week(weekdates, agg_rows)
    if not scores_by_week:
        raise HTTPException(
            status_code=503,
            detail={
                "request_id": request_id,
                "error": "no_signal_data",
                "message": "Cannot compute regime score for the resolved weekdates.",
            },
        )

    forecast = regime_service.compute_forecast_signals(scores_by_week)
    scores = [s_val for _, s_val in scores_by_week]
    _, current_regime_score = scores_by_week[0]
    current_regime = regime_service.classify_regime(current_regime_score)
    regime_confidence = regime_service.classify_confidence(current_regime_score)

    consistency_count = sum(
        1 for sv in scores if regime_service.classify_regime(sv) == current_regime
    )
    consistency_pct = consistency_count / len(scores)

    fc_confidence = regime_service.forecast_confidence(
        consistency_pct, current_regime_score, forecast["avg_delta"]
    )

    # --- Compute symbol decision ---
    trend = sym_row["trend"] or ""
    trend_cnt = int(sym_row["trend_cnt"] or 0)
    mt_cnt = int(sym_row["mt_cnt"] or 0)
    rsi = int(sym_row["rsi"] or 0)
    rsi_updn = sym_row["rsi_updn"]
    vol_tag = sym_row["vol_tag"]

    sym_bias = decision_service.symbol_bias(trend)
    sym_alignment = decision_service.alignment(sym_bias, current_regime)
    bias = decision_service.compute_bias(sym_bias, sym_alignment)
    confidence = decision_service.decision_confidence(sym_alignment, sym_bias, trend_cnt, rsi, current_regime_score)
    d_score = decision_service.decision_score(sym_alignment, sym_bias, trend_cnt, rsi, current_regime_score)
    notes = _signal_notes(rsi_updn, vol_tag, trend_cnt, mt_cnt)

    return {
        "request_id": request_id,
        "symbol": s,
        "exchange": ex,
        "weekdate": str(latest_wd),
        "bias": bias,
        "confidence": confidence,
        "decision_score": d_score,
        "alignment": sym_alignment,
        "symbol_context": {
            "trend": trend,
            "trend_cnt": trend_cnt,
            "mt_cnt": mt_cnt,
            "rsi": rsi,
            "rsi_updn": rsi_updn,
            "vol_tag": vol_tag,
            "symbol_bias": sym_bias,
        },
        "regime_context": {
            "current_regime": current_regime,
            "regime_score": round(current_regime_score, 4),
            "regime_confidence": regime_confidence,
            "forecast_regime": forecast["forecast_regime"],
            "forecast_confidence": fc_confidence,
            "recent_direction": forecast["recent_direction"],
            "regime_consistency": round(consistency_pct, 4),
            "weeks_analyzed": len(scores_by_week),
        },
        "signal_notes": notes,
    }
