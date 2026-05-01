# routers/portfolio.py

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from db import get_engine
from routers.signals import VALID_EXCHANGES, parse_symbol_exchange
from services import decision_service, regime_service

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

_FORECAST_LOOKBACK = 5
_VALID_UNIVERSES = {"top"}
_VALID_BIASES = {"auto", "bullish", "bearish"}
_WEIGHT_SUM_TOLERANCE = 0.01
_MAX_POSITIONS = 25


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class ConstructPortfolioRequest(BaseModel):
    universe: str = "top"
    count: int = Field(default=5, ge=1, le=10)
    bias: str = "auto"
    exchange: str | None = None


# ---------------------------------------------------------------------------
# Portfolio helpers
# ---------------------------------------------------------------------------

def _resolve_trend_codes(bias: str, current_regime: str) -> list[str]:
    """
    Return the sorted list of trend codes to pass to the candidate SQL IN clause.
    bias='auto' defers to current_regime; mixed regime includes all directional codes.
    """
    if bias == "bullish":
        return sorted(regime_service.BULLISH_TRENDS)
    if bias == "bearish":
        return sorted(regime_service.BEARISH_TRENDS)
    # auto
    if current_regime == "bullish":
        return sorted(regime_service.BULLISH_TRENDS)
    if current_regime == "bearish":
        return sorted(regime_service.BEARISH_TRENDS)
    # mixed regime — include all directional trends; decision_score will still
    # penalise divergent symbols through the alignment component
    return sorted(regime_service.BULLISH_TRENDS | regime_service.BEARISH_TRENDS)


def _bias_resolved(bias: str, current_regime: str) -> str:
    if bias != "auto":
        return bias
    return current_regime  # "bullish", "bearish", or "mixed"


def _construction_notes(
    bias: str,
    resolved_bias: str,
    current_regime: str,
    current_regime_score: float,
    candidates_evaluated: int,
    requested_count: int,
    filled_count: int,
    weight: float,
) -> list[str]:
    notes: list[str] = []

    if bias == "auto":
        notes.append(
            f"Bias auto-resolved to {resolved_bias} based on current regime "
            f"(score: {round(current_regime_score, 4)})"
        )

    if bias != "auto" and bias != current_regime and current_regime != "mixed":
        notes.append(
            f"Requested bias ({bias}) opposes current regime ({current_regime}); "
            "expect lower decision_scores and divergent alignments"
        )

    trend_desc = (
        "bullish trend states" if resolved_bias == "bullish"
        else "bearish trend states" if resolved_bias == "bearish"
        else "all directional trend states"
    )
    notes.append(f"{candidates_evaluated} candidates evaluated across {trend_desc}")

    if filled_count < requested_count:
        notes.append(
            f"Only {filled_count} of {requested_count} requested positions filled "
            f"— insufficient candidates after filtering"
        )
    else:
        notes.append(f"{filled_count} of {requested_count} requested positions filled")

    notes.append(f"Equal weight: {weight} per position")
    return notes


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/construct",
    summary="Construct a scored equal-weight portfolio",
    description=(
        "Generates an equal-weight portfolio by evaluating all eligible candidates from the "
        "latest Stock Trends weekly signal data through the decision scoring system and "
        "selecting the top N by decision_score DESC. Fully deterministic — no ML. "
        "Reuses the same regime and decision logic as /market/regime/forecast and "
        "/decision/evaluate-symbol.\n\n"
        "Request parameters:\n"
        "  universe (string, default='top'): Candidate universe. Allowed: 'top'. "
        "Selects securities with directional Stock Trends trend classifications resolved "
        "from bias.\n"
        "  count (integer 1-10, default=5): Number of equal-weight positions returned.\n"
        "  bias (string, default='auto'): Direction filter. Allowed: 'auto', 'bullish', "
        "'bearish'. auto resolves from current regime — bullish regime selects bullish "
        "trend states (^+, ^-, v^); bearish regime selects bearish trend states "
        "(v-, v+, ^v); mixed regime selects all directional trend states. "
        "'bullish' or 'bearish' force the trend state filter regardless of regime.\n"
        "  exchange (string, optional): Exchange filter. Allowed: 'N' (NYSE), "
        "'Q' (NASDAQ), 'A' (AMEX), 'T' (TSX). Omitted defaults to US exchanges N/Q/A.\n\n"
        "Key response fields:\n"
        "  decision_score (0-1): Ranking metric for each position.\n"
        "  portfolio_score: Mean decision_score of selected positions.\n"
        "  bias_requested / bias_resolved: Requested and effective bias direction.\n"
        "  candidates_evaluated: Total candidates scored before top-N selection.\n"
        "  candidate_selection_method: How the candidate universe was assembled.\n"
        "  candidate_ordering: Sort key applied to rank candidates before selection.\n"
        "  exchange_filter: Exchange codes applied during candidate selection.\n"
        "  universe: Universe parameter used.\n"
        "  regime_context: Current regime, confidence, and forecast.\n"
        "  construction_notes: Human-readable construction summary.\n\n"
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def construct_portfolio(body: ConstructPortfolioRequest, request: Request):
    request_id = getattr(request.state, "request_id", None)

    # --- Validate request ---
    if body.universe not in _VALID_UNIVERSES:
        raise HTTPException(
            status_code=400,
            detail={
                "request_id": request_id,
                "error": "invalid_universe",
                "value": body.universe,
                "valid": sorted(_VALID_UNIVERSES),
            },
        )

    if body.bias not in _VALID_BIASES:
        raise HTTPException(
            status_code=400,
            detail={
                "request_id": request_id,
                "error": "invalid_bias",
                "value": body.bias,
                "valid": sorted(_VALID_BIASES),
            },
        )

    norm_exchange: str | None = None
    if body.exchange:
        norm_exchange = body.exchange.strip().upper()
        if norm_exchange not in VALID_EXCHANGES:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request_id,
                    "error": "invalid_exchange",
                    "value": body.exchange,
                    "valid": sorted(VALID_EXCHANGES),
                },
            )

    engine = get_engine()

    with engine.connect() as conn:

        # --- Query 1: Resolve most recent 5 weekdates ---
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
                    "message": "No weekdates available in st_data.",
                },
            )

        latest_wd = weekdates[0]

        # --- Query 2: Regime aggregation over the 5-week lookback ---
        # Placeholders built from DB-returned date objects — no user input in SQL
        week_binds = {f"w{i}": wd for i, wd in enumerate(weekdates)}
        week_placeholders = ", ".join(f":w{i}" for i in range(len(weekdates)))
        agg_rows = conn.execute(
            text(
                f"""
                SELECT
                    weekdate,
                    trend,
                    COUNT(*) AS cnt
                FROM st_data
                WHERE weekdate IN ({week_placeholders})
                  AND type = 'CS'
                GROUP BY weekdate, trend
                ORDER BY weekdate DESC, trend
                """
            ),
            week_binds,
        ).mappings().all()

        # Compute regime in Python before resolving trend codes
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

        _, current_regime_score = scores_by_week[0]
        current_regime = regime_service.classify_regime(current_regime_score)

        # Resolve bias → trend codes for candidate SQL
        trend_codes = _resolve_trend_codes(body.bias, current_regime)
        trend_binds = {f"t{i}": code for i, code in enumerate(trend_codes)}
        trend_placeholders = ", ".join(f":t{i}" for i in range(len(trend_codes)))

        # --- Query 3: Full eligible candidate universe ---
        # Fetch all eligible candidates; Python ranks by decision_score DESC.
        # No pre-limit — alphabetical ordering must never act as a selection filter.
        candidate_params: dict = {
            "latest_wd": latest_wd,
            **trend_binds,
        }
        # Exchange filter: explicit exchange overrides the US-market default.
        # Default (no exchange): restrict to US-listed exchanges (N, Q, A).
        # Allowed exchange codes: N (NYSE), Q (NASDAQ), A (AMEX), T (TSX).
        # type = 'CS' already excludes non-tradable instrument types.
        if norm_exchange:
            exchange_clause = "AND exchange = :exchange"
            candidate_params["exchange"] = norm_exchange
        else:
            exchange_clause = "AND exchange IN ('N', 'Q', 'A')"

        candidate_rows = conn.execute(
            text(
                f"""
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
                WHERE weekdate = :latest_wd
                  AND type = 'CS'
                  AND trend IN ({trend_placeholders})
                  {exchange_clause}
                """
            ),
            candidate_params,
        ).mappings().all()

        # --- Query 4: ST-IM risk-adjusted returns for tiebreaking ---
        # Both the MAX(weekdate) subquery and the outer WHERE use the same exchange
        # scope so that a newer weekdate from a different exchange universe cannot
        # cause the in-scope rows to return empty.
        stim_params: dict = {}
        if norm_exchange:
            stim_exchange_clause = "AND exchange = :stim_exchange"
            stim_max_clause = "WHERE exchange = :stim_exchange"
            stim_params["stim_exchange"] = norm_exchange
        else:
            stim_exchange_clause = "AND exchange IN ('N', 'Q', 'A')"
            stim_max_clause = "WHERE exchange IN ('N', 'Q', 'A')"

        stim_rows = conn.execute(
            text(
                f"""
                SELECT symbol, exchange, x13wk, x13wksd, weekdate
                FROM st_returnmeans
                WHERE weekdate = (
                    SELECT MAX(weekdate) FROM st_returnmeans {stim_max_clause}
                )
                  {stim_exchange_clause}
                """
            ),
            stim_params,
        ).mappings().all()

    stim_weekdate = stim_rows[0]["weekdate"] if stim_rows else None
    stim_map: dict = {(r["symbol"], r["exchange"]): r for r in stim_rows}
    # Stale when STIM hasn't been updated for the latest st_data week
    stim_is_stale = stim_weekdate is None or stim_weekdate < latest_wd

    if not candidate_rows:
        raise HTTPException(
            status_code=422,
            detail={
                "request_id": request_id,
                "error": "insufficient_candidates",
                "message": (
                    f"No candidates found for bias='{body.bias}'"
                    + (f", exchange='{norm_exchange}'" if norm_exchange else "")
                    + f" on weekdate {latest_wd}."
                ),
            },
        )

    # --- Compute regime context (Python, no more SQL) ---
    forecast = regime_service.compute_forecast_signals(scores_by_week)
    scores = [s_val for _, s_val in scores_by_week]
    regime_confidence = regime_service.classify_confidence(current_regime_score)

    consistency_count = sum(
        1 for sv in scores if regime_service.classify_regime(sv) == current_regime
    )
    consistency_pct = consistency_count / len(scores)

    fc_confidence = regime_service.forecast_confidence(
        consistency_pct, current_regime_score, forecast["avg_delta"]
    )

    # --- Evaluate each candidate in-process ---
    evaluated: list[dict] = []
    for row in candidate_rows:
        trend = row["trend"] or ""
        trend_cnt = int(row["trend_cnt"] or 0)
        mt_cnt = int(row["mt_cnt"] or 0)
        rsi = int(row["rsi"] or 0)

        sym_bias = decision_service.symbol_bias(trend)
        sym_alignment = decision_service.alignment(sym_bias, current_regime)
        bias_label = decision_service.compute_bias(sym_bias, sym_alignment)
        confidence = decision_service.decision_confidence(
            sym_alignment, sym_bias, trend_cnt, rsi, current_regime_score
        )
        d_score = decision_service.decision_score(
            sym_alignment, sym_bias, trend_cnt, rsi, current_regime_score
        )

        # --- Attach ST-IM fields ---
        stim_entry = stim_map.get((row["symbol"], row["exchange"]))
        stim_covered = False
        stim_expected: float | None = None
        stim_vol: float | None = None
        stim_ra: float | None = None

        if not stim_is_stale and stim_entry is not None:
            x13wk = stim_entry["x13wk"]
            x13wksd = stim_entry["x13wksd"]
            if x13wk is not None and x13wksd is not None and float(x13wksd) != 0.0:
                stim_covered = True
                stim_expected = round(float(x13wk), 4)
                stim_vol = round(float(x13wksd), 4)
                stim_ra = round(float(x13wk) / float(x13wksd), 4)

        evaluated.append({
            "symbol": row["symbol"],
            "exchange": row["exchange"],
            "symbol_exchange": f'{row["symbol"]}-{row["exchange"]}',
            "trend": trend,
            "trend_cnt": trend_cnt,
            "mt_cnt": mt_cnt,
            "rsi": rsi,
            "bias": bias_label,
            "confidence": confidence,
            "decision_score": d_score,
            "stim_expected_return_13wk": stim_expected,
            "stim_volatility_13wk": stim_vol,
            "stim_risk_adjusted_13wk": stim_ra,
            "stim_percentile_13wk": None,
            "stim_covered": stim_covered,
        })

    # --- Compute ST-IM percentile ranks (winsorize 1/99%, normalize [0,1]) ---
    covered = [c for c in evaluated if c["stim_covered"]]
    if covered:
        ra_vals = [c["stim_risk_adjusted_13wk"] for c in covered]
        sorted_vals = sorted(ra_vals)
        n = len(sorted_vals)
        lo = sorted_vals[max(0, int(0.01 * n))]
        hi = sorted_vals[min(n - 1, int(0.99 * n))]
        winsorized = [max(lo, min(hi, v)) for v in ra_vals]
        w_min, w_max = min(winsorized), max(winsorized)
        for cand, wv in zip(covered, winsorized):
            cand["stim_percentile_13wk"] = (
                0.5 if w_max == w_min
                else round((wv - w_min) / (w_max - w_min), 4)
            )

    # --- Rank: decision_score DESC, ST-IM percentile DESC (tiebreaker), symbol ASC ---
    evaluated.sort(key=lambda x: (
        -x["decision_score"],
        -(x["stim_percentile_13wk"] if x["stim_covered"] else -1.0),
        x["symbol"],
    ))

    # --- Select top N and assign equal weight ---
    selected = evaluated[: body.count]
    weight = round(1.0 / len(selected), 4) if selected else 0.0

    portfolio = [
        {"rank": i + 1, "weight": weight, **item}
        for i, item in enumerate(selected)
    ]

    # --- Portfolio score = mean decision_score of selected positions ---
    portfolio_score = round(
        sum(p["decision_score"] for p in portfolio) / len(portfolio), 4
    ) if portfolio else 0.0

    stim_covered_count = sum(1 for c in evaluated if c["stim_covered"])
    stim_coverage_pct = round(stim_covered_count / len(evaluated), 4) if evaluated else 0.0
    ranking_method = (
        "decision_score_stim_tiebreak" if stim_covered_count > 0
        else "decision_score_only"
    )
    candidate_ordering = (
        "decision_score DESC, stim_percentile_13wk DESC, symbol ASC"
        if stim_covered_count > 0
        else "decision_score DESC, symbol ASC"
    )

    resolved_bias = _bias_resolved(body.bias, current_regime)
    notes = _construction_notes(
        bias=body.bias,
        resolved_bias=resolved_bias,
        current_regime=current_regime,
        current_regime_score=current_regime_score,
        candidates_evaluated=len(candidate_rows),
        requested_count=body.count,
        filled_count=len(selected),
        weight=weight,
    )

    return {
        "request_id": request_id,
        "weekdate": str(latest_wd),
        "portfolio": portfolio,
        "count": len(portfolio),
        "universe": body.universe,
        "exchange_filter": [norm_exchange] if norm_exchange else ["N", "Q", "A"],
        "candidates_evaluated": len(candidate_rows),
        "candidate_selection_method": "full_eligible_universe",
        "candidate_ordering": candidate_ordering,
        "portfolio_score": portfolio_score,
        "bias_requested": body.bias,
        "bias_resolved": resolved_bias,
        "stim_weekdate": str(stim_weekdate) if stim_weekdate else None,
        "stim_covered_count": stim_covered_count,
        "stim_coverage_pct": stim_coverage_pct,
        "ranking_method": ranking_method,
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
        "construction_notes": notes,
    }


# ---------------------------------------------------------------------------
# Evaluate endpoint — request schema
# ---------------------------------------------------------------------------

class EvaluatePortfolioPosition(BaseModel):
    symbol_exchange: str   # "AAPL-Q"
    weight: float          # Must be > 0


class EvaluatePortfolioRequest(BaseModel):
    positions: list[EvaluatePortfolioPosition] = Field(min_length=1, max_length=_MAX_POSITIONS)


# ---------------------------------------------------------------------------
# Evaluate endpoint — aggregate helpers
# ---------------------------------------------------------------------------

_CONF_MAP = {"high": 1.0, "moderate": 0.5, "low": 0.0}
_ALIGN_MAP = {"aligned": 1.0, "neutral": 0.0, "divergent": -1.0}


def _agg_portfolio_bias(internal: list[dict]) -> str:
    """
    Score-weighted directional aggregation using normalized weights.
    bullish_score = sum(norm_weight * decision_score) for bullish positions
    bearish_score = sum(norm_weight * decision_score) for bearish positions
    net >= 0.05 → bullish, net <= -0.05 → bearish, else → mixed
    """
    bullish_score = sum(
        item["norm_weight"] * item["decision_score"]
        for item in internal if item["sym_bias"] == "bullish"
    )
    bearish_score = sum(
        item["norm_weight"] * item["decision_score"]
        for item in internal if item["sym_bias"] == "bearish"
    )
    net = bullish_score - bearish_score
    if net >= 0.05:
        return "bullish"
    if net <= -0.05:
        return "bearish"
    return "mixed"


def _agg_portfolio_confidence(internal: list[dict]) -> str:
    """Weighted mean of confidence scores mapped back to tier."""
    mean_conf = sum(item["norm_weight"] * _CONF_MAP[item["confidence"]] for item in internal)
    if mean_conf >= 0.75:
        return "high"
    if mean_conf >= 0.40:
        return "moderate"
    return "low"


def _agg_portfolio_alignment(internal: list[dict]) -> str:
    """Weighted mean of alignment values mapped back to label."""
    mean_align = sum(item["norm_weight"] * _ALIGN_MAP[item["alignment"]] for item in internal)
    if mean_align >= 0.50:
        return "aligned"
    if mean_align <= -0.50:
        return "divergent"
    return "mixed"


def _evaluation_notes(
    positions_missing: int,
    positions_total: int,
    found_weight_sum: float,
    latest_wd: str,
    portfolio_alignment: str,
    current_regime: str,
) -> list[str]:
    notes: list[str] = []
    if positions_missing > 0:
        notes.append(
            f"{positions_missing} of {positions_total} positions not found in st_data "
            f"for weekdate {latest_wd} — excluded from aggregates"
        )
        notes.append(
            f"Portfolio aggregates computed on {positions_total - positions_missing} positions "
            f"representing {round(found_weight_sum, 4)} of submitted weight"
        )
    notes.append(
        f"Portfolio is {portfolio_alignment} with current {current_regime} regime"
    )
    return notes


# ---------------------------------------------------------------------------
# Evaluate endpoint — handler
# ---------------------------------------------------------------------------

@router.post(
    "/evaluate",
    summary="Evaluate a user-supplied portfolio",
    description=(
        "Accepts a list of symbol-weight pairs and evaluates each position using the "
        "same decision scoring and regime logic as /decision/evaluate-symbol. "
        "Returns position-level and portfolio-level aggregates. "
        "Weights must be positive and sum to 1.0 (±0.01 tolerance). "
        "Duplicates are rejected. Missing symbols are included with found=false "
        "and excluded from portfolio aggregates. "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def evaluate_portfolio(body: EvaluatePortfolioRequest, request: Request):
    request_id = getattr(request.state, "request_id", None)

    # --- Step 1: Parse and validate all positions before any DB access ---
    parsed: list[tuple[str, str, float]] = []
    seen: set[tuple[str, str]] = set()

    for pos in body.positions:
        try:
            s, ex = parse_symbol_exchange(pos.symbol_exchange)
        except ValueError as ve:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request_id,
                    "error": "invalid_input",
                    "message": str(ve),
                },
            )

        key = (s, ex)
        if key in seen:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request_id,
                    "error": "duplicate_positions",
                    "message": f"Duplicate position: {s}-{ex}",
                },
            )
        seen.add(key)

        if pos.weight <= 0:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request_id,
                    "error": "invalid_weights",
                    "message": f"Weight for {s}-{ex} must be > 0, got {pos.weight}",
                },
            )

        parsed.append((s, ex, pos.weight))

    weight_sum = sum(w for _, _, w in parsed)
    if not (1.0 - _WEIGHT_SUM_TOLERANCE <= weight_sum <= 1.0 + _WEIGHT_SUM_TOLERANCE):
        raise HTTPException(
            status_code=400,
            detail={
                "request_id": request_id,
                "error": "invalid_weights",
                "message": (
                    f"Weights must sum to 1.0 (±{_WEIGHT_SUM_TOLERANCE}), "
                    f"got {round(weight_sum, 6)}"
                ),
            },
        )

    engine = get_engine()

    with engine.connect() as conn:

        # --- Query 1: Resolve most recent 5 weekdates ---
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
                    "message": "No weekdates available in st_data.",
                },
            )

        latest_wd = weekdates[0]

        # --- Query 2: Regime aggregation over the 5-week lookback ---
        # Placeholders built from DB-returned date objects — no user input in SQL
        week_binds = {f"w{i}": wd for i, wd in enumerate(weekdates)}
        week_placeholders = ", ".join(f":w{i}" for i in range(len(weekdates)))
        agg_rows = conn.execute(
            text(
                f"""
                SELECT
                    weekdate,
                    trend,
                    COUNT(*) AS cnt
                FROM st_data
                WHERE weekdate IN ({week_placeholders})
                  AND type = 'CS'
                GROUP BY weekdate, trend
                ORDER BY weekdate DESC, trend
                """
            ),
            week_binds,
        ).mappings().all()

        # --- Query 3: Batch symbol lookup —
        # OR clauses built from parse_symbol_exchange-validated strings only
        or_parts = " OR ".join(
            f"(symbol = :s{i} AND exchange = :e{i})"
            for i in range(len(parsed))
        )
        sym_binds: dict = {"latest_wd": latest_wd}
        for i, (s, ex, _) in enumerate(parsed):
            sym_binds[f"s{i}"] = s
            sym_binds[f"e{i}"] = ex

        sym_rows = conn.execute(
            text(
                f"""
                SELECT
                    symbol,
                    exchange,
                    trend,
                    trend_cnt,
                    mt_cnt,
                    rsi,
                    weekdate
                FROM st_data
                WHERE weekdate = :latest_wd
                  AND type = 'CS'
                  AND ({or_parts})
                """
            ),
            sym_binds,
        ).mappings().all()

    # Index symbol lookup by (symbol, exchange)
    sym_lookup: dict[tuple[str, str], object] = {
        (r["symbol"], r["exchange"]): r for r in sym_rows
    }

    # --- Compute regime context (Python, no more SQL) ---
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

    # --- Evaluate each position in-process ---
    evaluated: list[dict] = []   # response positions
    internal: list[dict] = []    # found positions only — used for aggregates

    for s, ex, weight in parsed:
        row = sym_lookup.get((s, ex))
        if row is None:
            evaluated.append({
                "symbol": s,
                "exchange": ex,
                "symbol_exchange": f"{s}-{ex}",
                "weight": weight,
                "trend": None,
                "trend_cnt": None,
                "mt_cnt": None,
                "rsi": None,
                "bias": None,
                "confidence": None,
                "decision_score": None,
                "alignment": None,
                "found": False,
            })
        else:
            trend = row["trend"] or ""
            trend_cnt = int(row["trend_cnt"] or 0)
            mt_cnt = int(row["mt_cnt"] or 0)
            rsi = int(row["rsi"] or 0)

            sym_bias = decision_service.symbol_bias(trend)
            sym_alignment = decision_service.alignment(sym_bias, current_regime)
            bias_label = decision_service.compute_bias(sym_bias, sym_alignment)
            confidence = decision_service.decision_confidence(
                sym_alignment, sym_bias, trend_cnt, rsi, current_regime_score
            )
            d_score = decision_service.decision_score(
                sym_alignment, sym_bias, trend_cnt, rsi, current_regime_score
            )

            evaluated.append({
                "symbol": s,
                "exchange": ex,
                "symbol_exchange": f"{s}-{ex}",
                "weight": weight,
                "trend": trend,
                "trend_cnt": trend_cnt,
                "mt_cnt": mt_cnt,
                "rsi": rsi,
                "bias": bias_label,
                "confidence": confidence,
                "decision_score": d_score,
                "alignment": sym_alignment,
                "found": True,
            })
            internal.append({
                "weight": weight,
                "decision_score": d_score,
                "sym_bias": sym_bias,
                "alignment": sym_alignment,
                "confidence": confidence,
            })

    if not internal:
        raise HTTPException(
            status_code=422,
            detail={
                "request_id": request_id,
                "error": "all_positions_missing",
                "message": (
                    f"None of the {len(parsed)} submitted positions were found "
                    f"in st_data for weekdate {latest_wd}."
                ),
            },
        )

    # --- Normalize weights and compute portfolio aggregates ---
    found_weight_sum = sum(item["weight"] for item in internal)
    for item in internal:
        item["norm_weight"] = item["weight"] / found_weight_sum

    portfolio_score = round(
        sum(item["norm_weight"] * item["decision_score"] for item in internal), 4
    )
    portfolio_bias = _agg_portfolio_bias(internal)
    portfolio_confidence = _agg_portfolio_confidence(internal)
    portfolio_alignment = _agg_portfolio_alignment(internal)

    positions_missing = len(evaluated) - len(internal)
    notes = _evaluation_notes(
        positions_missing=positions_missing,
        positions_total=len(evaluated),
        found_weight_sum=found_weight_sum,
        latest_wd=str(latest_wd),
        portfolio_alignment=portfolio_alignment,
        current_regime=current_regime,
    )

    return {
        "request_id": request_id,
        "weekdate": str(latest_wd),
        "positions": evaluated,
        "positions_found": len(internal),
        "positions_missing": positions_missing,
        "effective_weight": round(found_weight_sum, 4),
        "portfolio_score": portfolio_score,
        "portfolio_bias": portfolio_bias,
        "portfolio_confidence": portfolio_confidence,
        "portfolio_alignment": portfolio_alignment,
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
        "evaluation_notes": notes,
    }


# ---------------------------------------------------------------------------
# Compare endpoint — constants
# ---------------------------------------------------------------------------

_TIE_THRESHOLD = 0.001
_ALIGN_RANK = {"aligned": 2, "mixed": 1, "divergent": 0}
_CONF_RANK  = {"high": 2, "moderate": 1, "low": 0}


# ---------------------------------------------------------------------------
# Compare endpoint — request schema
# ---------------------------------------------------------------------------

class ComparePortfolioRequest(BaseModel):
    left:  list[EvaluatePortfolioPosition] = Field(min_length=1, max_length=_MAX_POSITIONS)
    right: list[EvaluatePortfolioPosition] = Field(min_length=1, max_length=_MAX_POSITIONS)


# ---------------------------------------------------------------------------
# Compare endpoint — private helpers
# ---------------------------------------------------------------------------

def _parse_and_validate_positions(
    positions: list[EvaluatePortfolioPosition],
    side: str,
    request_id: object,
) -> list[tuple[str, str, float]]:
    """
    Parse and validate one side of a compare request.
    Raises HTTPException with "side" in detail on any failure.
    Returns list of (symbol, exchange, weight) tuples.
    """
    parsed: list[tuple[str, str, float]] = []
    seen: set[tuple[str, str]] = set()

    for pos in positions:
        try:
            s, ex = parse_symbol_exchange(pos.symbol_exchange)
        except ValueError as ve:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request_id,
                    "error": "invalid_input",
                    "side": side,
                    "message": str(ve),
                },
            )

        key = (s, ex)
        if key in seen:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request_id,
                    "error": "duplicate_positions",
                    "side": side,
                    "message": f"Duplicate position in {side}: {s}-{ex}",
                },
            )
        seen.add(key)

        if pos.weight <= 0:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": request_id,
                    "error": "invalid_weights",
                    "side": side,
                    "message": f"Weight for {s}-{ex} must be > 0, got {pos.weight}",
                },
            )

        parsed.append((s, ex, pos.weight))

    weight_sum = sum(w for _, _, w in parsed)
    if not (1.0 - _WEIGHT_SUM_TOLERANCE <= weight_sum <= 1.0 + _WEIGHT_SUM_TOLERANCE):
        raise HTTPException(
            status_code=400,
            detail={
                "request_id": request_id,
                "error": "invalid_weights",
                "side": side,
                "message": (
                    f"Weights for {side} must sum to 1.0 (±{_WEIGHT_SUM_TOLERANCE}), "
                    f"got {round(weight_sum, 6)}"
                ),
            },
        )

    return parsed


def _evaluate_positions_helper(
    parsed: list[tuple[str, str, float]],
    sym_lookup: dict,
    current_regime: str,
    current_regime_score: float,
) -> tuple[list[dict], list[dict]]:
    """
    Evaluate a parsed position list against a pre-fetched sym_lookup.
    Returns (evaluated, internal):
      evaluated — response-ready dicts, all positions including not-found
      internal  — found positions only, with decision fields, for aggregation
    """
    evaluated: list[dict] = []
    internal: list[dict] = []

    for s, ex, weight in parsed:
        row = sym_lookup.get((s, ex))
        if row is None:
            evaluated.append({
                "symbol": s,
                "exchange": ex,
                "symbol_exchange": f"{s}-{ex}",
                "weight": weight,
                "trend": None,
                "trend_cnt": None,
                "mt_cnt": None,
                "rsi": None,
                "bias": None,
                "confidence": None,
                "decision_score": None,
                "alignment": None,
                "found": False,
            })
        else:
            trend = row["trend"] or ""
            trend_cnt = int(row["trend_cnt"] or 0)
            mt_cnt = int(row["mt_cnt"] or 0)
            rsi = int(row["rsi"] or 0)

            sym_bias = decision_service.symbol_bias(trend)
            sym_alignment = decision_service.alignment(sym_bias, current_regime)
            bias_label = decision_service.compute_bias(sym_bias, sym_alignment)
            confidence = decision_service.decision_confidence(
                sym_alignment, sym_bias, trend_cnt, rsi, current_regime_score
            )
            d_score = decision_service.decision_score(
                sym_alignment, sym_bias, trend_cnt, rsi, current_regime_score
            )

            evaluated.append({
                "symbol": s,
                "exchange": ex,
                "symbol_exchange": f"{s}-{ex}",
                "weight": weight,
                "trend": trend,
                "trend_cnt": trend_cnt,
                "mt_cnt": mt_cnt,
                "rsi": rsi,
                "bias": bias_label,
                "confidence": confidence,
                "decision_score": d_score,
                "alignment": sym_alignment,
                "found": True,
            })
            internal.append({
                "weight": weight,
                "decision_score": d_score,
                "sym_bias": sym_bias,
                "alignment": sym_alignment,
                "confidence": confidence,
            })

    return evaluated, internal


def _build_portfolio_summary(
    evaluated: list[dict],
    internal: list[dict],
    latest_wd: str,
    current_regime: str,
) -> dict:
    """
    Compute per-portfolio aggregates from an evaluated + internal pair.
    Caller must ensure internal is non-empty before calling.
    """
    positions_missing = len(evaluated) - len(internal)
    found_weight_sum = sum(item["weight"] for item in internal)

    for item in internal:
        item["norm_weight"] = item["weight"] / found_weight_sum

    portfolio_score     = round(sum(item["norm_weight"] * item["decision_score"] for item in internal), 4)
    portfolio_bias      = _agg_portfolio_bias(internal)
    portfolio_confidence= _agg_portfolio_confidence(internal)
    portfolio_alignment = _agg_portfolio_alignment(internal)

    notes = _evaluation_notes(
        positions_missing=positions_missing,
        positions_total=len(evaluated),
        found_weight_sum=found_weight_sum,
        latest_wd=latest_wd,
        portfolio_alignment=portfolio_alignment,
        current_regime=current_regime,
    )

    return {
        "positions":         evaluated,
        "positions_found":   len(internal),
        "positions_missing": positions_missing,
        "effective_weight":  round(found_weight_sum, 4),
        "portfolio_score":   portfolio_score,
        "portfolio_bias":    portfolio_bias,
        "portfolio_confidence": portfolio_confidence,
        "portfolio_alignment":  portfolio_alignment,
        "evaluation_notes":  notes,
    }


def _compare_summary(
    left: dict,
    right: dict,
    left_parsed: list[tuple[str, str, float]],
    right_parsed: list[tuple[str, str, float]],
) -> dict:
    """Build the comparison block from two portfolio summaries."""
    left_score  = left["portfolio_score"]
    right_score = right["portfolio_score"]
    score_delta = round(left_score - right_score, 4)

    # score_advantage — raw direction only
    score_advantage = (
        "left"  if left_score > right_score else
        "right" if right_score > left_score else
        "tie"
    )

    # alignment_advantage
    la = _ALIGN_RANK[left["portfolio_alignment"]]
    ra = _ALIGN_RANK[right["portfolio_alignment"]]
    alignment_advantage = "left" if la > ra else "right" if ra > la else "tie"

    # confidence_advantage
    lc = _CONF_RANK[left["portfolio_confidence"]]
    rc = _CONF_RANK[right["portfolio_confidence"]]
    confidence_advantage = "left" if lc > rc else "right" if rc > lc else "tie"

    # winner — score first, alignment tiebreak
    if abs(score_delta) >= _TIE_THRESHOLD:
        winner = "left" if score_delta > 0 else "right"
    elif alignment_advantage != "tie":
        winner = alignment_advantage
    else:
        winner = "tie"

    effective_weight_delta = round(left["effective_weight"] - right["effective_weight"], 4)

    # overlap — submitted positions (all, not only found)
    left_keys  = {(s, ex) for s, ex, _ in left_parsed}
    right_keys = {(s, ex) for s, ex, _ in right_parsed}
    overlap_symbols = sorted(f"{s}-{ex}" for s, ex in left_keys & right_keys)

    return {
        "winner":                winner,
        "score_delta":           score_delta,
        "score_advantage":       score_advantage,
        "alignment_advantage":   alignment_advantage,
        "confidence_advantage":  confidence_advantage,
        "effective_weight_delta": effective_weight_delta,
        "overlap_count":         len(overlap_symbols),
        "overlap_symbols":       overlap_symbols,
    }


def _build_comparison_notes(
    winner: str,
    score_delta: float,
    left: dict,
    right: dict,
    overlap_count: int,
    overlap_symbols: list[str],
    current_regime: str,
) -> list[str]:
    notes: list[str] = []

    left_score  = left["portfolio_score"]
    right_score = right["portfolio_score"]
    abs_delta   = abs(score_delta)

    if abs_delta < _TIE_THRESHOLD:
        notes.append(
            f"Scores within tie threshold: left {left_score} vs right {right_score}"
        )
    elif score_delta > 0:
        notes.append(
            f"Left scores {abs_delta:.4f} higher than right ({left_score} vs {right_score})"
        )
    else:
        notes.append(
            f"Right scores {abs_delta:.4f} higher than left ({right_score} vs {left_score})"
        )

    left_align  = left["portfolio_alignment"]
    right_align = right["portfolio_alignment"]
    if left_align != right_align:
        better = "left" if _ALIGN_RANK[left_align] > _ALIGN_RANK[right_align] else "right"
        notes.append(
            f"Left alignment: {left_align} vs right alignment: {right_align} "
            f"— {better} is better aligned with current {current_regime} regime"
        )
    else:
        notes.append(
            f"Both portfolios share {left_align} alignment with current {current_regime} regime"
        )

    if overlap_count > 0:
        notes.append(
            f"{overlap_count} position(s) overlap between portfolios: {', '.join(overlap_symbols)}"
        )
    else:
        notes.append("No overlapping positions between portfolios")

    if left["positions_missing"] > 0:
        notes.append(
            f"Left: {left['positions_missing']} position(s) not found "
            f"— effective weight {left['effective_weight']}"
        )
    if right["positions_missing"] > 0:
        notes.append(
            f"Right: {right['positions_missing']} position(s) not found "
            f"— effective weight {right['effective_weight']}"
        )

    return notes


# ---------------------------------------------------------------------------
# Compare endpoint — handler
# ---------------------------------------------------------------------------

@router.post(
    "/compare",
    summary="Compare two user-supplied portfolios",
    description=(
        "Accepts two portfolios (left and right) and evaluates both using the same "
        "decision scoring and regime logic as /portfolio/evaluate. "
        "Both portfolios share one DB round-trip: weekdates, regime aggregation, "
        "and a combined symbol lookup for the union of all submitted positions. "
        "Returns per-portfolio results in the /portfolio/evaluate shape, plus a "
        "structured comparison block and comparison notes. "
        "Fetch /v1/pricing/catalog for current STC cost."
    ),
)
def compare_portfolios(body: ComparePortfolioRequest, request: Request):
    request_id = getattr(request.state, "request_id", None)

    # --- Step 1: Parse and validate both sides before any DB access ---
    left_parsed  = _parse_and_validate_positions(body.left,  "left",  request_id)
    right_parsed = _parse_and_validate_positions(body.right, "right", request_id)

    engine = get_engine()

    with engine.connect() as conn:

        # --- Query 1: Resolve most recent 5 weekdates ---
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
                    "message": "No weekdates available in st_data.",
                },
            )

        latest_wd = weekdates[0]

        # --- Query 2: Shared regime aggregation ---
        # Placeholders built from DB-returned date objects — no user input in SQL
        week_binds = {f"w{i}": wd for i, wd in enumerate(weekdates)}
        week_placeholders = ", ".join(f":w{i}" for i in range(len(weekdates)))
        agg_rows = conn.execute(
            text(
                f"""
                SELECT
                    weekdate,
                    trend,
                    COUNT(*) AS cnt
                FROM st_data
                WHERE weekdate IN ({week_placeholders})
                  AND type = 'CS'
                GROUP BY weekdate, trend
                ORDER BY weekdate DESC, trend
                """
            ),
            week_binds,
        ).mappings().all()

        # --- Query 3: Combined batch symbol lookup for left ∪ right ---
        # Sorted for deterministic bind ordering — no user input in SQL structure
        all_pairs = sorted(
            {(s, ex) for s, ex, _ in left_parsed} | {(s, ex) for s, ex, _ in right_parsed}
        )
        or_parts = " OR ".join(
            f"(symbol = :s{i} AND exchange = :e{i})"
            for i in range(len(all_pairs))
        )
        sym_binds: dict = {"latest_wd": latest_wd}
        for i, (s, ex) in enumerate(all_pairs):
            sym_binds[f"s{i}"] = s
            sym_binds[f"e{i}"] = ex

        sym_rows = conn.execute(
            text(
                f"""
                SELECT
                    symbol,
                    exchange,
                    trend,
                    trend_cnt,
                    mt_cnt,
                    rsi,
                    weekdate
                FROM st_data
                WHERE weekdate = :latest_wd
                  AND type = 'CS'
                  AND ({or_parts})
                """
            ),
            sym_binds,
        ).mappings().all()

    sym_lookup: dict[tuple[str, str], object] = {
        (r["symbol"], r["exchange"]): r for r in sym_rows
    }

    # --- Compute shared regime context ---
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
    regime_scores = [s_val for _, s_val in scores_by_week]
    _, current_regime_score = scores_by_week[0]
    current_regime   = regime_service.classify_regime(current_regime_score)
    regime_confidence = regime_service.classify_confidence(current_regime_score)

    consistency_count = sum(
        1 for sv in regime_scores if regime_service.classify_regime(sv) == current_regime
    )
    consistency_pct = consistency_count / len(regime_scores)
    fc_confidence = regime_service.forecast_confidence(
        consistency_pct, current_regime_score, forecast["avg_delta"]
    )

    # --- Evaluate both sides in-process ---
    left_evaluated,  left_internal  = _evaluate_positions_helper(
        left_parsed,  sym_lookup, current_regime, current_regime_score
    )
    right_evaluated, right_internal = _evaluate_positions_helper(
        right_parsed, sym_lookup, current_regime, current_regime_score
    )

    # --- Handle all-positions-missing cases ---
    left_all_missing  = len(left_internal)  == 0
    right_all_missing = len(right_internal) == 0

    if left_all_missing and right_all_missing:
        raise HTTPException(
            status_code=422,
            detail={
                "request_id": request_id,
                "error": "all_positions_missing_both",
                "message": (
                    f"No positions from either portfolio were found in st_data "
                    f"for weekdate {latest_wd}."
                ),
            },
        )
    if left_all_missing:
        raise HTTPException(
            status_code=422,
            detail={
                "request_id": request_id,
                "error": "all_positions_missing",
                "side": "left",
                "message": (
                    f"None of the {len(left_parsed)} left positions were found "
                    f"in st_data for weekdate {latest_wd}."
                ),
            },
        )
    if right_all_missing:
        raise HTTPException(
            status_code=422,
            detail={
                "request_id": request_id,
                "error": "all_positions_missing",
                "side": "right",
                "message": (
                    f"None of the {len(right_parsed)} right positions were found "
                    f"in st_data for weekdate {latest_wd}."
                ),
            },
        )

    # --- Build per-portfolio summaries ---
    latest_wd_str = str(latest_wd)
    left_summary  = _build_portfolio_summary(left_evaluated,  left_internal,  latest_wd_str, current_regime)
    right_summary = _build_portfolio_summary(right_evaluated, right_internal, latest_wd_str, current_regime)

    # --- Build comparison and notes ---
    comparison = _compare_summary(left_summary, right_summary, left_parsed, right_parsed)
    notes = _build_comparison_notes(
        winner=comparison["winner"],
        score_delta=comparison["score_delta"],
        left=left_summary,
        right=right_summary,
        overlap_count=comparison["overlap_count"],
        overlap_symbols=comparison["overlap_symbols"],
        current_regime=current_regime,
    )

    return {
        "request_id": request_id,
        "weekdate":   latest_wd_str,
        "left":       left_summary,
        "right":      right_summary,
        "comparison": comparison,
        "regime_context": {
            "current_regime":    current_regime,
            "regime_score":      round(current_regime_score, 4),
            "regime_confidence": regime_confidence,
            "forecast_regime":   forecast["forecast_regime"],
            "forecast_confidence": fc_confidence,
            "recent_direction":  forecast["recent_direction"],
            "regime_consistency": round(consistency_pct, 4),
            "weeks_analyzed":    len(scores_by_week),
        },
        "comparison_notes": notes,
    }
