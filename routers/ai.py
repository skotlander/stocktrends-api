import time
from decimal import Decimal
from datetime import datetime, timezone

from fastapi import APIRouter, Response
from sqlalchemy import text
from db import get_engine, get_metering_engine
from routers.workflows import WORKFLOW_REGISTRY, STC_TO_USD, WORKFLOW_ID_EXAMPLES
from discovery.endpoint_metadata import (
    build_tool_template,
    input_location_for_method,
    iter_endpoint_metadata,
    schema_to_parameters,
)
from discovery.inference_semantics import (
    COGNITION_ARCHITECTURE_DOC,
    INFERENCE_CONTRACT_ENDPOINT,
    STIM_PROVIDER_PROFILE_ENDPOINT,
    inference_contract,
    stim_provider_profile,
)
from discovery.provenance import (
    AI_CONTEXT_PROVENANCE_TEXT,
    INDICATORS_PROVENANCE_TEXT,
    STIM_PROVENANCE_TEXT,
    data_provenance,
    provenance_reference,
)
from discovery.service_meta import DATASET_DESCRIPTION, SERVICE_POSITIONING
from pricing.classifier import classify_request as _classify_request, NON_METERED_PATHS
from payments.policy_provider import (
    is_free_metered_path as _is_free_metered_path,
    get_effective_endpoint_payment_policy as _get_endpoint_policy,
    is_agent_pay_route as _is_agent_pay_route,
    get_agent_pay_auth_bypass_methods as _get_agent_pay_bypass_methods,
)

router = APIRouter(prefix="/ai", tags=["ai"])

# ---------------------------------------------------------------------------
# _MANIFEST_PUBLIC_PATHS mirrors ApiKeyMiddleware.public_paths for the paths
# that appear in this manifest.  Tests verify this stays in sync.
# ---------------------------------------------------------------------------
_MANIFEST_PUBLIC_PATHS: frozenset = frozenset({
    "/v1/openapi.json",
    "/v1/pricing",
    "/v1/pricing/catalog",
    "/v1/cost-estimate",
    "/v1/workflows",
    "/v1/instruments/lookup",
    "/v1/instruments/resolve",
    "/v1/stwr/reports/catalog",
    "/v1/meta/indicators",
    "/v1/meta/inference",
    "/v1/meta/stim",
    "/v1/meta/stwr",
    "/v1/leadership/definitions",
    "/v1/ai/context",
    "/v1/ai/tools",
    "/v1/ai/proof/market-edge",
})


def _access_metadata(path: str, method: str = "GET") -> dict:
    """
    Derive auth_required, metered, pricing_rule_id, and supported_rails
    from the runtime classifier and policy sources.

    Called once at module load to build _TOOLS — no DB access required.
    Classifier falls back to the default policy config when no control-plane
    URL is configured (safe in test and production startup alike).

    Rule ID derivation priority:
      1. Exact endpoint_payment_policy → stable, specific rule ID
      2. free_metered path             → "default_free_metered" (stable)
      3. agent_pay prefix (no policy)  → None  (dynamic: "default_subscription"
                                                for subscription callers,
                                                "agent_pay_required" for
                                                agent-pay callers — not safely
                                                representable as a single value)
      4. Metered subscription path     → decision.log_pricing_rule_id
      5. Auth-required non-metered     → None
      6. Free public                   → None
    """
    decision = _classify_request(
        path=path,
        has_paid_auth=True,
        payment_method_header=None,
        plan_code="pro",
        agent_identifier=None,
        method=method,
    )

    endpoint_policy = _get_endpoint_policy(path, method.upper())

    access_type = "free"
    requires_payment = False

    if endpoint_policy and endpoint_policy.pricing_rule_id:
        # Exact endpoint policy: rule and rails are stable.
        pricing_rule_id = endpoint_policy.pricing_rule_id
        supported_rails = list(endpoint_policy.allowed_rails)
        access_type = "paid"
        requires_payment = bool(endpoint_policy.machine_payment_rails)
    elif _is_free_metered_path(path):
        # Tracked but not billed; rule is stable.
        pricing_rule_id = "default_free_metered"
        supported_rails = []
        access_type = "free_metered"
    elif _is_agent_pay_route(path, method.upper()) and not endpoint_policy:
        # STIM prefix paths: the runtime rule ID depends on the caller's
        # access method (subscription vs agent-pay) so it cannot be
        # represented as a single static value. Use None to avoid drift.
        # Rails: subscription is always valid for paid callers; agent-pay
        # bypass methods come from the runtime config (not hardcoded).
        pricing_rule_id = None
        agent_pay_rails = list(_get_agent_pay_bypass_methods(path, method.upper()))
        supported_rails = ["subscription"] + agent_pay_rails
        access_type = "paid"
        requires_payment = bool(agent_pay_rails)
    elif decision.is_metered:
        # Subscription-covered metered path (e.g. /v1/pricing/catalog).
        pricing_rule_id = decision.log_pricing_rule_id
        supported_rails = ["subscription"]
        access_type = "paid" if path not in _MANIFEST_PUBLIC_PATHS else "free"
        requires_payment = False
    elif path not in _MANIFEST_PUBLIC_PATHS:
        # Auth-required, non-metered.
        pricing_rule_id = None
        supported_rails = ["subscription"]
        access_type = "subscription"
    else:
        # Truly free/public.
        pricing_rule_id = None
        supported_rails = []

    return {
        "auth_required": path not in _MANIFEST_PUBLIC_PATHS,
        "metered": bool(decision.is_metered),
        "pricing_rule_id": pricing_rule_id,
        "supported_rails": supported_rails,
        "access_type": access_type,
        "requires_payment": requires_payment,
    }


# ---------------------------------------------------------------------------
# Tool templates — static fields only (description, schema, etc.).
# auth_required / metered / pricing_rule_id / supported_rails are injected
# at module load by _build_tools() via _access_metadata() so they always
# reflect the actual runtime classifier behavior.
# ---------------------------------------------------------------------------

_TOOL_TEMPLATES = [
    # ---- Discovery / public ------------------------------------------------
    {
        "name": "ai_context",
        "title": "AI Context",
        "description": (
            "Returns dataset overview, endpoint groups, access model, and agent usage guidance. "
            "Includes long-horizon Stock Trends provenance for records extending back to 1980. "
            "Secondary explanatory context for agents after reading the machine-readable "
            "/v1/ai/tools manifest."
        ),
        "endpoint": "/v1/ai/context",
        "method": "GET",
        "category": "discovery",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_summary": "Dataset metadata, endpoint groups, auth model, and agent usage guidance.",
    },
    {
        "name": "ai_tools",
        "title": "AI Tools Manifest",
        "description": (
            "Returns this MCP-compatible tools manifest. Primary machine-readable entry point "
            "for agents. "
            "Lists all discoverable tools, workflows, pricing model, and auth expectations."
        ),
        "endpoint": "/v1/ai/tools",
        "method": "GET",
        "category": "discovery",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_summary": "MCP tools manifest: tools, workflows, pricing, auth.",
    },
    {
        "name": "openapi_schema",
        "title": "OpenAPI Schema",
        "description": (
            "Machine-readable OpenAPI contract for exact parameter locations, request bodies, "
            "response schemas, and auth headers. Planning helper for autonomous agents."
        ),
        "endpoint": "/v1/openapi.json",
        "method": "GET",
        "category": "discovery",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_summary": "OpenAPI JSON schema for Stock Trends API v1.",
    },
    {
        "name": "ai_proof_market_edge",
        "title": "Proof of Value - Market Edge",
        "description": (
            "Free synthetic-only planning helper. Shows Stock Trends signal fields, trend codes, "
            "RSI baseline semantics, and agent workflow value without exposing paid market data."
        ),
        "endpoint": "/v1/ai/proof/market-edge",
        "method": "GET",
        "category": "discovery",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_summary": "Synthetic signal example and next-step payment guidance.",
    },
    {
        "name": "pricing_metadata",
        "title": "Pricing Metadata",
        "description": (
            "Returns machine-readable pricing metadata including supported payment methods, "
            "endpoint families, and agent identity guidance."
        ),
        "endpoint": "/v1/pricing",
        "method": "GET",
        "category": "pricing",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_summary": "Pricing metadata: payment methods, endpoint families, agent headers.",
    },
    {
        "name": "pricing_catalog",
        "title": "Live Pricing Catalog",
        "description": (
            "Returns all active STC pricing rules from the pricing engine. "
            "Public planning infrastructure under the current API behavior. "
            "Agents should call this at startup to build a local cost map before issuing data requests."
        ),
        "endpoint": "/v1/pricing/catalog",
        "method": "GET",
        "category": "pricing",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_summary": "Live pricing rules: pricing_rule_id, endpoint_pattern, cost_per_request (STC), access_type, requires_payment.",
    },
    {
        "name": "workflow_registry",
        "title": "Workflow Registry",
        "description": (
            "Returns the static workflow registry with live per-step STC costs resolved from "
            "the pricing engine. Use this to understand available multi-step workflows and their costs."
        ),
        "endpoint": "/v1/workflows",
        "method": "GET",
        "category": "discovery",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_summary": "Workflow definitions with live per-step STC costs.",
    },
    # ---- Pricing / cost planning -------------------------------------------
    {
        "name": "cost_estimate",
        "title": "Workflow Cost Estimate",
        "description": (
            "Returns a deterministic cost estimate for a named workflow. "
            "Costs resolved from live pricing rules. Public, free, and non-metered."
        ),
        "endpoint": "/v1/cost-estimate",
        "method": "GET",
        "category": "pricing",
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_id": {
                    "type": "string",
                    "enum": WORKFLOW_ID_EXAMPLES,
                    "example": "portfolio_build",
                    "examples": WORKFLOW_ID_EXAMPLES,
                    "description": "Workflow ID. Safe executable examples are listed in enum; see GET /v1/workflows for full details.",
                },
                "quota_remaining": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Caller's current subscription quota remaining (optional).",
                },
                "rail_preference": {
                    "type": "string",
                    "enum": ["subscription", "x402", "mpp", "auto"],
                    "description": "Rail preference for cost assignment (default: auto).",
                },
            },
            "required": ["workflow_id"],
        },
        "output_summary": "Estimated total STC cost and per-step rail assignment for the workflow.",
        "safe_example_request": {
            "method": "GET",
            "path": "/v1/cost-estimate",
            "query": {"workflow_id": "portfolio_build", "rail_preference": "auto"},
        },
    },
    # ---- Planning helpers --------------------------------------------------
    {
        "name": "instrument_lookup",
        "title": "Instrument Lookup",
        "description": (
            "Planning helper for resolving a ticker into Stock Trends instrument rows and "
            "symbol_exchange values before paid symbol, ST-IM, or portfolio calls."
        ),
        "endpoint": "/v1/instruments/lookup",
        "method": "GET",
        "category": "planning_helper",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol, for example IBM.", "example": "IBM"},
                "cs_only": {"type": "boolean", "default": True, "description": "Filter to common stocks only."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                "details": {"type": "boolean", "default": False},
            },
            "required": ["symbol"],
        },
        "safe_example_request": {"method": "GET", "path": "/v1/instruments/lookup", "query": {"symbol": "IBM"}},
        "output_summary": "Candidate instruments and symbol_exchange keys such as IBM-N.",
    },
    {
        "name": "instrument_resolve",
        "title": "Instrument Resolve",
        "description": (
            "Planning helper that resolves symbol_exchange or symbol plus exchange into one "
            "instrument before downstream paid intelligence calls."
        ),
        "endpoint": "/v1/instruments/resolve",
        "method": "GET",
        "category": "planning_helper",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_exchange": {"type": "string", "description": "Combined symbol and exchange.", "example": "IBM-N"},
                "symbol": {"type": "string", "description": "Ticker symbol.", "example": "IBM"},
                "exchange": {"type": "string", "enum": ["N", "Q", "A", "B", "T", "I"], "description": "Exchange code."},
                "prefer_exchange": {"type": "string", "default": "N"},
                "cs_only": {"type": "boolean", "default": True},
                "details": {"type": "boolean", "default": False},
            },
            "required": [],
            "oneOf": [
                {"required": ["symbol_exchange"]},
                {"required": ["symbol", "exchange"]},
            ],
        },
        "safe_example_request": {"method": "GET", "path": "/v1/instruments/resolve", "query": {"symbol_exchange": "IBM-N"}},
        "output_summary": "One resolved instrument, or ambiguity guidance with candidate matches.",
    },
    {
        "name": "stwr_reports_catalog",
        "title": "STWR Reports Catalog",
        "description": (
            "Planning helper listing Stock Trends Weekly Reporter report codes. Use before "
            "paid /v1/stwr/reports/latest or /v1/stwr/reports/history calls."
        ),
        "endpoint": "/v1/stwr/reports/catalog",
        "method": "GET",
        "category": "planning_helper",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "safe_example_request": {"method": "GET", "path": "/v1/stwr/reports/catalog", "query": {}},
        "output_summary": "Report codes, names, and descriptions.",
    },
    {
        "name": "meta_indicators",
        "title": "Indicator Metadata",
        "description": (
            "Planning helper with definitions for Stock Trends fields such as trend, trend_cnt, "
            "mt_cnt, rsi, rsi_updn, and vol_tag. Includes the multi-decade indicator "
            "provenance reference."
        ),
        "endpoint": "/v1/meta/indicators",
        "method": "GET",
        "category": "planning_helper",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "safe_example_request": {"method": "GET", "path": "/v1/meta/indicators", "query": {}},
        "output_summary": "Indicator field definitions and trend-code meanings.",
    },
    {
        "name": "meta_inference",
        "title": "Inference Cognition Contract",
        "description": (
            "Planning helper defining the provider-agnostic Stock Trends inference contract. "
            "Use before interpreting ST-IM or future causal AI outputs so agents preserve "
            "provider identity, forecast horizon, distribution, confidence, evidence, "
            "uncertainty, explanation, signal sources, and auditability."
        ),
        "endpoint": "/v1/meta/inference",
        "method": "GET",
        "category": "planning_helper",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "safe_example_request": {"method": "GET", "path": "/v1/meta/inference", "query": {}},
        "output_summary": "Provider-agnostic inference and cognition contract metadata.",
    },
    {
        "name": "meta_stim",
        "title": "ST-IM Metadata",
        "description": (
            "Planning helper explaining the ST-IM provider profile, fields, base-period mean "
            "returns, confidence bounds, and 4, 13, and 40 week horizons. ST-IM is the "
            "current baseline inference provider, not the final intelligence layer. Includes "
            "historical classification provenance and limitations language."
        ),
        "endpoint": "/v1/meta/stim",
        "method": "GET",
        "category": "planning_helper",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "safe_example_request": {"method": "GET", "path": "/v1/meta/stim", "query": {}},
        "output_summary": "ST-IM field definitions and base-period mean return metadata.",
    },
    {
        "name": "meta_stwr",
        "title": "STWR Metadata",
        "description": (
            "Planning helper summarizing Stock Trends Weekly Reporter report families and "
            "how to choose report codes before paid report calls."
        ),
        "endpoint": "/v1/meta/stwr",
        "method": "GET",
        "category": "planning_helper",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "safe_example_request": {"method": "GET", "path": "/v1/meta/stwr", "query": {}},
        "output_summary": "STWR report family metadata and report-code guidance.",
    },
    # ---- Decision ----------------------------------------------------------
    {
        "name": "evaluate_symbol",
        "title": "Symbol Decision Evaluation",
        "description": (
            "Evaluates a single symbol's trend context against the live market regime to produce "
            "a synthesized bias, confidence score, and decision_score (0–1). "
            "Fully deterministic — no ML."
        ),
        "endpoint": "/v1/decision/evaluate-symbol",
        "method": "POST",
        "category": "decision",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_exchange": {
                    "type": "string",
                    "description": "Combined symbol and exchange, e.g. 'AAPL-Q'.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol, e.g. 'AAPL'. Requires exchange when used alone.",
                },
                "exchange": {
                    "type": "string",
                    "description": "Exchange code, e.g. 'Q' (Nasdaq), 'N' (NYSE).",
                },
            },
            "required": [],
            "oneOf": [
                {"required": ["symbol_exchange"]},
                {"required": ["symbol", "exchange"]},
            ],
        },
        "output_summary": (
            "bias, confidence, decision_score (0–1), alignment, symbol_context, regime_context, signal_notes."
        ),
    },
    # ---- Market regime -----------------------------------------------------
    {
        "name": "market_regime_latest",
        "title": "Current Market Regime",
        "description": (
            "Returns the current market regime classification derived from the distribution "
            "of Stock Trends trend codes across all active signals. "
            "regime_score = bullish_pct - bearish_pct, range -1.0 to +1.0. "
            "Bullish codes: {^+, ^-, v^}. Bearish codes: {v-, v+, ^v}. "
            "Also returns avg_rsi (universe relative performance) and avg_mt_cnt (universe trend maturity)."
        ),
        "endpoint": "/v1/market/regime/latest",
        "method": "GET",
        "category": "market",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_summary": "regime, confidence, regime_score, bullish_pct, bearish_pct, avg_rsi, avg_mt_cnt, signal_count, weekdate.",
    },
    {
        "name": "market_regime_history",
        "title": "Market Regime History",
        "description": (
            "Returns a historical sequence of weekly market regime snapshots, most recent first. "
            "Each entry uses the same classification logic as /market/regime/latest. "
            "regime_score = bullish_pct - bearish_pct per week. "
            "Useful for trend context and regime transition analysis."
        ),
        "endpoint": "/v1/market/regime/history",
        "method": "GET",
        "category": "market",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_summary": "history[](weekdate, regime, confidence, regime_score, bullish_pct, bearish_pct, avg_rsi, avg_mt_cnt, signal_count), count, limit.",
    },
    {
        "name": "market_regime_forecast",
        "title": "Market Regime Forecast",
        "description": (
            "Returns a deterministic forward regime outlook derived from the direction "
            "and consistency of recent weekly regime scores. No ML. "
            "forecast_regime: bullish | bearish | mixed. "
            "forecast_confidence based on regime_consistency and avg_weekly_score_delta. "
            "Reuses the same trend classification as /market/regime/latest."
        ),
        "endpoint": "/v1/market/regime/forecast",
        "method": "GET",
        "category": "market",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_summary": "forecast_regime, forecast_confidence, current_regime, current_regime_score, recent_direction, regime_consistency, projected_regime_score.",
    },
    # ---- Screener ----------------------------------------------------------
    {
        "name": "screener_top",
        "title": "Agent Screener Top",
        "description": (
            "Returns a ranked list of instruments from the latest Stock Trends signal data. "
            "Filters by trend code (default: bullish states ^+, ^-, v^), RSI threshold "
            "(relative performance vs S&P 500 benchmark, baseline 100), trend persistence "
            "(trend_cnt), and trend maturity (mt_cnt). "
            "Each result includes: trend, trend_cnt, mt_cnt, rsi, rsi_updn, vol_tag, symbol_exchange. "
            "Recommended first premium endpoint for agent portfolio and signal workflows."
        ),
        "endpoint": "/v1/agent/screener/top",
        "method": "GET",
        "category": "screener",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "description": "See /v1/openapi.json for full query parameter schema.",
        },
        "output_summary": "Ranked list of instruments with trend, trend_cnt, mt_cnt, rsi, rsi_updn, vol_tag fields.",
    },
    # ---- Portfolio ---------------------------------------------------------
    {
        "name": "portfolio_construct",
        "title": "Portfolio Construct",
        "description": "Constructs a portfolio from screened candidates using Stock Trends weighting logic.",
        "endpoint": "/v1/portfolio/construct",
        "method": "POST",
        "category": "portfolio",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "description": "See /v1/openapi.json for full request body schema.",
        },
        "output_summary": "Constructed portfolio with symbol weights and selection rationale.",
    },
    {
        "name": "portfolio_evaluate",
        "title": "Portfolio Evaluate",
        "description": "Evaluates risk and return profile of an existing or constructed portfolio.",
        "endpoint": "/v1/portfolio/evaluate",
        "method": "POST",
        "category": "portfolio",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "description": "See /v1/openapi.json for full request body schema.",
        },
        "output_summary": "Portfolio risk and return metrics.",
    },
    {
        "name": "portfolio_compare",
        "title": "Portfolio Compare",
        "description": "Compares two portfolios to quantify differences in risk and return profile.",
        "endpoint": "/v1/portfolio/compare",
        "method": "POST",
        "category": "portfolio",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "description": "See /v1/openapi.json for full request body schema.",
        },
        "output_summary": "Side-by-side comparison of two portfolios.",
    },
    # ---- STIM --------------------------------------------------------------
    {
        "name": "stim_latest",
        "title": "STIM Latest",
        "description": (
            "Retrieves the latest ST-IM (Stock Trends Inference Model) outputs: forward return "
            "expectations and statistical distributions for a symbol. "
            "Covers forward return distributions across 4, 13, and 40-week horizons. "
            f"{STIM_PROVENANCE_TEXT}"
        ),
        "endpoint": "/v1/stim/latest",
        "method": "GET",
        "category": "stim",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_exchange": {
                    "type": "string",
                    "description": "Symbol and exchange, e.g. 'IBM-N'.",
                },
            },
            "required": ["symbol_exchange"],
        },
        "output_summary": "ST-IM forward return distributions for 4, 13, and 40-week horizons.",
    },
    {
        "name": "stim_history",
        "title": "STIM History",
        "description": (
            "Retrieves historical ST-IM (Stock Trends Inference Model) distribution records for a symbol. "
            "Returns forward return distribution fields across 4, 13, and 40-week horizons: "
            "xNwk1 (lower CI bound), xNwk (mean), xNwk2 (upper CI bound), xNwksd (std deviation). "
            "Ordering depends on query scope; broad queries return most recent records first. "
            f"{STIM_PROVENANCE_TEXT}"
        ),
        "endpoint": "/v1/stim/history",
        "method": "GET",
        "category": "stim",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_exchange": {
                    "type": "string",
                    "description": "Symbol and exchange, e.g. 'IBM-N'.",
                },
            },
            "required": ["symbol_exchange"],
        },
        "output_summary": "Historical ST-IM distribution records: xNwk1, xNwk, xNwk2, xNwksd across 4, 13, and 40-week horizons.",
    },
    # ---- Selections / STIM Select ------------------------------------------
    {
        "name": "selections_latest",
        "title": "Selections Latest",
        "description": (
            "Returns the latest base ST-IM selection universe for the most recent weekdate, "
            "ranked by prob13wk descending — the probability of exceeding the "
            "13-week base-period mean random return (2.19%), assuming a normal distribution. "
            "No published threshold filter is applied; all base selection records for the week are returned. "
            "Use /selections/published/latest for the three-horizon published STIM Select definition."
        ),
        "endpoint": "/v1/selections/latest",
        "method": "GET",
        "category": "selections",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {
                    "type": "string",
                    "description": "Optional exchange filter: N, Q, A, B, T, I.",
                },
                "min_prob13wk": {
                    "type": "number",
                    "description": "Optional minimum prob13wk threshold (0.0–1.0).",
                },
            },
            "required": [],
        },
        "output_summary": "weekdate, count, data[](weekdate, exchange, symbol, prob13wk, symbol_exchange).",
    },
    {
        "name": "selections_history",
        "title": "Selections History",
        "description": (
            "Returns historical base ST-IM selection records. "
            "Filter by symbol_exchange, symbol, exchange, or date range. "
            "Each entry includes prob13wk — probability of exceeding the 13-week base-period "
            "mean random return (2.19%), assuming a normal distribution. "
            "No threshold filter is applied unless min_prob13wk is set. "
            "Use /selections/published/history for the three-horizon published definition."
        ),
        "endpoint": "/v1/selections/history",
        "method": "GET",
        "category": "selections",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_exchange": {
                    "type": "string",
                    "description": "Combined symbol and exchange, e.g. 'IBM-N'.",
                },
                "symbol": {"type": "string", "description": "Ticker symbol, e.g. 'IBM'."},
                "exchange": {"type": "string", "description": "Exchange code, e.g. 'N'."},
                "start": {"type": "string", "description": "Start date YYYY-MM-DD (inclusive)."},
                "end": {"type": "string", "description": "End date YYYY-MM-DD (inclusive)."},
                "min_prob13wk": {"type": "number", "description": "Optional minimum prob13wk threshold."},
            },
            "required": [],
        },
        "output_summary": "count, data[](weekdate, exchange, symbol, prob13wk, symbol_exchange).",
    },
    {
        "name": "selections_published_latest",
        "title": "Published STIM Select Latest",
        "description": (
            "Returns the latest published STIM Select list filtered to the published definition: "
            "x4wk1 > 0% (4-week lower CI bound), x13wk1 > 2.19% (13-week), x40wk1 > 6.45% (40-week), "
            "and prob13wk >= 55% by default. "
            "Ranked by prob13wk descending. Includes full ST-IM distribution fields."
        ),
        "endpoint": "/v1/selections/published/latest",
        "method": "GET",
        "category": "selections",
        "input_schema": {
            "type": "object",
            "properties": {
                "exchange": {"type": "string", "description": "Optional exchange filter: N, Q, A, B, T, I."},
                "min_prob13wk": {"type": "number", "description": "Minimum prob13wk threshold (default 0.55)."},
            },
            "required": [],
        },
        "output_summary": "weekdate, count, data[](weekdate, exchange, symbol, prob13wk, x4wk1, x13wk1, x40wk1, symbol_exchange).",
    },
    {
        "name": "selections_published_history",
        "title": "Published STIM Select History",
        "description": (
            "Returns historical published STIM Select records filtered to the three-horizon "
            "confidence interval criteria and prob13wk threshold. "
            "Filter by symbol_exchange, symbol, exchange, or date range."
        ),
        "endpoint": "/v1/selections/published/history",
        "method": "GET",
        "category": "selections",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_exchange": {"type": "string", "description": "e.g. 'IBM-N'."},
                "symbol": {"type": "string"},
                "exchange": {"type": "string"},
                "start": {"type": "string", "description": "Start date YYYY-MM-DD."},
                "end": {"type": "string", "description": "End date YYYY-MM-DD."},
                "min_prob13wk": {"type": "number", "description": "Minimum prob13wk threshold (default 0.55)."},
            },
            "required": [],
        },
        "output_summary": "count, data[](weekdate, exchange, symbol, prob13wk, x4wk1, x13wk1, x40wk1, symbol_exchange).",
    },
]


_REGISTRY_TOOL_TEMPLATE_OVERRIDES = frozenset({
    ("/v1/agent/screener/top", "GET"),
    ("/v1/indicators/latest", "GET"),
    ("/v1/indicators/history", "GET"),
    ("/v1/prices/latest", "GET"),
    ("/v1/prices/history", "GET"),
    ("/v1/stwr/reports/latest", "GET"),
    ("/v1/stwr/reports/history", "GET"),
    ("/v1/portfolio/construct", "POST"),
    ("/v1/portfolio/evaluate", "POST"),
    ("/v1/portfolio/compare", "POST"),
    ("/v1/stim/latest", "GET"),
    ("/v1/stim/history", "GET"),
})


def _append_registry_tool_templates(templates: list[dict]) -> list[dict]:
    """Add registry tools, and let selected registry entries replace older thin templates."""
    existing = {(tool["endpoint"], tool["method"]): idx for idx, tool in enumerate(templates)}
    result = list(templates)
    for entry in iter_endpoint_metadata():
        key = (entry["path"], entry["method"])
        tool_template = build_tool_template(entry["path"])
        if tool_template is None:
            continue
        if key in existing:
            if key in _REGISTRY_TOOL_TEMPLATE_OVERRIDES:
                result[existing[key]] = tool_template
            else:
                # Inject semantic fields from registry into hand-authored templates.
                for field in ("analytical_role", "interpretation_dependency", "interpretation_guidance", "required_interpretation_steps"):
                    if field in tool_template and field not in result[existing[key]]:
                        result[existing[key]][field] = tool_template[field]
            continue
        result.append(tool_template)
        existing[key] = len(result) - 1
    return result


_TOOL_TEMPLATES = _append_registry_tool_templates(_TOOL_TEMPLATES)


def _with_input_location_metadata(tool: dict) -> dict:
    location = tool.get("input_location") or input_location_for_method(tool["method"])
    schema = tool.get("input_schema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}, "required": []}

    schema = dict(schema)
    schema.setdefault("x-stocktrends-input-location", location)
    schema.setdefault("x-stocktrends-parameter-source", location)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for prop_schema in properties.values():
            if isinstance(prop_schema, dict):
                prop_schema.setdefault("x-stocktrends-input-location", location)
                prop_schema.setdefault("x-stocktrends-parameter-source", location)
    tool["input_location"] = location
    tool["parameter_source"] = location
    tool["input_schema"] = schema
    for inputs_field in ("required_inputs", "optional_inputs"):
        inputs = tool.get(inputs_field)
        if isinstance(inputs, dict):
            enriched_inputs = {}
            for name, meta in inputs.items():
                if isinstance(meta, dict):
                    item = dict(meta)
                    item.setdefault("input_location", location)
                    item.setdefault("parameter_source", location)
                    enriched_inputs[name] = item
                else:
                    enriched_inputs[name] = meta
            tool[inputs_field] = enriched_inputs

    if location == "query":
        tool["parameters"] = tool.get("parameters") or schema_to_parameters(schema, location)
        tool.pop("request_body_schema", None)
    else:
        tool["request_body_schema"] = tool.get("request_body_schema") or schema
    return tool


_PRICING_COST_MAP_TTL_SECONDS = 30
# Tests that need deterministic pricing metadata should patch _fetch_pricing_cost_map,
# because it owns the TTL cache wrapped around the raw DB loader.
_pricing_cost_map_cache: dict[str, Decimal] | None = None
_pricing_cost_map_cached_at = 0.0


def _load_pricing_cost_map() -> dict[str, Decimal]:
    try:
        engine = get_metering_engine()
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT rule_name, cost_per_request
                    FROM api_pricing_rules
                    WHERE is_active = 1
                    """
                )
            ).mappings().all()
    except Exception:
        return {}

    cost_map: dict[str, Decimal] = {}
    for row in rows:
        try:
            rule_name = str(row["rule_name"])
            cost = row["cost_per_request"]
        except Exception:
            continue
        if rule_name and cost is not None:
            cost_map[rule_name] = Decimal(str(cost))
    return cost_map


def _fetch_pricing_cost_map() -> dict[str, Decimal]:
    global _pricing_cost_map_cache, _pricing_cost_map_cached_at

    now = time.monotonic()
    if (
        _pricing_cost_map_cache is not None
        and now - _pricing_cost_map_cached_at < _PRICING_COST_MAP_TTL_SECONDS
    ):
        return dict(_pricing_cost_map_cache)

    cost_map = _load_pricing_cost_map()
    _pricing_cost_map_cache = dict(cost_map)
    _pricing_cost_map_cached_at = now
    return cost_map


def _with_pricing_metadata(tool: dict, cost_map: dict[str, Decimal]) -> dict:
    pricing_rule_id = tool.get("pricing_rule_id")
    stc_cost = cost_map.get(pricing_rule_id) if pricing_rule_id else None
    estimated_usd_cost = stc_cost * STC_TO_USD if stc_cost is not None else None
    pricing_note = (
        "STC is the source of truth. stc_cost is resolved from /v1/pricing/catalog when "
        "available; estimated_usd_cost uses the current planning reference of 1 STC approximately 1 USD."
    )

    tool["stc_cost"] = float(stc_cost) if stc_cost is not None else None
    tool["estimated_usd_cost"] = float(estimated_usd_cost) if estimated_usd_cost is not None else None
    tool["pricing_note"] = pricing_note if pricing_rule_id else "No paid STC price applies to this discovery entry."
    tool["pricing"] = {
        "pricing_rule_id": pricing_rule_id,
        "stc_cost": tool["stc_cost"],
        "estimated_usd_cost": tool["estimated_usd_cost"],
        "cost_source": "/v1/pricing/catalog",
        "supported_rails": list(tool.get("supported_rails", [])),
        "note": tool["pricing_note"],
    }
    return tool


def _build_tools() -> list:
    """
    Build the tools list by merging each template with runtime-derived
    access metadata from _access_metadata().

    Called at request time inside ai_tools() so the manifest always
    reflects current runtime policy rather than startup-time policy.
    """
    result = []
    cost_map = _fetch_pricing_cost_map()
    for template in _TOOL_TEMPLATES:
        meta = _access_metadata(template["endpoint"], template["method"])
        tool = {**template, **meta}
        tool = _with_input_location_metadata(tool)
        tool = _with_pricing_metadata(tool, cost_map)
        result.append(tool)
    return result


def _build_workflow_summary(workflow: dict) -> dict:
    """Return a simplified MCP-friendly workflow entry from the registry."""
    return {
        "workflow_id": workflow["workflow_id"],
        "name": workflow["name"],
        "description": workflow["description"],
        "tags": workflow["tags"],
        "supported_rails": workflow["supported_rails"],
        "step_count": len(workflow["steps"]),
        "pricing_rule_ids": [
            step["pricing_rule_id"]
            for step in workflow["steps"]
            if step.get("pricing_rule_id")
        ],
        "best_for": workflow.get("best_for"),
        "analytical_role": workflow.get("analytical_role"),
        "research_goal": workflow.get("research_goal"),
        "provenance_reference": provenance_reference(),
        "agent_goal_examples": workflow.get("agent_goal_examples", []),
        "symbol_selection_guidance": workflow.get("symbol_selection_guidance"),
        "interpretation_guidance": workflow.get("interpretation_guidance"),
        "required_interpretation_steps": workflow.get("required_interpretation_steps", []),
        "next_step_guidance": workflow.get("next_step_guidance", []),
        "note": "Use GET /v1/workflows for live per-step STC costs.",
    }


def get_last_update():
    """
    Returns the most recent weekdate from st_data.
    Falls back to None if the query fails.
    """
    try:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text("SELECT MAX(weekdate) AS last_update FROM st_data"))
            row = result.fetchone()

        if row and row.last_update:
            return str(row.last_update)
    except Exception:
        return None

    return None


@router.get("/context")
def ai_context():
    last_update = get_last_update()
    contract = inference_contract()
    stim_profile = stim_provider_profile()

    return {
        "dataset": "Stock Trends Market Indicators",
        "provider": "Stock Trends Publications",
        "description": DATASET_DESCRIPTION,
        "service_description": SERVICE_POSITIONING,
        "dataset_description": DATASET_DESCRIPTION,
        "provenance_summary": AI_CONTEXT_PROVENANCE_TEXT,
        "data_provenance": data_provenance(),
        "service_identity": {
            "short_name": "Stock Trends API",
            "primary_category": "agent_native_probabilistic_market_intelligence_infrastructure",
            "dominant_description": (
                "Agent-native probabilistic market intelligence infrastructure for "
                "autonomous market-research agents."
            ),
            "is": [
                "processed_weekly_stock_trends_signals",
                "probabilistic_forward_return_inference",
                "market_regime_breadth_and_leadership_context",
                "deterministic_decision_and_portfolio_analytics",
            ],
            "is_not": [
                "raw_market_data_api",
                "generic_screener_api",
                "trade_execution_system",
                "investment_adviser",
                "investment_advice",
            ],
            "core_methodology": [
                "Stock Trends trend classification",
                "relative performance versus benchmark",
                "trend persistence and maturity",
                "long-horizon classification provenance",
                "ST-IM forward-return distributions",
                "market structure confirmation",
            ],
            "forecast_horizons_weeks": [4, 13, 40],
        },
        "cognition_architecture": {
            "source": COGNITION_ARCHITECTURE_DOC,
            "inference_contract_endpoint": INFERENCE_CONTRACT_ENDPOINT,
            "current_baseline_provider_profile": STIM_PROVIDER_PROFILE_ENDPOINT,
            "current_baseline_provider": "stim",
            "doctrine": [
                "ST-IM is the current baseline inference provider, not the final intelligence layer.",
                "Future Causal AI is a first-class future inference provider.",
                "Discovery, metadata, OpenAPI, x402/MPP surfaces, and future MCP tools should use provider-agnostic inference concepts where practical.",
                "Reasoning interfaces must preserve uncertainty, evidence, explanations, confidence, signal sources, and auditability.",
            ],
            "provider_agnostic_concepts": list(contract["provider_agnostic_concepts"].keys()),
            "available_providers": contract["available_providers"],
            "current_baseline_provider_role": stim_profile["provider_role"],
            "future_provider_compatibility": "Future Causal AI providers should integrate through the same inference contract.",
        },
        "discovery_entrypoints": {
            "primary_machine_readable": "/v1/ai/tools",
            "secondary_explanatory": "/v1/ai/context",
            "provider_agnostic_inference_contract": "/v1/meta/inference",
            "stim_provider_profile": "/v1/meta/stim",
            "docs": "/v1/docs",
            "openapi": "/v1/openapi.json",
        },
        "update_frequency": "weekly",
        "last_update": last_update,
        "coverage": {
            "region": "North America",
            "asset_types": ["equities", "ETFs"],
            "historical_coverage_start_year": 1980,
            "approximate_observation_count": "16M+",
            "forecast_horizons_weeks": [4, 13, 40]
        },
        "core_indicators": [
            "trend",
            "trend_cnt",
            "mt_cnt",
            "rsi",
            "rsi_updn",
            "vol_tag"
        ],
        "field_definitions": {
            "trend": "Primary Stock Trends trend classification for the instrument.",
            "trend_cnt": "Number of consecutive weeks the current trend classification has persisted.",
            "mt_cnt": "Number of weeks the instrument has remained in its current major trend category.",
            "rsi": "Relative performance ratio versus the S&P 500 benchmark over 13 weeks. Baseline = 100. Values >100 indicate outperformance; <100 indicate underperformance. Not the traditional Wilder RSI oscillator.",
            "rsi_updn": "Weekly direction of relative strength versus benchmark.",
            "vol_tag": "Unusual volume classification for the current week."
        },
        "analytical_framework": {
            "positioning": (
                "Not a raw market data or generic screener service. "
                "Outputs are processed, ranked, and interpretation-ready. "
                "Each endpoint family serves a distinct analytical role in a research chain."
            ),
            "primary_unit_of_analysis": "weekly Stock Trends signal state plus probabilistic forward-return context",
            "not_raw_market_data": True,
            "not_generic_screener": True,
            "rsi_benchmark_baseline": {
                "field": "rsi",
                "baseline": 100,
                "meaning": "relative performance versus benchmark; >100 outperformance, <100 underperformance",
                "not": "traditional Wilder RSI oscillator",
                "primary_definition": "/v1/meta/indicators",
                "see_also": "interpretation_dependencies.rsi",
            },
            "market_structure_context": [
                "market_regime",
                "breadth_confirmation",
                "leadership_concentration",
                "leadership_rotation",
            ],
            "endpoint_roles": {
                "market_regime_classifier": "Classifies current market regime from trend distribution. Sets portfolio analysis context.",
                "market_breadth_context": "Measures sector/industry signal participation. Confirms or contradicts regime reading.",
                "leadership_intelligence": "Identifies where RSI outperformance and bullish trend alignment are concentrated.",
                "market_intelligence_filter": "Ranks candidate securities by signal quality before deeper analysis.",
                "probabilistic_forward_inference": "ST-IM forward return distributions. Not momentum — requires base-period mean comparison.",
                "probabilistic_selection_list": "STIM Select: securities satisfying all three ST-IM lower-bound thresholds plus prob13wk >= 55%.",
                "probabilistic_selection_universe": "Base ST-IM selection universe without the published three-horizon threshold filter.",
                "symbol_signal_intelligence": "Current and historical Stock Trends indicator fields for a symbol.",
                "symbol_decision_engine": "Deterministic decision-support context combining symbol signals and regime context.",
                "portfolio_construction_engine": "Produces ranked equal-weight portfolio construction analysis from eligible signal candidates.",
                "portfolio_evaluation_engine": "Evaluates or compares portfolios against current signal and regime context.",
                "curated_signal_report": "Named STWR screening reports: Stock Trends editorial signal lists.",
                "price_context": "Weekly price rows as supporting context for signal interpretation.",
            },
        },
        "analytical_chain": {
            "description": "Recommended research sequence for a full probabilistic market intelligence workflow.",
            "steps": [
                {
                    "step": 1,
                    "role": "semantic_context",
                    "endpoint": "/v1/ai/context",
                    "purpose": "Load this conceptual operating manual before flattening endpoints into generic tools.",
                },
                {
                    "step": 2,
                    "role": "workflow_selection",
                    "endpoint": "/v1/workflows",
                    "purpose": "Choose a mission-level workflow and inspect live step costs before paid execution.",
                },
                {
                    "step": 3,
                    "role": "inference_contract",
                    "endpoint": "/v1/meta/inference",
                    "purpose": "Load the provider-agnostic inference and cognition contract before interpreting provider-specific outputs.",
                },
                {
                    "step": 4,
                    "role": "interpretation_metadata",
                    "endpoint": "/v1/meta/stim",
                    "also_use": ["/v1/meta/indicators", "/v1/leadership/definitions"],
                    "purpose": "Load ST-IM provider profile, base-period means, indicator definitions, RSI baseline semantics, and leadership definitions.",
                },
                {
                    "step": 5,
                    "role": "market_regime_classifier",
                    "endpoint": "/v1/market/regime/latest",
                    "purpose": "Classify current regime and set portfolio analysis context.",
                },
                {
                    "step": 6,
                    "role": "market_breadth_context",
                    "endpoint": "/v1/breadth/sector/latest",
                    "purpose": "Confirm regime reading with sector-level signal participation.",
                },
                {
                    "step": 7,
                    "role": "leadership_intelligence",
                    "endpoint": "/v1/leadership/summary/latest",
                    "purpose": "Identify sectors and stocks with concentrated RSI outperformance.",
                },
                {
                    "step": 8,
                    "role": "market_intelligence_filter",
                    "endpoint": "/v1/agent/screener/top",
                    "purpose": "Discover ranked signal candidates before deeper analysis.",
                },
                {
                    "step": 9,
                    "role": "probabilistic_forward_inference",
                    "endpoint": "/v1/stim/latest",
                    "purpose": "Enrich candidates with forward return distributions. Requires /v1/meta/inference and /v1/meta/stim for interpretation.",
                },
                {
                    "step": 10,
                    "role": "symbol_decision_engine",
                    "endpoint": "/v1/decision/evaluate-symbol",
                    "purpose": "Evaluate candidate with deterministic decision-support context in the current regime.",
                },
                {
                    "step": 11,
                    "role": "portfolio_construction_engine",
                    "endpoint": "/v1/portfolio/construct",
                    "purpose": "Run equal-weight portfolio construction analysis from scored candidates.",
                },
                {
                    "step": 12,
                    "role": "portfolio_evaluation_engine",
                    "endpoint": "/v1/portfolio/compare",
                    "purpose": "Compare portfolio alternatives; assess signal score and regime alignment differences.",
                },
            ],
        },
        "probabilistic_semantics": {
            "provider_agnostic_contract": {
                "endpoint": INFERENCE_CONTRACT_ENDPOINT,
                "architecture_source": COGNITION_ARCHITECTURE_DOC,
                "available_provider_profiles": {
                    "stim": STIM_PROVIDER_PROFILE_ENDPOINT,
                },
                "future_provider_compatibility": (
                    "Future causal AI providers should use the same inference vocabulary "
                    "for provider identity, horizon, distribution, confidence, evidence, "
                    "uncertainty, explanation, signal source, and auditability."
                ),
            },
            "stim_model": {
                "full_name": "Stock Trends Inference Model",
                "inference_provider_id": "stim",
                "provider_role": "current_baseline_inference_provider",
                "provider_profile_endpoint": STIM_PROVIDER_PROFILE_ENDPOINT,
                "not_final_intelligence_layer": True,
                "provenance_summary": STIM_PROVENANCE_TEXT,
                "provenance_reference": provenance_reference(),
                "output_type": "probabilistic forward return distribution",
                "not_momentum": True,
                "not": ["momentum_indicator", "simple_price_change_model", "generic_technical_indicator"],
                "horizons_weeks": [4, 13, 40],
                "fields_per_horizon": {
                    "xNwk": "expected mean return",
                    "xNwk1": "lower confidence bound",
                    "xNwk2": "upper confidence bound",
                    "xNwksd": "standard deviation",
                },
                "base_period_means_pct": {
                    "x4wk": 0.0,
                    "x13wk": 2.19,
                    "x40wk": 6.45,
                },
                "comparison_required": True,
                "comparison_rule": "Compare each ST-IM mean and lower confidence bound against the matching base-period mean return.",
                "primary_probability_field": "prob13wk",
                "randomness_assumption": "Markets are noisy, uncertain, and partly random; ST-IM estimates conditional historical tendencies.",
                "distribution_assumption": "normal approximation with central-limit-theorem intuition across large historical observation populations",
                "classification_role": (
                    "Stock Trends classifications convert weekly market behavior into "
                    "structured factor states used to form comparable historical populations."
                ),
                "limitations": [
                    "regime_shifts",
                    "non_stationarity",
                    "sample_size_weakness",
                    "tail_events",
                    "liquidity_shocks",
                    "news_shocks",
                    "uncertainty_in_individual_stock_outcomes",
                ],
                "interpretation_requirement": (
                    "Always fetch /v1/meta/inference and /v1/meta/stim before interpreting ST-IM outputs. "
                    "A positive raw mean is not bullish unless it exceeds the relevant base-period mean. "
                    "Probabilities are conditional historical tendencies, not guarantees or direct buy/sell commands."
                ),
            },
            "stim_select": {
                "description": "Securities satisfying all four publication criteria simultaneously.",
                "thresholds_source": "/v1/meta/stim",
                "criteria_context": "Current explanatory STIM Select methodology; use /v1/meta/stim for authoritative base-period threshold context.",
                "criteria": {
                    "x4wk1":    {"operator": ">",  "threshold_pct": 0.0,  "description": "4-week lower CI bound > base-period mean of 0%"},
                    "x13wk1":   {"operator": ">",  "threshold_pct": 2.19, "description": "13-week lower CI bound > base-period mean of 2.19%"},
                    "x40wk1":   {"operator": ">",  "threshold_pct": 6.45, "description": "40-week lower CI bound > base-period mean of 6.45%"},
                    "prob13wk": {"operator": ">=", "threshold": 0.55,     "description": "Probability of exceeding 13-week base-period mean >= 55%"},
                },
                "ranking": "prob13wk descending",
                "note": "Probabilistic candidates — not investment advice. Not guaranteed outcomes.",
            },
            "regime_score": {
                "formula": "bullish_pct - bearish_pct",
                "range": [-1.0, 1.0],
                "interpretation": "Portfolio analysis input, not a trade entry signal.",
                "confirmation_required": "Compare with /v1/breadth/sector/latest and /v1/leadership/summary/latest.",
            },
        },
        "endpoint_family_relationships": {
            "screener_top": {
                "endpoints": ["/v1/agent/screener/top"],
                "use_when": "broad candidate discovery",
                "semantics": "ranked Stock Trends signal filter, not a generic screener",
                "feeds": ["/v1/stim/latest", "/v1/decision/evaluate-symbol", "/v1/portfolio/construct"],
            },
            "selections_latest": {
                "endpoints": ["/v1/selections/latest", "/v1/selections/history"],
                "use_when": "filtered and ranked STIM candidate universe",
                "semantics": "base ST-IM selection records ranked by prob13wk; no published threshold filter applied",
                "interpret_with": ["/v1/meta/inference", "/v1/meta/stim", "/v1/stim/latest"],
            },
            "selections_published": {
                "endpoints": ["/v1/selections/published/latest", "/v1/selections/published/history"],
                "use_when": "strict STIM Select-style candidates",
                "semantics": "current STIM Select methodology using three-horizon lower-bound thresholds plus prob13wk publication criteria",
                "thresholds_source": "/v1/meta/stim",
                "authoritative_threshold_context": "/v1/meta/stim",
                "interpret_with": ["/v1/meta/inference", "/v1/meta/stim"],
            },
            "stim": {
                "endpoints": ["/v1/stim/latest", "/v1/stim/history"],
                "use_when": "symbol-specific probabilistic forward-return analysis",
                "semantics": "ST-IM distributions across 4, 13, and 40-week horizons",
                "interpret_with": ["/v1/meta/inference", "/v1/meta/stim", "/v1/indicators/latest"],
            },
            "indicators": {
                "endpoints": ["/v1/indicators/latest", "/v1/indicators/history"],
                "use_when": "Stock Trends signal context for a symbol",
                "semantics": INDICATORS_PROVENANCE_TEXT,
                "interpret_with": ["/v1/meta/indicators"],
            },
            "regime": {
                "endpoints": ["/v1/market/regime/latest", "/v1/market/regime/history", "/v1/market/regime/forecast"],
                "use_when": "market-structure context and portfolio bias",
                "semantics": "market-level trend distribution; confirm before using as bias input",
                "confirm_with": ["/v1/breadth/sector/latest", "/v1/leadership/summary/latest"],
            },
            "breadth": {
                "endpoints": ["/v1/breadth/sector/latest", "/v1/breadth/sector/history"],
                "use_when": "participation and confirmation",
                "semantics": "sector/industry participation behind regime and candidate quality",
                "confirm_with": ["/v1/market/regime/latest", "/v1/leadership/summary/latest"],
            },
            "leadership": {
                "endpoints": ["/v1/leadership/summary/latest", "/v1/leadership/rotation/history"],
                "use_when": "concentration and rotation analysis",
                "semantics": "where benchmark-relative performance and trend alignment are concentrated or rotating",
                "interpret_with": ["/v1/leadership/definitions"],
            },
            "decision": {
                "endpoints": ["/v1/decision/evaluate-symbol"],
                "use_when": "single-symbol decision evaluation",
                "semantics": "deterministic synthesis of symbol signal and regime context",
                "feeds": ["/v1/portfolio/evaluate", "/v1/portfolio/compare"],
            },
            "portfolio": {
                "endpoints": ["/v1/portfolio/construct", "/v1/portfolio/evaluate", "/v1/portfolio/compare"],
                "use_when": "portfolio analysis and comparison",
                "semantics": "candidate evidence, regime context, and signal structure synthesized into portfolio analysis scores",
                "depends_on": ["/v1/market/regime/latest", "/v1/agent/screener/top", "/v1/stim/latest"],
            },
        },
        "workflow_guidance": {
            "stim_forecast_review": {
                "mission": "Review one symbol's ST-IM forward-return distributions across 4, 13, and 40 weeks.",
                "start_with": ["/v1/meta/inference", "/v1/meta/stim", "/v1/instruments/resolve"],
                "core_path": ["/v1/stim/latest", "/v1/indicators/latest", "/v1/stim/history"],
                "success_condition": "Compare ST-IM means, confidence bounds, and probabilities against base-period means.",
            },
            "sector_rotation_analysis": {
                "workflow_id_available": False,
                "mission": "Evaluate regime, breadth participation, leadership concentration, and sector rotation.",
                "core_path": [
                    "/v1/market/regime/latest",
                    "/v1/breadth/sector/latest",
                    "/v1/leadership/summary/latest",
                    "/v1/leadership/rotation/history",
                ],
                "success_condition": "Separate broad participation from narrow leadership concentration before selecting candidates.",
            },
            "portfolio_build": {
                "mission": "Discover candidates and run bounded Stock Trends-informed portfolio construction analysis.",
                "core_path": ["/v1/agent/screener/top", "/v1/portfolio/construct", "/v1/portfolio/evaluate"],
                "success_condition": "Portfolio analysis output includes candidate evidence, resolved context, regime context, and score structure.",
            },
            "portfolio_compare_review": {
                "mission": "Compare an existing allocation with a Stock Trends-informed analytical alternative.",
                "core_path": ["/v1/portfolio/evaluate", "/v1/portfolio/construct", "/v1/portfolio/compare"],
                "success_condition": "Explain score_delta, winner, alignment advantage, and regime fit without presenting advice.",
            },
            "symbol_decision": {
                "mission": "Evaluate a known symbol in current regime context.",
                "start_with": ["/v1/instruments/resolve", "/v1/market/regime/latest"],
                "core_path": ["/v1/decision/evaluate-symbol", "/v1/indicators/latest", "/v1/stim/latest"],
                "success_condition": "Report deterministic decision-support context, confidence, signal notes, and ST-IM context when requested.",
            },
            "full_research_sweep": {
                "workflow_id_available": False,
                "mission": "Run the recommended analytical chain end to end with budget controls.",
                "core_path": [
                    "/v1/ai/context",
                    "/v1/workflows",
                    "/v1/meta/inference",
                    "/v1/meta/stim",
                    "/v1/market/regime/latest",
                    "/v1/breadth/sector/latest",
                    "/v1/leadership/summary/latest",
                    "/v1/agent/screener/top",
                    "/v1/stim/latest",
                    "/v1/decision/evaluate-symbol",
                    "/v1/portfolio/construct",
                    "/v1/portfolio/compare",
                ],
                "success_condition": "Produce research context that preserves regime, breadth, leadership, ST-IM, decision, and portfolio evidence.",
            },
        },
        "decision_boundaries": {
            "stock_trends_is_not": [
                "raw_market_data_api",
                "generic_screener_api",
                "trade_execution_system",
                "investment_adviser",
                "investment_advice",
            ],
            "stim_is_not": ["momentum_indicator", "simple_price_change_signal"],
            "regime_breadth_leadership": "market-structure context; do not treat as standalone trade instructions",
            "selections": "filtered or published ST-IM candidate universes, not guaranteed outcomes",
            "portfolio_outputs": "analytical synthesis for research review, not account-specific advice",
        },
        "interpretation_dependencies": {
            "stim": {
                "required_endpoint": "/v1/meta/stim",
                "provider_agnostic_contract_endpoint": "/v1/meta/inference",
                "required_before": [
                    "/v1/meta/inference",
                    "/v1/stim/latest",
                    "/v1/stim/history",
                    "/v1/selections/latest",
                    "/v1/selections/published/latest",
                ],
                "dependency": "base-period mean returns for 4, 13, and 40-week horizons",
            },
            "indicators": {
                "required_endpoint": "/v1/meta/indicators",
                "dependency": "trend codes, trend_cnt, mt_cnt, rsi, rsi_updn, and vol_tag definitions",
            },
            "rsi": {
                "benchmark": "S&P 500",
                "benchmark_baseline": 100,
                "above_baseline": "outperformance",
                "below_baseline": "underperformance",
                "not": "traditional Wilder RSI oscillator",
                "primary_definition": "/v1/meta/indicators",
                "see_also": "analytical_framework.rsi_benchmark_baseline",
            },
            "leadership": {
                "recommended_endpoint": "/v1/leadership/definitions",
                "dependency": "leadership screens, ranking fields, and taxonomy levels",
            },
            "pricing": {
                "required_endpoint": "/v1/pricing/catalog",
                "dependency": "live STC costs before autonomous paid execution",
            },
        },
        "post_endpoint_execution_semantics": {
            "scope": "analytical POST endpoints only",
            "descriptive_metadata_only": True,
            "runtime_behavior_changed": False,
            "common": {
                "state_mutation": False,
                "bounded_cost": True,
                "safe_for_autonomous_execution_with_budget_controls": True,
                "requires_auth_or_payment": True,
                "pricing_source": "/v1/pricing/catalog",
            },
            "endpoints": {
                "/v1/decision/evaluate-symbol": {
                    "state_mutation": False,
                    "bounded_cost": True,
                    "deterministic_for_identical_inputs": True,
                    "safe_for_autonomous_execution_with_budget_controls": True,
                },
                "/v1/portfolio/construct": {
                    "state_mutation": False,
                    "bounded_cost": True,
                    "deterministic_for_identical_inputs": True,
                    "determinism_scope": "same inputs, same weekly data snapshot, same eligible universe",
                    "safe_for_autonomous_execution_with_budget_controls": True,
                },
                "/v1/portfolio/evaluate": {
                    "state_mutation": False,
                    "bounded_cost": True,
                    "deterministic_for_identical_inputs": True,
                    "safe_for_autonomous_execution_with_budget_controls": True,
                },
                "/v1/portfolio/compare": {
                    "state_mutation": False,
                    "bounded_cost": True,
                    "deterministic_for_identical_inputs": True,
                    "safe_for_autonomous_execution_with_budget_controls": True,
                },
            },
        },
        "research_context_not_investment_advice": {
            "not_investment_advice": True,
            "agent_instruction": "Present outputs as probabilistic market research context, not instructions to buy or sell.",
            "no_guarantee": "ST-IM, selections, decisions, and portfolio scores are analytical outputs, not guaranteed outcomes.",
            "user_context_required": "Agents should combine outputs with user objectives, constraints, and independent review.",
        },
        "recommended_starting_paths": {
            "autonomous_agent_discovery": [
                "/v1/ai/context",
                "/v1/workflows",
                "/v1/pricing/catalog",
                "/v1/ai/tools",
            ],
            "interpretation_setup": [
                "/v1/meta/indicators",
                "/v1/meta/inference",
                "/v1/meta/stim",
                "/v1/leadership/definitions",
            ],
            "candidate_discovery": [
                "/v1/market/regime/latest",
                "/v1/breadth/sector/latest",
                "/v1/leadership/summary/latest",
                "/v1/agent/screener/top",
                "/v1/selections/latest",
                "/v1/selections/published/latest",
            ],
            "symbol_review": [
                "/v1/instruments/resolve",
                "/v1/indicators/latest",
                "/v1/stim/latest",
                "/v1/decision/evaluate-symbol",
            ],
            "portfolio_synthesis": [
                "/v1/portfolio/construct",
                "/v1/portfolio/evaluate",
                "/v1/portfolio/compare",
            ],
        },
        "trend_categories": {
            "^+": "bullish",
            "^-": "weak bullish",
            "v^": "bullish crossover",
            "v-": "bearish",
            "v+": "weak bearish",
            "^v": "bearish crossover"
        },
        "endpoint_groups": {
            "discovery": [
                "/v1/ai/tools",
                "/v1/ai/context",
                "/v1/ai/proof/market-edge",
                "/v1/docs",
                "/v1/openapi.json"
            ],
            "planning_helpers": [
                "/v1/cost-estimate",
                "/v1/workflows",
                "/v1/instruments/lookup",
                "/v1/instruments/resolve",
                "/v1/stwr/reports/catalog",
                "/v1/meta/indicators",
                "/v1/meta/inference",
                "/v1/meta/stim",
                "/v1/meta/stwr",
                "/v1/leadership/definitions",
                "/v1/ai/proof/market-edge"
            ],
            "pricing": [
                "/v1/pricing",
                "/v1/pricing/catalog",
                "/v1/workflows",
                "/v1/cost-estimate"
            ],
            "instruments": [
                "/v1/instruments/lookup",
                "/v1/instruments/resolve"
            ],
            "screening": [
                "/v1/agent/screener/top"
            ],
            "stim": [
                "/v1/stim/latest",
                "/v1/stim/history"
            ],
            "market": [
                "/v1/market/regime/latest",
                "/v1/market/regime/history",
                "/v1/market/regime/forecast"
            ],
            "decision": [
                "/v1/decision/evaluate-symbol"
            ],
            "portfolio": [
                "/v1/portfolio/construct",
                "/v1/portfolio/evaluate",
                "/v1/portfolio/compare"
            ],
            "breadth": [
                "/v1/breadth/sector/latest",
                "/v1/breadth/sector/history"
            ],
            "leadership": [
                "/v1/leadership/definitions",
                "/v1/leadership/summary/latest",
                "/v1/leadership/rotation/history"
            ],
            "selections": [
                "/v1/selections/latest",
                "/v1/selections/history",
                "/v1/selections/published/latest",
                "/v1/selections/published/history"
            ]
        },
        "access_model": {
            "public_discovery": [
                "/v1/ai/tools",
                "/v1/ai/context",
                "/v1/ai/proof/market-edge",
                "/v1/docs",
                "/v1/openapi.json"
            ],
            "subscription_supported": True,
            "x402_agent_pay": "live",
            "mpp": "live"
        },
        "auth": {
            "subscription_scheme": "X-API-Key",
            "subscription_header": "X-API-Key: YOUR_API_KEY",
            "alternative_scheme": "Bearer",
            "alternative_header": "Authorization: Bearer YOUR_API_KEY",
            "agent_identity_headers": [
                "X-StockTrends-Agent-Id",
                "X-StockTrends-Agent-Type",
                "X-StockTrends-Agent-Vendor",
                "X-StockTrends-Agent-Version"
            ],
            "payment_headers": [
                "X-StockTrends-Payment-Method",
                "X-StockTrends-Payment-Network",
                "X-StockTrends-Payment-Token",
                "X-StockTrends-Payment-Reference",
                "X-StockTrends-Payment-Amount"
            ]
        },
        "pricing": {
            "catalog_endpoint": "/v1/pricing/catalog",
            "pricing_metadata_endpoint": "/v1/pricing",
            "workflow_registry_endpoint": "/v1/workflows",
            "cost_estimate_endpoint": "/v1/cost-estimate",
            "planning_role": (
                "/v1/pricing explains payment identity, agent identity, accepted headers, and rails; "
                "/v1/pricing/catalog gives concrete endpoint prices; /v1/workflows gives task-level strategy."
            ),
            "note": "Use the live pricing catalog and API response headers as the authoritative source of endpoint pricing and payment requirements."
        },
        "usage_guidance": [
            "Start with /v1/ai/tools as the primary machine-readable manifest for agent discovery.",
            "Use /v1/ai/context as the secondary explanatory endpoint for dataset and endpoint-family context.",
            "Before calling premium endpoints, call /v1/ai/proof/market-edge (no auth required) to inspect signal structure and confirm field schemas before purchasing access.",
            "Use /v1/docs and /v1/openapi.json for exact request and response contracts.",
            "Use /v1/workflows to select a market-research strategy and endpoint sequence.",
            "Use planning helpers (/v1/cost-estimate, /v1/instruments/lookup, /v1/instruments/resolve, /v1/stwr/reports/catalog, /v1/meta/indicators, /v1/meta/inference, /v1/meta/stim, /v1/meta/stwr, /v1/leadership/definitions) to resolve symbols, estimate costs, and understand metadata before paid calls.",
            "Use /v1/pricing/catalog to discover live pricing rules before calling premium endpoints.",
            "Use /v1/pricing to understand payment identity, agent identity, accepted headers, and supported rails.",
            "For x402, inspect the HTTP 402 stocktrends_preview before payment to confirm purpose, inputs, response shape, related endpoints, pricing_rule_id, cost, and rails.",
            "Use subscription access for persistent developer workflows and x402 for agent-native pay-per-request access.",
            "Start premium agent-pay workflows with /v1/agent/screener/top or /v1/stim/latest.",
            "Cache discovery and metadata responses where appropriate because the dataset updates weekly."
        ],
        "example_queries": [
            {
                "description": "Look up an instrument by symbol",
                "path": "/v1/instruments/lookup?symbol=AAPL"
            },
            {
                "description": "Get a premium ranked screener result set",
                "path": "/v1/agent/screener/top"
            },
            {
                "description": "Retrieve the latest ST-IM distribution for one symbol",
                "path": "/v1/stim/latest?symbol_exchange=IBM-N"
            },
            {
                "description": "Retrieve the current market regime classification",
                "path": "/v1/market/regime/latest"
            }
        ],
        "recommended_first_flows": {
            "human_developer": [
                "/v1/docs",
                "/v1/openapi.json",
                "/v1/pricing/catalog",
                "/v1/stim/latest?symbol_exchange=IBM-N"
            ],
            "agent": [
                "/v1/ai/tools",
                "/v1/ai/context",
                "/v1/pricing/catalog",
                "/v1/agent/screener/top"
            ]
        },
        "docs": "https://api.stocktrends.com/v1/docs",
        "openapi": "https://api.stocktrends.com/v1/openapi.json",
        "llms_txt": "https://api.stocktrends.com/llms.txt",
        "ai_plugin": "https://api.stocktrends.com/.well-known/ai-plugin.json",
        "dataset_manifest": "https://api.stocktrends.com/ai-dataset.json",
        "tools_manifest": "https://api.stocktrends.com/v1/ai/tools",
        "license": "https://stocktrends.com/stock-trends-data-license",
        "terms": "https://stocktrends.com/terms-of-use",
        "support": {
            "email": "api@stocktrends.com"
        }
    }


# ---------------------------------------------------------------------------
# GET /v1/ai/tools — MCP/Bazaar-compatible tools manifest
# Public, non-metered, fully static (no DB calls).
# Exposes real endpoints only. Pricing costs are NOT hardcoded here;
# agents should call /v1/pricing/catalog for authoritative live STC costs.
# ---------------------------------------------------------------------------

_RECOMMENDED_WORKFLOW_IDS = {"portfolio_build", "symbol_decision", "regime_analysis"}


@router.get(
    "/tools",
    summary="MCP tools manifest",
    description=(
        "Public, non-metered. Returns the Stock Trends API as an MCP/Bazaar-compatible "
        "tools manifest and primary machine-readable entry point for agents. Exposes "
        "confirmed real endpoints only. Costs reference the STC model; use "
        "/v1/pricing/catalog for authoritative live values. "
        "Workflows are exposed in a simplified format; use /v1/workflows for live per-step costs."
    ),
)


def ai_tools():
    tools = _build_tools()
    workflows = [_build_workflow_summary(w) for w in WORKFLOW_REGISTRY]
    recommended_workflows = [w for w in workflows if w["workflow_id"] in _RECOMMENDED_WORKFLOW_IDS]

    return {
        "provider": "stocktrends",
        "version": "v1",
        "service_description": SERVICE_POSITIONING,
        "provenance_summary": AI_CONTEXT_PROVENANCE_TEXT,
        "data_provenance": data_provenance(),
        "discovery_entrypoints": {
            "primary_machine_readable": "/v1/ai/tools",
            "secondary_explanatory": "/v1/ai/context",
            "provider_agnostic_inference_contract": "/v1/meta/inference",
            "stim_provider_profile": "/v1/meta/stim",
            "docs": "/v1/docs",
            "openapi": "/v1/openapi.json",
        },
        "recommended_first_call": {
            "endpoint": "/v1/agent/screener/top",
            "method": "GET",
            "reason": "Returns top-ranked actionable screener results — highest immediate value for agent portfolio and signal workflows.",
            "auth_required": True,
            "supported_rails": ["subscription", "x402", "mpp"],
            "expected_flow": [
                "fetch /v1/ai/tools",
                "fetch /v1/workflows to choose a task-level strategy",
                "fetch /v1/pricing/catalog to resolve live STC costs",
                "fetch /v1/pricing to understand payment rails and headers",
                "call /v1/agent/screener/top with auth header",
                "if x402 rail: receive HTTP 402 challenge and inspect stocktrends_preview before paying",
                "retry with X-StockTrends-Payment-* headers to complete payment",
            ],
        },
        "quickstart": [
            {"step": 1, "action": "fetch", "path": "/v1/ai/tools", "note": "Primary machine-readable discovery. This endpoint."},
            {"step": 2, "action": "fetch", "path": "/v1/workflows", "note": "Choose a strategy and endpoint sequence for the task."},
            {"step": 3, "action": "fetch", "path": "/v1/pricing/catalog", "note": "Resolve live STC costs for each endpoint. Public under current API behavior."},
            {"step": 4, "action": "fetch", "path": "/v1/pricing", "note": "Inspect payment rails, identity headers, and x402/MPP guidance."},
            {"step": 5, "action": "call", "path": "/v1/agent/screener/top", "note": "First premium endpoint. Auth required. x402 and subscription supported."},
        ],
        "recommended_first_workflows": recommended_workflows,
        "agent_onboarding_notes": [
            "Do not hardcode STC costs. Fetch /v1/pricing/catalog at agent startup.",
            "Prefer /v1/ai/tools as the primary machine-readable entrypoint.",
            "Use /v1/ai/context for explanatory dataset context and endpoint group overviews.",
            "Use /v1/workflows to choose a task-level strategy and endpoint sequence.",
            "Use /v1/meta/inference for the provider-agnostic inference contract; use /v1/meta/stim for the current ST-IM baseline provider profile.",
            "Treat the 1980+ historical classification record and 16M+ observations as research provenance, not a guarantee of future performance.",
            "Use helper endpoints for autonomous planning: /v1/cost-estimate, /v1/instruments/lookup, /v1/instruments/resolve, /v1/stwr/reports/catalog, /v1/meta/indicators, /v1/meta/inference, /v1/meta/stim, /v1/meta/stwr, /v1/leadership/definitions, and /v1/ai/proof/market-edge.",
            "Use /v1/pricing to understand payment identity, agent identity, accepted headers, and rails.",
            "Use /v1/docs or /v1/openapi.json for exact request/response contracts.",
            "Paid endpoint entries list their supported rails; current agent-pay endpoints support subscription, x402, and mpp.",
            "For x402, inspect the HTTP 402 stocktrends_preview before payment to confirm purpose, inputs, response shape, supported rails, and cost.",
        ],
        "tools": tools,
        "workflows": workflows,
        "pricing": {
            "unit": "STC",
            "unit_description": "Stock Trends Credits. 1 STC ≈ $1 USD (reference value, not a fixed peg).",
            "model": "All endpoints price in STC. Payment rails translate STC into subscription debit, x402 amount, or MPP session debit.",
            "metadata_endpoint": "/v1/pricing",
            "catalog_endpoint": "/v1/pricing/catalog",
            "workflow_registry_endpoint": "/v1/workflows",
            "cost_estimate_endpoint": "/v1/cost-estimate",
            "x402_preview_location": "HTTP 402 response body field stocktrends_preview",
            "note": (
                "STC costs are dynamic and resolved from the pricing catalog. "
                "Do not hardcode costs — always fetch /v1/pricing/catalog at agent startup."
            ),
        },
        "auth": {
            "modes": [
                {
                    "mode": "subscription",
                    "description": "API key with active subscription. Provides monthly STC allocation.",
                    "headers": {
                        "primary": "X-API-Key: YOUR_API_KEY",
                        "alternative": "Authorization: Bearer YOUR_API_KEY",
                    },
                },
                {
                    "mode": "x402",
                    "description": "Per-request agent payment via HTTP 402 challenge/verify flow.",
                    "headers": {
                        "X-StockTrends-Payment-Method": "x402",
                        "X-StockTrends-Payment-Network": "base",
                        "X-StockTrends-Payment-Token": "USDC",
                        "X-StockTrends-Payment-Reference": "<reference>",
                        "X-StockTrends-Payment-Amount": "<amount>",
                    },
                },
                {
                    "mode": "mpp",
                    "description": "Session-based payments. STC consumed within an active payment session.",
                    "headers": {
                        "X-StockTrends-Payment-Method": "mpp",
                        "X-StockTrends-Session-Id": "<session_id>",
                    },
                },
            ],
            "agent_identity_headers": {
                "X-StockTrends-Agent-Id": "Stable external agent identifier (required for agent attribution).",
                "X-StockTrends-Agent-Type": "Agent category, e.g. 'editorial'.",
                "X-StockTrends-Agent-Vendor": "Vendor or platform operating the agent.",
                "X-StockTrends-Agent-Version": "Agent software version.",
                "X-StockTrends-Request-Purpose": "Optional statement of request purpose.",
                "X-StockTrends-Session-Id": "Optional session/workflow correlation ID.",
            },
        },
        "agent_conversion_path": {
            "proof_endpoint": "/v1/ai/proof/market-edge",
            "proof_description": (
                "Free, non-metered. Demonstrates signal structure and value proposition "
                "without requiring payment or authentication."
            ),
            "conversion_steps": [
                {
                    "step": 1,
                    "call": "GET /v1/ai/proof/market-edge",
                    "note": "No auth needed. See signal structure and value proposition.",
                },
                {
                    "step": 2,
                    "call": "GET /v1/workflows",
                    "note": "Choose a strategy and endpoint sequence for the research task.",
                },
                {
                    "step": 3,
                    "call": "GET /v1/pricing/catalog",
                    "note": "Resolve live STC costs for target endpoints.",
                },
                {
                    "step": 4,
                    "call": "GET /v1/pricing",
                    "note": "Inspect payment rails, identity headers, and accepted payment method guidance.",
                },
                {
                    "step": 5,
                    "call": "GET /v1/agent/screener/top",
                    "note": "First premium call. Supports subscription, x402, mpp.",
                },
            ],
            "payment_methods_supported": ["subscription", "x402", "mpp"],
            "on_payment_required": (
                "Selected agent-pay endpoints may return HTTP 402 with an x402 challenge "
                "when no payment has been presented. The response body contains "
                "accepted_payment_methods, pricing, payment_required, and stocktrends_preview fields. "
                "Use stocktrends_preview to confirm endpoint purpose, required inputs, safe example request, "
                "response shape, related endpoints, pricing_rule_id, STC cost, and supported rails before paying. "
                "Subscription callers receive 401/403 on auth failure, not 402. "
                "MPP uses session authorization rather than the x402 challenge flow."
            ),
        },
        "notes": [
            "Start with /v1/ai/tools as the primary machine-readable entry point for agents.",
            "Use /v1/workflows to choose a strategy, then /v1/pricing/catalog to budget each endpoint.",
            "Use /v1/instruments/lookup and /v1/instruments/resolve to produce symbol_exchange values before paid symbol workflows.",
            "Use /v1/stwr/reports/catalog and /v1/meta/* endpoints as planning helpers, not side documentation.",
            "Use /v1/pricing to understand payment rails, agent identity headers, and accepted payment headers.",
            "All paid endpoints price in STC. Fetch /v1/pricing/catalog at agent startup.",
            "For x402, inspect the 402 stocktrends_preview before paying.",
            "Use /v1/cost-estimate to plan STC spend before executing a workflow.",
            "Use /v1/ai/context as the secondary explanatory endpoint for dataset and endpoint overview.",
            "Use data_provenance for historical coverage, observation count, semantic continuity, and limitations.",
            "See /v1/docs and /v1/openapi.json for exact request/response contracts.",
        ],
    }


# ---------------------------------------------------------------------------
# GET /v1/ai/proof/market-edge — free, non-metered, public proof-of-value
# No auth, no DB calls, no billing record.  All data is synthetic/illustrative.
# ---------------------------------------------------------------------------

_PROOF_CACHE_MAX_AGE = 3600  # seconds

_PROOF_STATIC_BODY: dict = {
    "endpoint": "/v1/ai/proof/market-edge",
    "version": "v1",
    "cache_policy": {
        "max_age_seconds": _PROOF_CACHE_MAX_AGE,
        "strategy": "static",
        "note": "This response is static. No live or real-time market data is included.",
    },
    "agent_guidance": {
        "purpose": (
            "Demonstrates Stock Trends signal structure and value proposition "
            "without requiring payment or authentication."
        ),
        "next_steps": [
            {
                "step": 1,
                "call": "GET /v1/pricing/catalog",
                "note": "Resolve live STC costs for target endpoints.",
            },
            {
                "step": 2,
                "call": "GET /v1/agent/screener/top",
                "note": "Live premium signals. Auth or payment required.",
            },
            {
                "step": 3,
                "call": "GET /v1/workflows",
                "note": "Discover multi-step agent workflows with per-step STC costs.",
            },
        ],
        "on_payment_required": (
            "Selected agent-pay endpoints may return HTTP 402 with an x402 challenge "
            "when no payment has been presented. Inspect the response body for "
            "accepted_payment_methods and payment_required fields. "
            "The PAYMENT-REQUIRED response header carries base64-encoded x402 requirements. "
            "Subscription callers receive 401/403 on auth failure, not 402. "
            "MPP uses session authorization rather than the x402 challenge flow."
        ),
    },
    "value_proposition": {
        "headline": (
            "Stock Trends delivers processed, ranked, actionable signals — not raw prices."
        ),
        "differentiators": [
            "Proprietary trend classification across 2000+ North American equities and ETFs",
            "Weekly structured signals: trend state, trend persistence (trend_cnt), trend maturity (mt_cnt), relative strength (rsi), volume context (vol_tag)",
            "ST-IM (Stock Trends Inference Model): forward return expectations and statistical distributions across 4, 13, and 40-week horizons",
            "Sector breadth context for market regime detection",
            "Agent-optimized structured JSON with consistent scoring fields",
            "Multi-rail payments: subscription, x402 (per-request), MPP (session-based)",
        ],
        "vs_raw_price": (
            "A raw price API returns a number. Stock Trends returns a ranked trend signal set, "
            "regime context, and a structured workflow-ready response "
            "— all in one call."
        ),
    },
    "market_snapshot": {
        "note": (
            "SYNTHETIC DATA ONLY — not live, not real-time, not actionable. "
            "Symbols are impossible synthetic identifiers, not real tickers. "
            "Reflects real response structure."
        ),
        "as_of": "synthetic",
        "instruments": [
            {
                "symbol": "SAMPLE_A1",
                "trend": "^+",
                "trend_cnt": 12,
                "mt_cnt": 8,
                "rsi": 118,
                "rsi_updn": "+",
                "vol_tag": "*",
                "rank": 1,
            },
            {
                "symbol": "SAMPLE_B2",
                "trend": "^-",
                "trend_cnt": 7,
                "mt_cnt": 12,
                "rsi": 103,
                "rsi_updn": "+",
                "vol_tag": "",
                "rank": 8,
            },
            {
                "symbol": "SAMPLE_C3",
                "trend": "v^",
                "trend_cnt": 2,
                "mt_cnt": 1,
                "rsi": 97,
                "rsi_updn": "-",
                "vol_tag": "",
                "rank": 47,
            },
            {
                "symbol": "SAMPLE_D4",
                "trend": "v-",
                "trend_cnt": 5,
                "mt_cnt": 3,
                "rsi": 88,
                "rsi_updn": "-",
                "vol_tag": "",
                "rank": 189,
            },
            {
                "symbol": "SAMPLE_E5",
                "trend": "v+",
                "trend_cnt": 11,
                "mt_cnt": 7,
                "rsi": 72,
                "rsi_updn": "-",
                "vol_tag": "!!",
                "rank": 387,
            },
        ],
        "sector_summary": {
            "top_sector": "Technology",
            "bottom_sector": "Utilities",
            "breadth_signal": "bullish_expansion",
        },
    },
    "signal_highlights": [
        {
            "signal_type": "bullish_trend_entry",
            "description": (
                "Illustrative: instruments in bullish trend states (^+, ^-) with high "
                "RSI and persistent trend_cnt represent top-ranked candidates in the screener."
            ),
            "note": "Live ranked signals available via /v1/agent/screener/top.",
        },
        {
            "signal_type": "sector_rotation",
            "description": (
                "Illustrative: sector breadth signals identify regime shifts "
                "before they appear in index prices."
            ),
            "note": "Live sector context available via /v1/breadth/sector/latest.",
        },
    ],
    "sample_workflow": {
        "name": "agent_market_edge",
        "description": "Recommended workflow to extract signal edge from Stock Trends",
        "steps": [
            {
                "step": 1,
                "call": "GET /v1/ai/tools",
                "purpose": "Discover all available tools and payment options",
            },
            {
                "step": 2,
                "call": "GET /v1/pricing/catalog",
                "purpose": "Resolve live STC costs",
            },
            {
                "step": 3,
                "call": "GET /v1/agent/screener/top",
                "purpose": "Retrieve top-ranked live signals",
            },
            {
                "step": 4,
                "call": "GET /v1/stim/latest",
                "purpose": "Get ST-IM (Stock Trends Inference Model) forward return expectations and statistical distributions for a symbol",
            },
            {
                "step": 5,
                "call": "POST /v1/portfolio/construct",
                "purpose": "Construct portfolio from signal output",
            },
        ],
    },
    "conversion_prompt": {
        "action": (
            "Access live signals by authenticating with a subscription API key, "
            "or initiate per-request payment via x402 or a session via MPP."
        ),
        "start_here": "/v1/ai/tools",
        "pricing": "/v1/pricing",
        "payment_methods": ["subscription", "x402", "mpp"],
        "payment_notes": {
            "subscription": (
                "API key with active plan. Monthly STC allocation. "
                "Header: X-API-Key or Authorization: Bearer."
            ),
            "x402": (
                "Per-request payment via HTTP 402 challenge/verify flow. "
                "Network: Base (eip155:8453). Token: USDC. "
                "Response header: PAYMENT-REQUIRED (base64-encoded requirements)."
            ),
            "mpp": (
                "Session-based payments. STC consumed within an active session. "
                "Header: X-StockTrends-Session-Id."
            ),
        },
    },
}


@router.get(
    "/proof/market-edge",
    summary="Free proof-of-value endpoint for agent discovery",
    description=(
        "Public, non-metered. Returns a static, synthetic demonstration of Stock Trends "
        "signal structure and value proposition. No authentication required. "
        "No live or real-time market data is included — all instrument data is fictional. "
        "Intended as a no-cost entry point for autonomous agents evaluating the API."
    ),
)
def ai_proof_market_edge(response: Response) -> dict:
    response.headers["Cache-Control"] = f"public, max-age={_PROOF_CACHE_MAX_AGE}"
    return {
        **_PROOF_STATIC_BODY,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
