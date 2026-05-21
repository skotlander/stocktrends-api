"""Provider-agnostic Stock Trends inference and cognition semantics.

This module is intentionally data-only. It is safe for metadata endpoints,
OpenAPI generation, discovery manifests, and payment previews to import without
touching routers, pricing, payment enforcement, or database state.
"""

from __future__ import annotations

import copy
from typing import Any


COGNITION_ARCHITECTURE_DOC = "docs/STOCK_TRENDS_COGNITION_ARCHITECTURE.md"
INFERENCE_CONTRACT_ENDPOINT = "/v1/meta/inference"
STIM_PROVIDER_PROFILE_ENDPOINT = "/v1/meta/stim"


INFERENCE_CONTRACT: dict[str, Any] = {
    "contract_id": "stocktrends.inference.v1",
    "name": "Stock Trends provider-agnostic inference contract",
    "purpose": (
        "Defines the reusable inference and cognition vocabulary used by Stock Trends "
        "API metadata, discovery, OpenAPI extensions, x402/MPP previews, and future MCP "
        "reasoning tools."
    ),
    "architecture_source": COGNITION_ARCHITECTURE_DOC,
    "core_doctrine": [
        "ST-IM is the current baseline inference provider, not the final intelligence layer.",
        "Future Causal AI is a first-class future inference provider.",
        "Agent-facing surfaces should use provider-agnostic inference concepts where practical.",
        "Reasoning interfaces must preserve uncertainty, evidence, explanations, confidence, signal sources, and auditability.",
    ],
    "provider_agnostic_concepts": {
        "inference_provider": "The model, method, or intelligence source producing an inference output.",
        "signal_source": "The upstream Stock Trends signal family or data product used as evidence.",
        "forecast_horizon": "The future time window to which an inference output applies.",
        "probability_distribution": "A representation of possible future outcomes, including assumptions and uncertainty.",
        "confidence_measure": "A bounded or categorical measure of how strongly evidence supports the inference.",
        "evidence": "Structured inputs, signal states, statistical populations, or causal factors supporting an output.",
        "uncertainty": "Known limits, dispersion, sample weakness, regime dependence, or tail-risk exposure.",
        "explanation": "Human- and agent-readable rationale for how evidence supports the output.",
        "reasoning_interpretation": "Rules that describe how agents should use the inference without overstating it.",
        "auditability": "Metadata needed to trace provider, source inputs, assumptions, and confidence.",
    },
    "reasoning_rules": [
        "Interpret inference outputs as decision-support evidence, not deterministic predictions.",
        "Preserve provider identity; do not collapse all inference outputs into ST-IM-specific fields.",
        "Preserve forecast horizon, distribution assumption, confidence measure, evidence, and uncertainty together.",
        "Probabilities are conditional tendencies under stated assumptions, not guarantees, price targets, or buy/sell commands.",
        "Signals should frame narratives; external narratives must not override Stock Trends signal evidence.",
        "Future causal providers must be able to add causal factors, counterfactuals, and intervention analysis without changing the core contract.",
    ],
    "available_providers": [
        {
            "provider_id": "stim",
            "name": "Stock Trends Inference Model",
            "status": "current_baseline",
            "profile_endpoint": STIM_PROVIDER_PROFILE_ENDPOINT,
            "role": "durable, explainable baseline inference provider",
        }
    ],
    "future_provider_slots": [
        {
            "provider_id": "causal_ai",
            "status": "planned",
            "expected_outputs": [
                "causal_graph",
                "causal_factor_attribution",
                "counterfactual_analysis",
                "intervention_analysis",
                "regime_transition_probability",
                "causal_explanation",
                "portfolio_decision_intelligence",
            ],
        }
    ],
    "surface_alignment": {
        "metadata_endpoints": [INFERENCE_CONTRACT_ENDPOINT, STIM_PROVIDER_PROFILE_ENDPOINT],
        "discovery_surfaces": ["/v1/ai/context", "/v1/ai/tools", "/v1/openapi.json"],
        "payment_preview_surfaces": ["x402 stocktrends_preview", "MPP session metadata"],
        "future_mcp_role": "Expose constrained reasoning primitives backed by Stock Trends API and metered intelligence endpoints.",
    },
}


STIM_PROVIDER_PROFILE: dict[str, Any] = {
    "provider_id": "stim",
    "provider_name": "Stock Trends Inference Model",
    "provider_role": "current_baseline_inference_provider",
    "not_final_intelligence_layer": True,
    "contract_endpoint": INFERENCE_CONTRACT_ENDPOINT,
    "architecture_source": COGNITION_ARCHITECTURE_DOC,
    "full_name": "Stock Trends Inference Model",
    "output_type": "probabilistic forward return distribution",
    "signal_sources": [
        "Stock Trends trend classification",
        "trend persistence",
        "trend maturity",
        "relative performance",
        "volume context",
        "historical forward-return populations",
    ],
    "forecast_horizons": [
        {"id": "x4wk", "weeks": 4, "base_period_mean_return_pct": 0.0},
        {"id": "x13wk", "weeks": 13, "base_period_mean_return_pct": 2.19},
        {"id": "x40wk", "weeks": 40, "base_period_mean_return_pct": 6.45},
    ],
    "base_period_mean_returns_pct": {
        "x4wk": 0.00,
        "x13wk": 2.19,
        "x40wk": 6.45,
    },
    "field_model": {
        "xNwk": "Estimated mean forward return for the horizon.",
        "xNwk1": "Lower confidence-bound or lower percentile estimate for mean forward return.",
        "xNwk2": "Upper confidence-bound or upper percentile estimate for mean forward return.",
        "xNwksd": "Standard deviation used for the horizon distribution estimate.",
        "prob13wk": "Probability of exceeding the 13-week base-period mean return.",
    },
    "randomness_assumptions": [
        "Markets are noisy, uncertain, and partly random.",
        "ST-IM does not eliminate randomness; it estimates conditional historical tendencies.",
        "Individual outcomes can diverge materially from the estimated distribution.",
    ],
    "distribution_framing": {
        "assumption": "normal_approximation",
        "central_limit_theorem_intuition": (
            "Large historical populations of similarly classified observations can support "
            "distribution-level reasoning even though individual securities remain uncertain."
        ),
        "probability_formula": "probability_outperform = 1 - normal_cdf((base_mean - stim_mean) / standard_deviation)",
    },
    "classification_role": (
        "The Stock Trends classification system converts raw weekly market behavior into "
        "structured, repeatable factor states. These states create historical populations "
        "from which forward-return distributions can be estimated and compared."
    ),
    "probability_interpretation": [
        "Interpret ST-IM probabilities as conditional historical tendencies.",
        "Do not interpret probabilities as guarantees, price targets, or direct buy/sell commands.",
        "Compare each ST-IM mean and lower bound to the matching base-period mean before drawing conclusions.",
        "Treat stale or missing ST-IM records as historical fallback context.",
    ],
    "stim_select": {
        "role": "strict_filtering_framework",
        "description": (
            "Identifies instruments whose probability and confidence profiles appear favorable "
            "relative to broad/random baseline distributions."
        ),
        "criteria": {
            "x4wk1": {"operator": ">", "threshold_pct": 0.0},
            "x13wk1": {"operator": ">", "threshold_pct": 2.19},
            "x40wk1": {"operator": ">", "threshold_pct": 6.45},
            "prob13wk": {"operator": ">=", "threshold": 0.55},
            "all_criteria_required": True,
        },
        "ranking": "prob13wk descending",
    },
    "strengths": [
        "Repeatable classification-driven inference",
        "Distribution-level comparison across large historical populations",
        "Useful ranking and screening context",
        "Explainable baseline for future provider comparison",
        "Portfolio workflow support through repeated decision-making",
    ],
    "portfolio_applications": [
        "ranking",
        "screening",
        "allocation_review",
        "regime-aware interpretation",
        "market-structure analysis",
        "repeated decision-making under uncertainty",
    ],
    "limitations": [
        "regime_shifts",
        "non_stationarity",
        "sample_size_weakness",
        "tail_events",
        "liquidity_shocks",
        "news_shocks",
        "uncertainty_in_individual_stock_outcomes",
    ],
}


def inference_contract() -> dict[str, Any]:
    return copy.deepcopy(INFERENCE_CONTRACT)


def stim_provider_profile() -> dict[str, Any]:
    return copy.deepcopy(STIM_PROVIDER_PROFILE)


def stim_interpretation_guidance() -> dict[str, Any]:
    profile = stim_provider_profile()
    return {
        "inference_contract_endpoint": INFERENCE_CONTRACT_ENDPOINT,
        "inference_provider": {
            "provider_id": profile["provider_id"],
            "provider_role": profile["provider_role"],
            "not_final_intelligence_layer": profile["not_final_intelligence_layer"],
            "profile_endpoint": STIM_PROVIDER_PROFILE_ENDPOINT,
        },
        "base_period_mean_returns_pct": {
            "x4wk": "4-week baseline from /v1/meta/stim",
            "x13wk": "13-week baseline from /v1/meta/stim",
            "x40wk": "40-week baseline from /v1/meta/stim",
        },
        "mean_return_fields": ["x4wk", "x13wk", "x40wk"],
        "standard_deviation_fields": ["x4wksd", "x13wksd", "x40wksd"],
        "calculation": {
            "delta_vs_base": "stim_mean - base_mean",
            "z": "(base_mean - stim_mean) / standard_deviation",
            "probability_outperform": "1 - normal_cdf(z)",
        },
        "interpretation_rules": [
            "Call /v1/meta/inference and /v1/meta/stim before interpreting ST-IM results.",
            "Raw x4wk/x13wk/x40wk means are estimated mean returns, not standalone bullish signals.",
            "Do not interpret a positive raw mean as bullish unless it exceeds the relevant base mean or has adequate probability of exceeding it.",
            "Use x4wksd/x13wksd/x40wksd to estimate probability of exceeding base-period means under the stated distribution assumption.",
            "Interpret ST-IM probabilities as conditional historical tendencies, not guarantees, price targets, or buy/sell commands.",
            "If is_stale=true or missing_reason is present, treat the ST-IM result as historical fallback and disclose that limitation.",
        ],
        "randomness_assumptions": profile["randomness_assumptions"],
        "distribution_framing": profile["distribution_framing"],
        "classification_role": profile["classification_role"],
        "limitations": profile["limitations"],
        "portfolio_applications": profile["portfolio_applications"],
        "stim_select_style_logic": {
            "prob13wk_minimum": 0.55,
            "prob13wk_minimum_description": "13-week probability of exceeding the base-period mean should be at least 55%.",
            "lower_confidence_bounds": "Emphasize lower confidence bounds relative to base-period means where applicable.",
        },
    }


def openapi_inference_extension(path: str) -> dict[str, Any] | None:
    full_path = path if path.startswith("/v1/") else f"/v1{path}"

    if full_path == INFERENCE_CONTRACT_ENDPOINT:
        return {
            "x-stocktrends-cognition-contract": "provider_agnostic_inference_contract",
            "x-stocktrends-cognition-architecture": COGNITION_ARCHITECTURE_DOC,
            "x-stocktrends-inference-provider-agnostic": True,
        }

    if full_path == STIM_PROVIDER_PROFILE_ENDPOINT or full_path.startswith("/v1/stim"):
        return {
            "x-stocktrends-cognition-contract": "provider_agnostic_inference_contract",
            "x-stocktrends-cognition-architecture": COGNITION_ARCHITECTURE_DOC,
            "x-stocktrends-inference-provider": "stim",
            "x-stocktrends-inference-provider-role": "current_baseline_inference_provider",
            "x-stocktrends-provider-profile": STIM_PROVIDER_PROFILE_ENDPOINT,
            "x-stocktrends-inference-contract": INFERENCE_CONTRACT_ENDPOINT,
            "x-stocktrends-not-final-intelligence-layer": True,
        }

    if full_path.startswith("/v1/selections") or full_path.startswith("/v1/portfolio") or full_path.startswith("/v1/decision"):
        return {
            "x-stocktrends-cognition-contract": "provider_agnostic_inference_contract",
            "x-stocktrends-cognition-architecture": COGNITION_ARCHITECTURE_DOC,
            "x-stocktrends-inference-contract": INFERENCE_CONTRACT_ENDPOINT,
            "x-stocktrends-reasoning-interpretation": "decision_support_with_uncertainty_evidence_and_confidence",
        }

    return None
