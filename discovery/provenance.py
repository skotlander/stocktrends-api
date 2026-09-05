"""Static Stock Trends framework provenance for public discovery surfaces.

This module is intentionally data-only. It must not import routers, pricing,
payment enforcement, or database state. Public metadata uses an approximate
snapshot rather than live counts so discovery requests stay lightweight.
"""

from __future__ import annotations

import copy
from typing import Any


HISTORICAL_COVERAGE_START_YEAR = 1980
APPROXIMATE_OBSERVATION_COUNT = "16M+"
UPDATE_FREQUENCY = "weekly"
HISTORICAL_INTERPRETATION_LIMIT = (
    "Historical depth supports research and probabilistic interpretation but "
    "does not guarantee future performance."
)
NO_ADVICE_LIMIT = (
    "Stock Trends outputs are not investment advice, price targets, or direct "
    "buy/sell commands."
)
EXTERNAL_EVENT_LABEL_LIMIT = (
    "Externally named market events are not native Stock Trends labels unless "
    "separately curated."
)

AI_CONTEXT_PROVENANCE_TEXT = (
    "Stock Trends is a long-horizon market-intelligence framework, not a shallow "
    "market-data wrapper. Its historical classification record extends back to 1980 "
    "and includes 16M+ observations encoded through a consistent Stock Trends doctrine "
    "covering trend, relative performance, relative performance direction, volume, "
    "breadth, sector leadership, and regime structure. Agents should treat this "
    "historical depth as research provenance for probabilistic interpretation, not as "
    "a guarantee of future performance."
)

STIM_PROVENANCE_TEXT = (
    "ST-IM is an inference layer built on decades of Stock Trends classification "
    "history. Its value is not only point-in-time prediction, but probabilistic "
    "interpretation of current signals against long-horizon historical behavior. "
    "ST-IM outputs are not guarantees, price targets, investment advice, or direct "
    "buy/sell commands."
)

INDICATORS_PROVENANCE_TEXT = (
    "Stock Trends indicators are part of a multi-decade classification framework "
    "with records extending back to 1980. Their value comes from consistent semantics "
    "across market history: trend state, trend persistence, relative performance, "
    "relative performance direction, and volume activity are encoded in a stable "
    "doctrine designed for longitudinal research."
)

DATA_PROVENANCE: dict[str, Any] = {
    "historical_coverage_start_year": HISTORICAL_COVERAGE_START_YEAR,
    "approximate_observation_count": APPROXIMATE_OBSERVATION_COUNT,
    "update_frequency": UPDATE_FREQUENCY,
    "classification_framework": "Stock Trends trend classification methodology",
    "semantic_continuity": (
        "Stock Trends indicators use a consistent classification doctrine across "
        "decades of observations."
    ),
    "native_signal_domains": [
        "trend classification",
        "relative performance",
        "relative performance direction",
        "volume activity",
        "market breadth",
        "sector leadership",
        "regime structure",
    ],
    "research_value": [
        "long-horizon signal validation",
        "regime analysis",
        "sector rotation research",
        "portfolio construction research",
        "causal and probabilistic market analysis",
        "agentic market-intelligence workflows",
    ],
    "important_limits": [
        HISTORICAL_INTERPRETATION_LIMIT,
        NO_ADVICE_LIMIT,
        EXTERNAL_EVENT_LABEL_LIMIT,
    ],
}

# ---------------------------------------------------------------------------
# Evidence families.
#
# Stock Trends publishes three kinds of evidence.  They rest on different
# methods, cover different populations, and carry different limitations, so they
# are exposed as separate entries with their own provenance rather than merged.
#
# Merging them would produce exactly the claim this API must not make: a single
# undifferentiated statement about performance or edge.  A classification record
# is not an outcome measurement, and an outcome measurement of a signal rule is
# not an account result.
#
# Every statement below is sourced from a repository contract or an implemented
# endpoint.  No figure appears here that the repository does not itself hold.
# ---------------------------------------------------------------------------
EVIDENCE_SEPARATION_RULE = (
    "These evidence families use different methodologies, cover different populations, "
    "and carry different limitations. Evaluate each on its own terms. Do not combine "
    "them into a single performance, edge, or alpha claim, and do not treat any of them "
    "as a prediction of future results."
)

EVIDENCE_FAMILIES: list[dict[str, Any]] = [
    {
        "family_id": "historical_classification_provenance",
        "name": "Historical classification provenance",
        "evidence_type": "data_foundation",
        "methodology": (
            "Weekly Stock Trends classification of North American equities and ETFs "
            "applied under a historically consistent doctrine covering trend state, trend "
            "persistence, trend maturity, relative performance, relative performance "
            "direction, volume activity, breadth, sector leadership, and regime structure."
        ),
        "provenance": (
            f"Records extend back to {HISTORICAL_COVERAGE_START_YEAR} with "
            f"{APPROXIMATE_OBSERVATION_COUNT} observations, updated {UPDATE_FREQUENCY}. "
            "Stable classification semantics are what make observations from different "
            "periods comparable for longitudinal research."
        ),
        "what_it_is": (
            "The data foundation the rest of the service is built on, and the basis for "
            "comparing a current state against historically similar states."
        ),
        "what_it_is_not": (
            "Not a performance result, not a backtest, and not an outcome measurement."
        ),
        "inspect_at": ["/v1/meta/indicators", "/v1/ai/context"],
        "limitations": [
            HISTORICAL_INTERPRETATION_LIMIT,
            EXTERNAL_EVENT_LABEL_LIMIT,
            NO_ADVICE_LIMIT,
        ],
    },
    {
        "family_id": "inference_outcome_evidence",
        "name": "ST-IM Select realized outcome evidence",
        "evidence_type": "aggregate_signal_rule_outcomes",
        "methodology": (
            "Realized forward returns for historical observations meeting ST-IM Select "
            "criteria, measured at the 4, 13, and 40-week horizons against the "
            "corresponding base-period mean returns, restricted to observations whose "
            "measurement window has completed."
        ),
        "provenance": (
            "Aggregated from the Stock Trends classification record and the ST-IM "
            "provider profile published at /v1/meta/stim. Responses carry generated_at "
            "and source_latest_mature_weekdate so a caller can audit recency."
        ),
        "what_it_is": (
            "An aggregate outcome measurement for a stated signal rule, over a stated "
            "historical population, at stated horizons."
        ),
        "what_it_is_not": (
            "Not current selections, not individual symbols, not a portfolio result, and "
            "not a claim that any individual observation outperformed."
        ),
        "inspect_at": ["/v1/selections/stim-select/outcomes/summary", "/v1/meta/stim"],
        "limitations": [
            "Aggregate only; current selections and individual symbols are out of scope.",
            "Distribution-level tendencies do not describe any individual outcome.",
            "Subject to regime shifts, non-stationarity, sample-size weakness, and tail events.",
            NO_ADVICE_LIMIT,
        ],
    },
    {
        "family_id": "model_portfolio_and_strategy_records",
        "name": "Stock Trends model-portfolio and strategy records",
        "evidence_type": "rule_based_model_records",
        "methodology": (
            "Official Stock Trends model portfolios built from declared strategy rules, "
            "exposed with their return histories, closed-position records, and the "
            "strategy definitions they were constructed from, so the rules and the "
            "recorded results can be inspected together."
        ),
        "provenance": (
            "Live official Stock Trends model portfolio metadata and history. Current "
            "live holdings and current buy/sell candidates are intentionally excluded."
        ),
        "what_it_is": "An inspectable rule-based model record with a declared ruleset.",
        "what_it_is_not": (
            "Not audited brokerage-account performance, not live capital, and not a "
            "representation of returns any account achieved."
        ),
        "inspect_at": [
            "/v1/stocktrends/portfolios",
            "/v1/stocktrends/portfolios/{port_id}/returns",
            "/v1/stocktrends/portfolios/{port_id}/summary",
            "/v1/stocktrends/portfolios/{port_id}/positions/history",
            "/v1/stocktrends/strategies",
        ],
        "limitations": [
            (
                "Rule-based model records with declared cost and stop-loss "
                "assumptions; not audited brokerage-account performance."
            ),
            "Realised transaction costs and slippage may differ from the declared assumptions.",
            NO_ADVICE_LIMIT,
        ],
    },
]

# A fourth public surface exists for structure inspection rather than evidence,
# and is listed separately so it is never mistaken for an outcome record.
ILLUSTRATIVE_STRUCTURE_SURFACE = {
    "endpoint": "/v1/ai/proof/market-edge",
    "content": "static illustrative field structure",
    "what_it_is": (
        "A free, unauthenticated view of signal field structure and response shape, for "
        "confirming schemas before paid calls."
    ),
    "what_it_is_not": "Not live market data, and not outcome or performance evidence.",
}


def evidence_families() -> list[dict[str, Any]]:
    return copy.deepcopy(EVIDENCE_FAMILIES)


def evidence_map() -> dict[str, Any]:
    """Public evidence map: families kept separate, with their own limitations."""
    return {
        "separation_rule": EVIDENCE_SEPARATION_RULE,
        "families": evidence_families(),
        "illustrative_structure_surface": copy.deepcopy(ILLUSTRATIVE_STRUCTURE_SURFACE),
    }


PROVENANCE_METADATA_ENDPOINTS = [
    "/v1/ai/context",
    "/v1/meta/indicators",
    "/v1/meta/stim",
]

PROVENANCE_RELEVANT_ENDPOINT_PREFIXES = (
    "/v1/agent/screener",
    "/v1/indicators",
    "/v1/selections",
    "/v1/market",
    "/v1/breadth",
    "/v1/leadership",
    "/v1/decision",
    "/v1/portfolio",
    "/v1/workflows",
)


def data_provenance() -> dict[str, Any]:
    return copy.deepcopy(DATA_PROVENANCE)


def provenance_reference() -> dict[str, Any]:
    """Compact provenance pointer for per-endpoint/tool metadata."""
    return {
        "historical_coverage_start_year": HISTORICAL_COVERAGE_START_YEAR,
        "approximate_observation_count": APPROXIMATE_OBSERVATION_COUNT,
        "classification_framework": DATA_PROVENANCE["classification_framework"],
        "semantic_continuity": DATA_PROVENANCE["semantic_continuity"],
        "full_metadata_endpoints": list(PROVENANCE_METADATA_ENDPOINTS),
        "interpretation_limit": HISTORICAL_INTERPRETATION_LIMIT,
    }


def endpoint_needs_provenance(path: str) -> bool:
    full_path = path if path.startswith("/v1/") else f"/v1{path}"
    return (
        full_path in PROVENANCE_METADATA_ENDPOINTS
        or any(full_path.startswith(prefix) for prefix in PROVENANCE_RELEVANT_ENDPOINT_PREFIXES)
    )


def openapi_provenance_extension(path: str) -> dict[str, Any] | None:
    if not endpoint_needs_provenance(path):
        return None
    return {
        "x-stocktrends-data-provenance-reference": provenance_reference(),
    }
