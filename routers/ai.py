from decimal import Decimal
from datetime import datetime, timezone

from fastapi import APIRouter, Response
from sqlalchemy import text
from db import get_engine, get_metering_engine
from routers.workflows import WORKFLOW_REGISTRY, STC_TO_USD
from discovery.endpoint_metadata import build_tool_template, iter_endpoint_metadata
from pricing.classifier import classify_request as _classify_request, NON_METERED_PATHS
from payments.policy_provider import (
    is_free_metered_path as _is_free_metered_path,
    get_effective_endpoint_payment_policy as _get_endpoint_policy,
    is_agent_pay_route as _is_agent_pay_route,
    get_agent_pay_auth_bypass_methods as _get_agent_pay_bypass_methods,
)

router = APIRouter(prefix="/ai", tags=["ai"])

SERVICE_POSITIONING = (
    "Autonomous portfolio intelligence API for AI agents. Provides market regime analysis, "
    "Stock Trends signal intelligence, probabilistic forward-return modeling, stock selection, "
    "portfolio construction, portfolio comparison, and symbol decision evaluation for 4-40 week "
    "investment horizons. Built for agentic research workflows using x402 and MPP payment rails."
)

WORKFLOW_ID_EXAMPLES = [
    "regime_analysis",
    "symbol_decision",
    "portfolio_build",
    "portfolio_compare_review",
]

# ---------------------------------------------------------------------------
# _MANIFEST_PUBLIC_PATHS mirrors ApiKeyMiddleware.public_paths for the paths
# that appear in this manifest.  Tests verify this stays in sync.
# ---------------------------------------------------------------------------
_MANIFEST_PUBLIC_PATHS: frozenset = frozenset({
    "/v1/openapi.json",
    "/v1/pricing",
    "/v1/pricing/catalog",
    "/v1/workflows",
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
      5. Auth-required non-metered     → None  (e.g. /v1/cost-estimate)
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

    if endpoint_policy and endpoint_policy.pricing_rule_id:
        # Exact endpoint policy: rule and rails are stable.
        pricing_rule_id = endpoint_policy.pricing_rule_id
        supported_rails = list(endpoint_policy.allowed_rails)
    elif _is_free_metered_path(path):
        # Tracked but not billed; rule is stable.
        pricing_rule_id = "default_free_metered"
        supported_rails = []
    elif _is_agent_pay_route(path, method.upper()) and not endpoint_policy:
        # STIM prefix paths: the runtime rule ID depends on the caller's
        # access method (subscription vs agent-pay) so it cannot be
        # represented as a single static value. Use None to avoid drift.
        # Rails: subscription is always valid for paid callers; agent-pay
        # bypass methods come from the runtime config (not hardcoded).
        pricing_rule_id = None
        agent_pay_rails = list(_get_agent_pay_bypass_methods(path, method.upper()))
        supported_rails = ["subscription"] + agent_pay_rails
    elif decision.is_metered:
        # Subscription-covered metered path (e.g. /v1/pricing/catalog).
        pricing_rule_id = decision.log_pricing_rule_id
        supported_rails = ["subscription"]
    elif path not in _MANIFEST_PUBLIC_PATHS:
        # Auth-required, non-metered (e.g. /v1/cost-estimate).
        pricing_rule_id = None
        supported_rails = ["subscription"]
    else:
        # Truly free/public.
        pricing_rule_id = None
        supported_rails = []

    return {
        "auth_required": path not in _MANIFEST_PUBLIC_PATHS,
        "metered": bool(decision.is_metered),
        "pricing_rule_id": pricing_rule_id,
        "supported_rails": supported_rails,
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
            "api_pricing_rules. Use this to understand available multi-step workflows and their costs."
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
            "Costs resolved from live pricing rules. Requires a valid API key. Non-metered."
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
            "mt_cnt, rsi, rsi_updn, and vol_tag."
        ),
        "endpoint": "/v1/meta/indicators",
        "method": "GET",
        "category": "planning_helper",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "safe_example_request": {"method": "GET", "path": "/v1/meta/indicators", "query": {}},
        "output_summary": "Indicator field definitions and trend-code meanings.",
    },
    {
        "name": "meta_stim",
        "title": "ST-IM Metadata",
        "description": (
            "Planning helper explaining ST-IM fields, base-period mean returns, confidence bounds, "
            "and 4, 13, and 40 week horizons."
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
            "Covers forward return distributions across 4, 13, and 40-week horizons."
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
            "Ordering depends on query scope; broad queries return most recent records first."
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
            "Returns the latest st_select stock list for the most recent weekdate, "
            "ranked by prob13wk descending — the probability of exceeding the "
            "13-week base-period mean random return (2.19%), assuming a normal distribution. "
            "No threshold filter is applied; all st_select records for the week are returned. "
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
            "Returns historical st_select records. "
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
            continue
        result.append(tool_template)
        existing[key] = len(result) - 1
    return result


_TOOL_TEMPLATES = _append_registry_tool_templates(_TOOL_TEMPLATES)


def _input_location_for_method(method: str) -> str:
    return "query" if method.upper() in {"GET", "HEAD", "DELETE"} else "body"


def _schema_to_parameters(schema: dict, location: str) -> list[dict]:
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    parameters = []
    for name, prop_schema in properties.items():
        param = {
            "name": name,
            "in": location,
            "required": name in required,
            "schema": prop_schema if isinstance(prop_schema, dict) else {},
        }
        if isinstance(prop_schema, dict):
            if prop_schema.get("description"):
                param["description"] = prop_schema["description"]
            if "example" in prop_schema:
                param["example"] = prop_schema["example"]
        if location == "query":
            param["style"] = "form"
            param["explode"] = True
        parameters.append(param)
    return parameters


def _with_input_location_metadata(tool: dict) -> dict:
    location = tool.get("input_location") or _input_location_for_method(tool["method"])
    schema = tool.get("input_schema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}, "required": []}

    schema = dict(schema)
    schema.setdefault("x-stocktrends-input-location", location)
    tool["input_location"] = location
    tool["input_schema"] = schema

    if location == "query":
        tool["parameters"] = tool.get("parameters") or _schema_to_parameters(schema, location)
        tool.pop("request_body_schema", None)
    else:
        tool["request_body_schema"] = tool.get("request_body_schema") or schema
    return tool


def _fetch_pricing_cost_map() -> dict[str, Decimal]:
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
        "pricing_rule_ids": [step["pricing_rule_id"] for step in workflow["steps"]],
        "best_for": workflow.get("best_for"),
        "agent_goal_examples": workflow.get("agent_goal_examples", []),
        "symbol_selection_guidance": workflow.get("symbol_selection_guidance"),
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

    return {
        "dataset": "Stock Trends Market Indicators",
        "provider": "Stock Trends Publications",
        "description": SERVICE_POSITIONING,
        "dataset_description": "Weekly structured market intelligence dataset covering North American equities and ETFs, including Stock Trends trend classification, trend persistence, trend maturity, relative strength, unusual volume signals, breadth, leadership, ST-IM (Stock Trends Inference Model) forward return distributions, market regime analytics, and deterministic decision/portfolio workflows.",
        "discovery_entrypoints": {
            "primary_machine_readable": "/v1/ai/tools",
            "secondary_explanatory": "/v1/ai/context",
            "docs": "/v1/docs",
            "openapi": "/v1/openapi.json",
        },
        "update_frequency": "weekly",
        "last_update": last_update,
        "coverage": {
            "region": "North America",
            "asset_types": ["equities", "ETFs"],
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
                "/v1/workflows",
                "/v1/instruments/lookup",
                "/v1/instruments/resolve",
                "/v1/stwr/reports/catalog",
                "/v1/meta/indicators",
                "/v1/meta/stim",
                "/v1/meta/stwr",
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
            "Use /v1/workflows to select an investment-research strategy and recommended endpoint sequence.",
            "Use planning helpers (/v1/instruments/lookup, /v1/instruments/resolve, /v1/stwr/reports/catalog, /v1/meta/indicators, /v1/meta/stim, /v1/meta/stwr) to resolve symbols and understand metadata before paid calls.",
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
        "discovery_entrypoints": {
            "primary_machine_readable": "/v1/ai/tools",
            "secondary_explanatory": "/v1/ai/context",
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
            "Use helper endpoints for autonomous planning: /v1/instruments/lookup, /v1/instruments/resolve, /v1/stwr/reports/catalog, /v1/meta/indicators, /v1/meta/stim, /v1/meta/stwr, and /v1/ai/proof/market-edge.",
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
                "STC costs are dynamic and resolved from api_pricing_rules. "
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
