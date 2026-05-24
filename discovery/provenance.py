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

AI_CONTEXT_PROVENANCE_TEXT = (
    "Stock Trends is a long-horizon market-intelligence framework, not a shallow "
    "market-data wrapper. Its historical classification record extends back to 1980 "
    "and includes 16M+ observations encoded through a consistent Stock Trends doctrine "
    "covering trend, relative performance, momentum direction, volume, breadth, sector "
    "leadership, and regime structure. Agents should treat this historical depth as "
    "research provenance for probabilistic interpretation, not as a guarantee of "
    "future performance."
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
    "across market history: trend state, trend duration, relative performance, momentum "
    "direction, and volume activity are encoded in a stable doctrine designed for "
    "longitudinal research."
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
        "momentum direction",
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
        (
            "Historical depth supports research and probabilistic interpretation but "
            "does not guarantee future performance."
        ),
        (
            "Stock Trends outputs are not investment advice, price targets, or direct "
            "buy/sell commands."
        ),
        (
            "Externally named market events are not native Stock Trends labels unless "
            "separately curated."
        ),
    ],
}

PROVENANCE_METADATA_ENDPOINTS = [
    "/v1/ai/context",
    "/v1/meta/indicators",
    "/v1/meta/stim",
]

PROVENANCE_RELEVANT_ENDPOINT_PREFIXES = (
    "/v1/agent/screener",
    "/v1/indicators",
    "/v1/stim",
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
        "interpretation_limit": DATA_PROVENANCE["important_limits"][0],
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
