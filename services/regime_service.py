# services/regime_service.py
#
# Shared regime scoring and forecast logic.
# Consumed by routers/market.py and routers/decision.py.
# No SQL here — callers pass pre-fetched rows.

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from services.market_semantics import BEARISH_TRENDS, BULLISH_TRENDS

DIRECTION_THRESHOLD: float = 0.02


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def classify_regime(regime_score: float) -> str:
    if regime_score >= 0.10:
        return "bullish"
    if regime_score <= -0.10:
        return "bearish"
    return "mixed"


def classify_confidence(regime_score: float) -> str:
    abs_score = abs(regime_score)
    if abs_score >= 0.30:
        return "high"
    if abs_score >= 0.10:
        return "moderate"
    return "low"


def recent_direction(avg_delta: float) -> str:
    if avg_delta >= DIRECTION_THRESHOLD:
        return "improving"
    if avg_delta <= -DIRECTION_THRESHOLD:
        return "deteriorating"
    return "stable"


def forecast_confidence(
    consistency_pct: float,
    current_score: float,
    avg_delta: float,
) -> str:
    if (
        consistency_pct >= 0.80
        and abs(current_score) >= 0.20
        and abs(avg_delta) >= DIRECTION_THRESHOLD
    ):
        return "high"
    if consistency_pct >= 0.60:
        return "moderate"
    return "low"


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

def compute_regime_score(rows: list[Any]) -> float | None:
    """
    Given a list of rows with 'cnt' and 'trend' keys,
    compute regime_score = (bullish_cnt - bearish_cnt) / classified_cnt.
    Neutral and unknown trend states are excluded. Returns None if no rows are
    classified as bullish or bearish.
    """
    bullish_cnt = 0
    bearish_cnt = 0
    classified_cnt = 0
    for row in rows:
        cnt = int(row["cnt"] or 0)
        trend = row["trend"] or ""
        if trend in BULLISH_TRENDS:
            bullish_cnt += cnt
            classified_cnt += cnt
        elif trend in BEARISH_TRENDS:
            bearish_cnt += cnt
            classified_cnt += cnt
    if classified_cnt == 0:
        return None
    return (bullish_cnt - bearish_cnt) / classified_cnt


def compute_scores_by_week(
    weekdates: list[date],
    agg_rows: list[Any],
) -> list[tuple[date, float]]:
    """
    Group agg_rows (which have 'weekdate', 'cnt', 'trend') by weekdate,
    compute regime_score per week, return list of (weekdate, score)
    in the same order as weekdates (most recent first), skipping empty weeks.
    """
    groups: dict[date, list] = defaultdict(list)
    for row in agg_rows:
        groups[row["weekdate"]].append(row)

    result: list[tuple[date, float]] = []
    for wd in weekdates:
        score = compute_regime_score(groups.get(wd, []))
        if score is not None:
            result.append((wd, score))
    return result


def compute_forecast_signals(
    scores_by_week: list[tuple[date, float]],
) -> dict:
    """
    Derive forward-looking forecast metrics from a series of weekly scores
    (most recent first).

    Returns dict with:
        avg_delta, projected_score, forecast_regime, recent_direction
    """
    scores = [s for _, s in scores_by_week]
    deltas = [scores[i] - scores[i + 1] for i in range(len(scores) - 1)]
    avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
    projected_score = max(-1.0, min(1.0, scores[0] + avg_delta))
    return {
        "avg_delta": avg_delta,
        "projected_score": projected_score,
        "forecast_regime": classify_regime(projected_score),
        "recent_direction": recent_direction(avg_delta),
    }
