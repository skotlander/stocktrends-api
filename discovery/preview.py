# discovery/preview.py
#
# Static, per-endpoint preview snippets surfaced in x402 challenge responses
# under the "stocktrends_preview" key.  Content is schema-level only — no
# live data, no STC costs, no real tickers.  All values are illustrative.
#
# Rules (enforced by tests):
#   - No imports from db, routers, payments, or pricing packages.
#   - Each call to get_endpoint_preview() returns a deep copy so callers
#     cannot mutate the registry (including nested lists/dicts).
#   - Returns None for unknown paths; callers must omit the key in that case.

import copy

_PREVIEW_BY_PATH: dict[str, dict] = {
    "/v1/agent/screener/top": {
        "response_shape": [
            "request_id", "screener", "weekdate", "filter_summary",
            "count", "total_matched",
            "results[].rank", "results[].symbol", "results[].exchange",
            "results[].symbol_exchange", "results[].trend", "results[].trend_cnt",
            "results[].mt_cnt", "results[].rsi", "results[].rsi_updn",
            "results[].vol_tag", "results[].weekdate",
        ],
        "note": (
            "Returns ranked instruments with trend classification, trend persistence "
            "(trend_cnt), trend maturity (mt_cnt), RSI (relative performance vs benchmark, "
            "baseline 100), and volume-tag fields per result row. "
            "See /v1/ai/proof/market-edge for a static structural example."
        ),
    },
    "/v1/stim/latest": {
        "response_shape": [
            "request_id", "symbol_exchange", "weekdate", "exchange", "symbol",
            "x4wk1", "x4wk2", "x4wk", "x4wksd",
            "x13wk1", "x13wk2", "x13wk", "x13wksd",
            "x40wk1", "x40wk2", "x40wk", "x40wksd",
            "latest_data_weekdate", "is_stale", "missing_reason", "missing_weekdate",
        ],
        "note": (
            "Returns Stock Trends Inference Model (ST-IM) outputs: forward return "
            "expectations and statistical distributions across 4-week, 13-week, "
            "and 40-week horizons for a given symbol/exchange. "
            "Fields: xNwk1 = lower bound, xNwk2 = upper bound, xNwk = expected return (mean), "
            "xNwksd = standard deviation. is_stale=true when ST-IM estimate is missing for "
            "the latest market week (insufficient sample)."
        ),
    },
    "/v1/stim/history": {
        "response_shape": [
            "request_id", "symbol_exchange", "start", "end",
            "count", "data", "include_gaps", "gaps",
        ],
        "note": (
            "Returns a historical series of ST-IM (Stock Trends Inference Model) forward return "
            "distribution records for a given symbol/exchange. Each record contains expected "
            "returns and standard deviations for 4-week, 13-week, and 40-week horizons "
            "(x4wk, x4wksd, x13wk, x13wksd, x40wk, x40wksd and percentile bounds). "
            "Gaps are weeks where st_data exists but no ST-IM estimate was produced "
            "(typically due to insufficient sample)."
        ),
    },
    "/v1/decision/evaluate-symbol": {
        "response_shape": [
            "request_id", "symbol", "exchange", "weekdate",
            "bias", "confidence", "decision_score", "alignment",
            "symbol_context.trend", "symbol_context.trend_cnt", "symbol_context.mt_cnt",
            "symbol_context.rsi", "symbol_context.rsi_updn", "symbol_context.vol_tag",
            "symbol_context.symbol_bias",
            "regime_context.current_regime", "regime_context.regime_score",
            "regime_context.regime_confidence", "regime_context.forecast_regime",
            "regime_context.forecast_confidence", "regime_context.recent_direction",
            "regime_context.regime_consistency", "regime_context.weeks_analyzed",
            "signal_notes",
        ],
        "note": (
            "Returns a structured buy/hold/sell decision (bias) with confidence, "
            "decision_score (0–1), per-symbol Stock Trends signal context, and market "
            "regime context. Fully deterministic — no ML."
        ),
    },
    "/v1/portfolio/construct": {
        "response_shape": [
            "request_id", "weekdate",
            "portfolio[].rank", "portfolio[].weight", "portfolio[].symbol",
            "portfolio[].exchange", "portfolio[].symbol_exchange",
            "portfolio[].trend", "portfolio[].trend_cnt", "portfolio[].mt_cnt",
            "portfolio[].rsi", "portfolio[].bias", "portfolio[].confidence",
            "portfolio[].decision_score",
            "count", "universe", "exchange_filter",
            "candidates_evaluated", "candidate_selection_method", "candidate_ordering",
            "portfolio_score",
            "bias_requested", "bias_resolved",
            "regime_context.current_regime", "regime_context.regime_score",
            "regime_context.regime_confidence", "regime_context.forecast_regime",
            "regime_context.forecast_confidence", "regime_context.recent_direction",
            "regime_context.regime_consistency", "regime_context.weeks_analyzed",
            "construction_notes",
        ],
        "note": (
            "Returns a constructed equal-weight portfolio allocation with per-position weights, "
            "Stock Trends signal fields, decision scores, and market regime context. "
            "universe and exchange_filter echo the parameters used. "
            "candidate_selection_method ('full_eligible_universe') and candidate_ordering "
            "('decision_score DESC, symbol ASC') describe how candidates were assembled "
            "and ranked before top-N selection."
        ),
    },
    "/v1/portfolio/evaluate": {
        "response_shape": [
            "request_id", "weekdate",
            "positions[].symbol", "positions[].exchange", "positions[].symbol_exchange",
            "positions[].weight", "positions[].trend", "positions[].trend_cnt",
            "positions[].mt_cnt", "positions[].rsi", "positions[].bias",
            "positions[].confidence", "positions[].decision_score",
            "positions[].alignment", "positions[].found",
            "positions_found", "positions_missing", "effective_weight",
            "portfolio_score", "portfolio_bias", "portfolio_confidence",
            "portfolio_alignment",
            "regime_context.current_regime", "regime_context.regime_score",
            "regime_context.regime_confidence", "regime_context.forecast_regime",
            "regime_context.forecast_confidence", "regime_context.recent_direction",
            "regime_context.regime_consistency", "regime_context.weeks_analyzed",
            "evaluation_notes",
        ],
        "note": (
            "Evaluates a user-supplied list of symbol-weight pairs using Stock Trends "
            "decision scoring and market regime context. Returns position-level and "
            "portfolio-level aggregates (portfolio_score, portfolio_bias, portfolio_alignment). "
            "Missing symbols included with found=false, excluded from aggregates."
        ),
    },
    "/v1/portfolio/compare": {
        "response_shape": [
            "request_id", "weekdate",
            "left.positions[].symbol", "left.positions[].exchange",
            "left.positions[].symbol_exchange", "left.positions[].weight",
            "left.positions[].trend", "left.positions[].trend_cnt",
            "left.positions[].mt_cnt", "left.positions[].rsi",
            "left.positions[].bias", "left.positions[].confidence",
            "left.positions[].decision_score", "left.positions[].alignment",
            "left.positions[].found",
            "left.positions_found", "left.positions_missing", "left.effective_weight",
            "left.portfolio_score", "left.portfolio_bias", "left.portfolio_confidence",
            "left.portfolio_alignment", "left.evaluation_notes",
            "right.positions[].symbol", "right.portfolio_score",
            "right.portfolio_bias", "right.portfolio_alignment",
            "comparison.winner", "comparison.score_delta", "comparison.score_advantage",
            "comparison.alignment_advantage", "comparison.confidence_advantage",
            "comparison.effective_weight_delta", "comparison.overlap_count",
            "comparison.overlap_symbols",
            "regime_context.current_regime", "regime_context.regime_score",
            "regime_context.regime_confidence", "regime_context.forecast_regime",
            "regime_context.forecast_confidence", "regime_context.recent_direction",
            "regime_context.regime_consistency", "regime_context.weeks_analyzed",
            "comparison_notes",
        ],
        "note": (
            "Returns per-portfolio evaluation results (left and right) in the same shape "
            "as /portfolio/evaluate, plus a structured comparison block identifying the "
            "winner by decision score, alignment advantage, and overlapping positions."
        ),
    },
    "/v1/market/regime/latest": {
        "response_shape": [
            "regime", "confidence", "regime_score",
            "bullish_pct", "bearish_pct",
            "avg_rsi", "avg_mt_cnt",
            "weekdate", "signal_count",
        ],
        "note": (
            "Returns the current market regime classification derived from the distribution "
            "of Stock Trends trend codes across all active signals. "
            "regime: bullish | bearish | mixed. "
            "regime_score = bullish_pct - bearish_pct, range -1.0 to +1.0. "
            "Bullish codes: {^+, ^-, v^}. Bearish codes: {v-, v+, ^v}."
        ),
    },
    "/v1/market/regime/history": {
        "response_shape": [
            "history[].weekdate", "history[].regime", "history[].confidence",
            "history[].regime_score", "history[].bullish_pct", "history[].bearish_pct",
            "history[].avg_rsi", "history[].avg_mt_cnt", "history[].signal_count",
            "count", "limit", "start_date",
        ],
        "note": (
            "Returns a recent weekly sequence of market regime snapshots, most recent first. "
            "Each entry uses the same classification logic as /market/regime/latest. "
            "regime_score = bullish_pct - bearish_pct, range -1.0 to +1.0."
        ),
    },
    "/v1/market/regime/forecast": {
        "response_shape": [
            "forecast_regime", "forecast_confidence",
            "current_regime", "current_regime_score",
            "recent_direction", "regime_consistency",
            "projected_regime_score", "avg_weekly_score_delta",
            "recent_scores", "weeks_analyzed", "lookback", "weekdate",
        ],
        "note": (
            "Returns a deterministic forward regime outlook derived from the direction "
            "and consistency of recent weekly regime scores. No ML. "
            "forecast_regime: bullish | bearish | mixed. "
            "recent_direction: improving | deteriorating | stable."
        ),
    },
    "/v1/selections/latest": {
        "response_shape": [
            "request_id", "weekdate", "exchange", "min_prob13wk",
            "include_data", "include_mast", "cs_only", "count",
            "data[].weekdate", "data[].exchange", "data[].symbol",
            "data[].prob13wk", "data[].symbol_exchange",
        ],
        "note": (
            "Returns the latest st_select stock list ordered by prob13wk descending "
            "(probability of exceeding the 13-week base-period mean return of 2.19%, "
            "assuming normal distribution). "
            "No threshold filter is applied. "
            "Use /selections/published/latest for the three-horizon published STIM Select definition. "
            "Use include_data=true to add Stock Trends signal fields per symbol."
        ),
    },
    "/v1/selections/history": {
        "response_shape": [
            "request_id", "symbol", "exchange", "symbol_exchange",
            "start", "end", "min_prob13wk",
            "include_data", "include_mast", "cs_only", "count",
            "data[].weekdate", "data[].exchange", "data[].symbol",
            "data[].prob13wk", "data[].symbol_exchange",
        ],
        "note": (
            "Returns historical st_select records. "
            "Filter by symbol, exchange, or date range. "
            "Each entry includes prob13wk — probability of exceeding the 13-week base-period "
            "mean return of 2.19%. "
            "No threshold filter is applied unless min_prob13wk is set. "
            "Use /selections/published/history for the three-horizon published definition. "
            "Use include_data=true to add Stock Trends signal fields per record."
        ),
    },
}


def get_endpoint_preview(path: str) -> dict | None:
    """Return a deep copy of the preview for *path*, or None if none exists."""
    entry = _PREVIEW_BY_PATH.get(path)
    if entry is None:
        return None
    return copy.deepcopy(entry)
