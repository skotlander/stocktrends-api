# routers/market.py

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from api.routing import pre_payment_semantic_validator
from db import get_engine
from services import epoch_queries, regime_queries, regime_service
from services.market_semantics import BEARISH_TRENDS, BULLISH_TRENDS, NEUTRAL_TRENDS
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


router = APIRouter(prefix="/market", tags=["market"])

# Row bounds read from the shared table so runtime, OpenAPI and discovery cannot
# state different numbers. Existing values are unchanged: this endpoint returns
# the most recent eligible weeks and has never paged backwards through history.
REGIME_HISTORY_PATH = "/v1/market/regime/history"
REGIME_HISTORY_DEFAULT_LIMIT = history_default_limit(REGIME_HISTORY_PATH)
REGIME_HISTORY_MAX_LIMIT = history_max_limit(REGIME_HISTORY_PATH)
EPOCH_HISTORY_PATH = "/v1/market/epoch/history"
EPOCH_HISTORY_DEFAULT_LIMIT = history_default_limit(EPOCH_HISTORY_PATH)
EPOCH_HISTORY_MAX_LIMIT = history_max_limit(EPOCH_HISTORY_PATH)


def _no_signal_data(request: Request, message: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "request_id": getattr(request.state, "request_id", None),
            "error": "no_signal_data",
            "message": message,
        },
    )


def _number(row: Any, field: str) -> float:
    return float(row[field] or 0)


def _regime_snapshot(weekdate: date, rows: list[Any]) -> dict[str, Any] | None:
    """Build one market-regime response object from canonical aggregate rows."""
    bullish_count = 0
    bearish_count = 0
    neutral_count = 0
    observed_count = 0
    valid_rsi_sum = 0.0
    valid_rsi_count = 0
    mt_cnt_sum = 0.0
    mt_cnt_count = 0

    for row in rows:
        count = int(row["cnt"] or 0)
        trend = row["trend"] or ""
        observed_count += count

        if trend in BULLISH_TRENDS:
            bullish_count += count
        elif trend in BEARISH_TRENDS:
            bearish_count += count
        else:
            if trend in NEUTRAL_TRENDS:
                neutral_count += count
            continue

        # Maturity and valid RSI use the same classified population as regime.
        valid_rsi_sum += _number(row, "valid_rsi_sum")
        valid_rsi_count += int(row["valid_rsi_count"] or 0)
        mt_cnt_sum += _number(row, "mt_cnt_sum")
        mt_cnt_count += int(row["mt_cnt_count"] or 0)

    classified_count = bullish_count + bearish_count
    if classified_count == 0:
        return None

    score = regime_service.compute_regime_score(rows)
    if score is None:
        return None
    unclassified_count = observed_count - classified_count - neutral_count

    return {
        "regime": regime_service.classify_regime(score),
        "confidence": regime_service.classify_confidence(score),
        "regime_score": round(score, 4),
        "bullish_pct": round(bullish_count / classified_count, 4),
        "bearish_pct": round(bearish_count / classified_count, 4),
        "avg_rsi": (
            round(valid_rsi_sum / valid_rsi_count, 2)
            if valid_rsi_count
            else None
        ),
        "avg_mt_cnt": (
            round(mt_cnt_sum / mt_cnt_count, 2) if mt_cnt_count else None
        ),
        "weekdate": str(weekdate),
        "signal_count": classified_count,
        "classified_count": classified_count,
        "observed_count": observed_count,
        "neutral_count": neutral_count,
        "unclassified_count": unclassified_count,
        "population": "equities",
    }


def _epoch_snapshot(row: Any) -> dict[str, Any]:
    """Expose one persisted Epoch snapshot without recomputation or remapping."""
    return dict(row)


def _validate_epoch_history_date_range(request: Request, values: dict[str, Any]) -> None:
    """Reject an inverted request-only Epoch history range before payment."""
    start_date = values.get("start_date")
    end_date = values.get("end_date")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail={
                "request_id": getattr(request.state, "request_id", None),
                "error": "invalid_date_range",
                "message": "start_date must be before or equal to end_date.",
            },
        )


@router.get(
    "/epoch/latest",
    summary="Latest persisted Market Epoch v1 state",
    description=(
        "Returns the latest persisted snapshot from the frozen Market Epoch v1 unsupervised "
        "K=3 market-state classifier, based on six aggregate weekly Stock Trends features. "
        "Epoch labels (BROAD_BULLISH, BEARISH_MATURITY, and BULLISH_MATURITY) describe "
        "contextual market state; they are not trade signals, forward-return forecasts, or "
        "investment recommendations. changed_this_week is a persisted 0/1 flag indicating the assigned Epoch differs from "
        "the immediately previous official weekly Epoch; weeks_in_epoch is its consecutive "
        "weekly persistence. assigned_distance and second_nearest_distance are frozen-centroid "
        "distances in standardized feature space. separation_margin is their difference, not "
        "a probability or forecast confidence. The word Maturity does not imply a reversal must "
        "follow. Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def market_epoch_latest(request: Request):
    engine = get_engine()
    with engine.connect() as conn:
        row = epoch_queries.fetch_latest_epoch_row(conn)
    if row is None:
        raise _no_signal_data(request, "No persisted Market Epoch v1 snapshot is available.")
    return _epoch_snapshot(row)


@router.get(
    "/epoch/history",
    summary="Persisted Market Epoch v1 history",
    description=(
        "Returns newest-first persisted snapshots from the frozen Market Epoch v1 unsupervised "
        "K=3 market-state classifier, based on six aggregate weekly Stock Trends features. "
        "Epoch state is contextual, not a trade signal or forward-return forecast. "
        "separation_margin is the second-nearest frozen-centroid distance minus assigned "
        "distance in standardized feature space; it is not a probability or forecast confidence. "
        "The descriptive word Maturity does not imply a reversal must follow. "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
@pre_payment_semantic_validator(_validate_epoch_history_date_range)
def market_epoch_history(
    request: Request,
    limit: int = Query(
        default=EPOCH_HISTORY_DEFAULT_LIMIT,
        ge=1,
        le=EPOCH_HISTORY_MAX_LIMIT,
        description=(
            "Number of persisted weekly Epoch snapshots to return. Default 52, max 2600. "
            "The response reports the applied limit and whether more matching rows existed."
        ),
    ),
    start_date: date | None = Query(
        default=None,
        description="Optional inclusive earliest weekdate (YYYY-MM-DD).",
    ),
    end_date: date | None = Query(
        default=None,
        description="Optional inclusive latest weekdate (YYYY-MM-DD).",
    ),
):
    engine = get_engine()
    with engine.connect() as conn:
        probed_rows = epoch_queries.fetch_epoch_history_rows(
            conn,
            limit=probe_limit(limit),
            start_date=start_date,
            end_date=end_date,
        )
    rows, truncated_by_limit = split_probe_rows(probed_rows, limit)
    if not rows:
        raise _no_signal_data(request, "No persisted Market Epoch v1 snapshots are available.")
    history = [_epoch_snapshot(row) for row in rows]

    return {
        "history": history,
        "count": len(history),
        "limit": limit,
        "start_date": str(start_date) if start_date else None,
        "end_date": str(end_date) if end_date else None,
        "applied_bounds": build_applied_bounds(
            start=str(start_date) if start_date else None,
            end=str(end_date) if end_date else None,
            window_source=(
                WINDOW_SOURCE_CALLER
                if start_date or end_date
                else WINDOW_SOURCE_NOT_APPLIED
            ),
            default_window_weeks=None,
            limit=limit,
            limit_source=(
                LIMIT_SOURCE_CALLER
                if "limit" in request.query_params
                else LIMIT_SOURCE_DEFAULT
            ),
            max_limit=EPOCH_HISTORY_MAX_LIMIT,
            rows_returned=len(history),
            truncated_by_limit=truncated_by_limit,
            widen_with=(
                f"Raise limit up to {EPOCH_HISTORY_MAX_LIMIT} for more persisted weeks, "
                "and use inclusive start_date and end_date to select the period."
            ),
        ),
    }


@router.get(
    "/regime/latest",
    summary="Current market regime classification",
    description=(
        "Returns a canonical-equity market regime from classified Stock Trends trend "
        "codes. Population is CS and UN equities; bullish = {^+, ^-, v^}; bearish "
        "= {v-, v+, ^v}. Neutral and unknown states are reported separately and do "
        "not dilute regime_score. Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def market_regime_latest(request: Request):
    engine = get_engine()
    with engine.connect() as conn:
        weekdates = regime_queries.fetch_eligible_regime_weekdates(conn, limit=1)
        if not weekdates:
            raise _no_signal_data(request, "No classified canonical market weekdate available.")
        rows = regime_queries.fetch_regime_trend_aggregates(conn, weekdates=weekdates)

    snapshot = _regime_snapshot(weekdates[0], rows)
    if snapshot is None:
        raise _no_signal_data(request, "No classified signals found for the latest weekdate.")
    return snapshot


@router.get(
    "/regime/history",
    summary="Historical weekly market regime classification",
    description=(
        "Returns weekly canonical-equity market regime snapshots. Bullish and bearish "
        "percentages use classified directional trend states only; neutral and unknown "
        "states are transparent but excluded from the directional denominator. "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def market_regime_history(
    request: Request,
    limit: int = Query(
        default=REGIME_HISTORY_DEFAULT_LIMIT,
        ge=1,
        le=REGIME_HISTORY_MAX_LIMIT,
        description=(
            "Number of weekly periods to return. Default 12, max 52. The most recent "
            "eligible weeks are returned; the applied_bounds block on the response "
            "reports the limit used and whether more eligible weeks existed."
        ),
    ),
    start_date: date | None = Query(
        default=None,
        description=(
            "Optional earliest weekdate to include (YYYY-MM-DD). This filters which "
            "weeks are eligible; it does not move the window backwards. With the 52-week "
            "ceiling the endpoint covers recent regime history, not an arbitrary period."
        ),
    ),
):
    engine = get_engine()
    with engine.connect() as conn:
        # Trim the probe week before aggregation, so observing truncation costs
        # one extra weekdate lookup and no extra aggregation work.
        probed_weekdates = regime_queries.fetch_eligible_regime_weekdates(
            conn, limit=probe_limit(limit), start_date=start_date
        )
        weekdates, truncated_by_limit = split_probe_rows(probed_weekdates, limit)
        if not weekdates:
            raise _no_signal_data(request, "No classified canonical market weekdates available.")
        aggregate_rows = regime_queries.fetch_regime_trend_aggregates(
            conn, weekdates=weekdates
        )

    rows_by_week: dict[date, list[Any]] = defaultdict(list)
    for row in aggregate_rows:
        rows_by_week[row["weekdate"]].append(row)
    history = [
        snapshot
        for weekdate in weekdates
        if (snapshot := _regime_snapshot(weekdate, rows_by_week.get(weekdate, [])))
        is not None
    ]

    return {
        "history": history,
        "applied_bounds": build_applied_bounds(
            # This endpoint has an earliest-eligible-week filter and no end bound,
            # so `start` carries start_date and `end` is genuinely absent rather
            # than defaulted to something the endpoint does not support.
            start=str(start_date) if start_date else None,
            end=None,
            window_source=(
                WINDOW_SOURCE_CALLER if start_date else WINDOW_SOURCE_NOT_APPLIED
            ),
            default_window_weeks=None,
            limit=limit,
            limit_source=(
                LIMIT_SOURCE_CALLER
                if "limit" in request.query_params
                else LIMIT_SOURCE_DEFAULT
            ),
            max_limit=REGIME_HISTORY_MAX_LIMIT,
            rows_returned=len(history),
            truncated_by_limit=truncated_by_limit,
            widen_with=(
                "Raise limit up to "
                f"{REGIME_HISTORY_MAX_LIMIT} for more weeks, and use start_date to set "
                "the earliest eligible week. The most recent eligible weeks are returned; "
                "this endpoint does not page backwards through history."
            ),
        ),
        "count": len(history),
        "limit": limit,
        "start_date": str(start_date) if start_date else None,
    }


@router.get(
    "/regime/forecast",
    summary="Forward-looking market regime forecast",
    description=(
        "Returns a deterministic forward-looking canonical-equity regime outlook based "
        "on the direction and consistency of classified weekly regime scores. "
        "Fully deterministic — no ML. "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def market_regime_forecast(
    request: Request,
    lookback: int = Query(
        default=5,
        ge=2,
        le=13,
        description="Number of recent weeks to analyze. Default 5, min 2, max 13.",
    ),
):
    engine = get_engine()
    with engine.connect() as conn:
        weekdates = regime_queries.fetch_eligible_regime_weekdates(conn, limit=lookback)
        if not weekdates:
            raise _no_signal_data(request, "No classified canonical market weekdates available.")
        aggregate_rows = regime_queries.fetch_regime_trend_aggregates(
            conn, weekdates=weekdates
        )

    scores_by_week = regime_service.compute_scores_by_week(weekdates, aggregate_rows)
    if not scores_by_week:
        raise _no_signal_data(request, "Signal count is zero for the resolved weekdates.")

    # Derive forecast signals — scores_by_week is most recent first.
    forecast = regime_service.compute_forecast_signals(scores_by_week)
    scores = [score for _, score in scores_by_week]
    current_weekdate, current_score = scores_by_week[0]
    current_label = regime_service.classify_regime(current_score)
    # Consistency: fraction of lookback weeks carrying the same regime label.
    consistency_count = sum(
        1 for score in scores if regime_service.classify_regime(score) == current_label
    )
    consistency_pct = consistency_count / len(scores)

    return {
        "forecast_regime": forecast["forecast_regime"],
        "forecast_confidence": regime_service.forecast_confidence(
            consistency_pct, current_score, forecast["avg_delta"]
        ),
        "current_regime": current_label,
        "current_regime_score": round(current_score, 4),
        "recent_direction": forecast["recent_direction"],
        "regime_consistency": round(consistency_pct, 4),
        "projected_regime_score": round(forecast["projected_score"], 4),
        "avg_weekly_score_delta": round(forecast["avg_delta"], 4),
        "recent_scores": [round(score, 4) for score in scores],
        "weeks_analyzed": len(scores_by_week),
        "lookback": lookback,
        "weekdate": str(current_weekdate),
    }
