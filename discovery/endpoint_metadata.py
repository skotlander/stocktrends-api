# discovery/endpoint_metadata.py
#
# Central, static endpoint metadata for machine-plannable discovery and x402
# challenges. This module is intentionally data-only: no database, router,
# pricing, or payment imports. Costs remain resolved at runtime from the STC
# pricing engine; this registry only names the pricing_rule_id and describes
# structure, inputs, and strategy.

from __future__ import annotations

import copy
import json
from typing import Any


SUPPORTED_RAILS = ["subscription", "x402", "mpp"]

SERVICE_NAME = "Stock Trends API"
SERVICE_CATEGORY = "agent_native_probabilistic_market_intelligence"
DEVELOPER_PORTAL_URL = "https://developer.stocktrends.com/"
AI_CONTEXT_URL = "https://api.stocktrends.com/v1/ai/context"
TOOLS_MANIFEST_URL = "https://api.stocktrends.com/v1/ai/tools"
WORKFLOWS_URL = "https://api.stocktrends.com/v1/workflows"
PRICING_CATALOG_URL = "https://api.stocktrends.com/v1/pricing/catalog"
COMPACT_SAFE_EXAMPLE_MAX_BYTES = 384

# ---------------------------------------------------------------------------
# Canonical analytical role constants.
# Endpoint roles describe WHAT an endpoint does analytically.
# Workflow roles (in routers/workflows.py) end in "_workflow" and describe
# a multi-step research goal — they are a distinct taxonomy.
# ---------------------------------------------------------------------------
ROLE_MARKET_INTELLIGENCE_FILTER = "market_intelligence_filter"
ROLE_MARKET_REGIME_CLASSIFIER = "market_regime_classifier"
ROLE_MARKET_BREADTH_CONTEXT = "market_breadth_context"
ROLE_LEADERSHIP_INTELLIGENCE = "leadership_intelligence"
ROLE_PROBABILISTIC_FORWARD_INFERENCE = "probabilistic_forward_inference"
ROLE_PROBABILISTIC_SELECTION_LIST = "probabilistic_selection_list"
ROLE_PROBABILISTIC_SELECTION_UNIVERSE = "probabilistic_selection_universe"
ROLE_SYMBOL_SIGNAL_INTELLIGENCE = "symbol_signal_intelligence"
ROLE_SYMBOL_DECISION_ENGINE = "symbol_decision_engine"
ROLE_PORTFOLIO_CONSTRUCTION_ENGINE = "portfolio_construction_engine"
ROLE_PORTFOLIO_EVALUATION_ENGINE = "portfolio_evaluation_engine"
ROLE_CURATED_SIGNAL_REPORT = "curated_signal_report"
ROLE_PRICE_CONTEXT = "price_context"

EXCHANGE_ENUM = ["N", "Q", "A", "B", "T", "I"]
US_EXCHANGE_ENUM = ["N", "Q", "A"]

SYMBOL_EXCHANGE_INPUT = {
    "type": "string",
    "required": True,
    "example": "IBM-N",
    "safe_default_for_demo": "IBM-N",
    "pattern": "^[A-Z0-9.]+-[A-Z]$",
    "description": "Stock Trends symbol plus exchange suffix.",
}

SYMBOL_EXCHANGE_PROPERTY_SCHEMA = {
    "type": "string",
    "example": "IBM-N",
    "pattern": "^[A-Z0-9.]+-[A-Z]$",
    "description": "Stock Trends symbol plus exchange suffix.",
}

SYMBOL_INPUT = {
    "type": "string",
    "required": False,
    "example": "IBM",
    "description": "Ticker symbol. Use with exchange when symbol_exchange is not supplied.",
}

EXCHANGE_INPUT = {
    "type": "string",
    "required": False,
    "enum": EXCHANGE_ENUM,
    "example": "N",
    "description": "Stock Trends exchange suffix. Common examples: N=NYSE, Q=NASDAQ, A=AMEX, T=TSX.",
}

CS_ONLY_INPUT = {
    "type": "boolean",
    "required": False,
    "safe_default": True,
    "example": True,
    "description": "Filter to Common Stocks only where the endpoint supports instrument type filtering.",
}

START_INPUT = {
    "type": "string",
    "required": False,
    "format": "date",
    "example": "2025-01-03",
    "description": "Inclusive start weekdate in YYYY-MM-DD format.",
}

END_INPUT = {
    "type": "string",
    "required": False,
    "format": "date",
    "example": "2025-12-26",
    "description": "Inclusive end weekdate in YYYY-MM-DD format.",
}

STIM_INTERPRETATION_DEPENDENCY = {
    "endpoint": "/v1/meta/stim",
    "method": "GET",
    "required_before_interpretation": True,
    "reason": "Base-period mean returns are required to interpret ST-IM means and probabilities correctly.",
}

STIM_INTERPRETATION_GUIDANCE = {
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
        "Call /v1/meta/stim before interpreting ST-IM results.",
        "Raw x4wk/x13wk/x40wk means are estimated mean returns, not standalone bullish signals.",
        "Do not interpret a positive raw mean as bullish unless it exceeds the relevant base mean or has adequate probability of exceeding it.",
        "Use x4wksd/x13wksd/x40wksd to estimate probability of exceeding base-period means.",
        "If is_stale=true or missing_reason is present, treat the ST-IM result as historical fallback and disclose that limitation.",
    ],
    "stim_select_style_logic": {
        "prob13wk_minimum": 0.55,
        "prob13wk_minimum_description": "13-week probability of exceeding the base-period mean should be at least 55%.",
        "lower_confidence_bounds": "Emphasize lower confidence bounds relative to base-period means where applicable.",
    },
}

STIM_REQUIRED_INTERPRETATION_STEPS = [
    "Fetch GET /v1/meta/stim.",
    "Read base_period_mean_returns_pct.x4wk, x13wk, and x40wk.",
    "For each horizon, compare xNwk to the matching base mean.",
    "Compute delta_vs_base = stim_mean - base_mean.",
    "Compute z = (base_mean - stim_mean) / standard_deviation.",
    "Compute probability_outperform = 1 - normal_cdf(z).",
    "Review lower confidence bounds against base-period means where available.",
    "Disclose stale or fallback data when is_stale=true or missing_reason is present.",
]

REGIME_INTERPRETATION_GUIDANCE = {
    "regime_score_scale": {
        "range": [-1.0, 1.0],
        "formula": "bullish_pct - bearish_pct",
        "strong_bullish": "> 0.5",
        "mixed": "-0.1 to 0.1",
        "strong_bearish": "< -0.5",
    },
    "interpretation_rules": [
        "regime_score = bullish_pct minus bearish_pct across all active trend signals.",
        "Do not use regime_score as a trade entry signal; use it as a portfolio bias input.",
        "avg_rsi > 100 indicates the average universe security outperforms the S&P 500 benchmark.",
        "avg_mt_cnt reveals whether the current regime is early-stage or mature.",
        "Confirm with /v1/market/regime/history before acting on a single regime reading.",
    ],
    "downstream_workflow": (
        "Use regime result to set bias in /v1/agent/screener/top or /v1/portfolio/construct. "
        "Confirm with /v1/breadth/sector/latest and /v1/leadership/summary/latest."
    ),
    "confirmation_endpoints": ["/v1/breadth/sector/latest", "/v1/leadership/summary/latest"],
}

STIM_SELECT_INTERPRETATION_GUIDANCE = {
    "publication_criteria": {
        "x4wk1":    {"operator": ">",  "threshold_pct": 0.0,  "description": "4-week ST-IM lower CI bound must exceed base-period mean of 0%."},
        "x13wk1":   {"operator": ">",  "threshold_pct": 2.19, "description": "13-week ST-IM lower CI bound must exceed base-period mean of 2.19%."},
        "x40wk1":   {"operator": ">",  "threshold_pct": 6.45, "description": "40-week ST-IM lower CI bound must exceed base-period mean of 6.45%."},
        "prob13wk": {"operator": ">=", "threshold": 0.55,     "description": "Probability of exceeding 13-week base-period mean must be at least 55%."},
        "all_criteria_required": True,
    },
    "interpretation_rules": [
        "All four publication criteria must be satisfied simultaneously.",
        "prob13wk >= 0.55 is the documented publication threshold.",
        "Rank by prob13wk descending; higher prob13wk means stronger 13-week outperformance probability.",
        "These are probabilistic candidates, not guaranteed outcomes — not investment advice.",
        "Use /v1/stim/latest on individual symbols to see full distribution context.",
    ],
    "base_period_context_endpoint": "/v1/meta/stim",
}


def _symbol_lookup_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "symbol_exchange": copy.deepcopy(SYMBOL_EXCHANGE_INPUT),
        },
        {
            "symbol": copy.deepcopy(SYMBOL_INPUT),
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
            "cs_only": copy.deepcopy(CS_ONLY_INPUT),
        },
    )


def _date_inputs(default_limit: int, max_limit: int) -> dict[str, Any]:
    return {
        "start": copy.deepcopy(START_INPUT),
        "end": copy.deepcopy(END_INPUT),
        "limit": {
            "type": "integer",
            "required": False,
            "safe_default": default_limit,
            "minimum": 1,
            "maximum": max_limit,
            "example": default_limit,
            "description": "Maximum number of rows returned.",
        },
    }


def _metadata(
    *,
    path: str,
    method: str,
    tool_name: str,
    title: str,
    category: str,
    pricing_rule_id: str,
    resource_description: str,
    bazaar_output_description: str,
    purpose: str,
    investment_agent_value: str,
    workflow_role: str,
    output_summary: str,
    response_shape: list[str],
    example_object: dict[str, Any],
    safe_example_request: dict[str, Any],
    required_inputs: dict[str, Any] | None = None,
    optional_inputs: dict[str, Any] | None = None,
    input_rule: str | None = None,
    notes: list[str] | None = None,
    related_endpoints: list[str] | None = None,
    next_recommended_calls: list[str] | None = None,
    tags: list[str] | None = None,
    supported_rails: list[str] | None = None,
    access_type: str = "paid",
    requires_payment: bool = True,
    analytical_role: str | None = None,
    interpretation_dependency: dict[str, Any] | None = None,
    interpretation_guidance: dict[str, Any] | None = None,
    required_interpretation_steps: list[str] | None = None,
) -> dict[str, Any]:
    metadata = {
        "path": path,
        "method": method,
        "tool_name": tool_name,
        "title": title,
        "category": category,
        "pricing_rule_id": pricing_rule_id,
        "supported_rails": list(SUPPORTED_RAILS if supported_rails is None else supported_rails),
        "access_type": access_type,
        "requires_payment": requires_payment,
        "resource_description": resource_description,
        "bazaar_output": {
            "type": "json",
            "description": bazaar_output_description,
            "example": copy.deepcopy(example_object),
        },
        "purpose": purpose,
        "investment_agent_value": investment_agent_value,
        "workflow_role": workflow_role,
        "input_rule": input_rule,
        "required_inputs": required_inputs or {},
        "optional_inputs": optional_inputs or {},
        "safe_example_request": safe_example_request,
        "response_shape": response_shape,
        "example_object": example_object,
        "output_summary": output_summary,
        "notes": notes or [],
        "related_endpoints": related_endpoints or [],
        "next_recommended_calls": next_recommended_calls or [],
        "tags": tags or [category],
    }
    if analytical_role is not None:
        metadata["analytical_role"] = analytical_role
    if interpretation_dependency is not None:
        metadata["interpretation_dependency"] = copy.deepcopy(interpretation_dependency)
    if interpretation_guidance is not None:
        metadata["interpretation_guidance"] = copy.deepcopy(interpretation_guidance)
    if required_interpretation_steps is not None:
        metadata["required_interpretation_steps"] = copy.deepcopy(required_interpretation_steps)
    return metadata


_REQ_SYMBOL, _OPT_SYMBOL = _symbol_lookup_inputs()

_ENDPOINT_METADATA_BY_PATH: dict[str, dict[str, Any]] = {
    "/v1/agent/screener/top": _metadata(
        path="/v1/agent/screener/top",
        method="GET",
        tool_name="screener_top",
        title="Agent Screener Top",
        category="screener",
        pricing_rule_id="agent_screener_top",
        resource_description=(
            "Ranked Stock Trends signal screener for agent workflows, using trend state, "
            "trend persistence, trend maturity, relative performance, and volume signal fields."
        ),
        bazaar_output_description=(
            "Returns ranked instruments with trend, trend_cnt, mt_cnt, rsi, rsi_updn, vol_tag, "
            "weekdate, and symbol_exchange fields."
        ),
        purpose="Find a ranked candidate set from the latest Stock Trends signal data.",
        investment_agent_value=(
            "Gives agents a compact first paid call for discovering securities with directional "
            "Stock Trends signal context before deeper symbol, ST-IM, or portfolio analysis."
        ),
        workflow_role="Candidate discovery and ranking.",
        output_summary="Ranked instruments with Stock Trends signal fields and filter summary.",
        response_shape=[
            "request_id", "screener", "weekdate", "filter_summary", "count", "total_matched",
            "results[].rank", "results[].symbol", "results[].exchange", "results[].symbol_exchange",
            "results[].trend", "results[].trend_cnt", "results[].mt_cnt", "results[].rsi",
            "results[].rsi_updn", "results[].vol_tag", "results[].weekdate",
        ],
        example_object={
            "request_id": "req_demo",
            "weekdate": "YYYY-MM-DD",
            "count": 1,
            "results": [
                {
                    "rank": 1,
                    "symbol_exchange": "SAMPLE-N",
                    "trend": "^+",
                    "trend_cnt": 8,
                    "mt_cnt": 12,
                    "rsi": 118,
                    "rsi_updn": "+",
                    "vol_tag": "*",
                }
            ],
        },
        safe_example_request={
            "method": "GET",
            "path": "/v1/agent/screener/top",
            "query": {"limit": 10, "min_rsi": 40},
        },
        optional_inputs={
            "limit": {"type": "integer", "required": False, "safe_default": 25, "minimum": 1, "maximum": 100},
            "min_rsi": {
                "type": "integer",
                "required": False,
                "safe_default": 40,
                "example": 40,
                "description": "Minimum Stock Trends RSI threshold. RSI baseline is 100.",
            },
            "exchange": {"type": "string", "required": False, "enum": EXCHANGE_ENUM},
        },
        analytical_role=ROLE_MARKET_INTELLIGENCE_FILTER,
        notes=[
            "RSI is relative performance versus benchmark with baseline 100.",
            "Use the free /v1/ai/proof/market-edge endpoint for a synthetic signal structure example.",
        ],
        related_endpoints=["/v1/decision/evaluate-symbol", "/v1/portfolio/construct", "/v1/stim/latest"],
        next_recommended_calls=["/v1/decision/evaluate-symbol", "/v1/portfolio/construct"],
    ),
    "/v1/indicators/latest": _metadata(
        path="/v1/indicators/latest",
        method="GET",
        tool_name="indicators_latest",
        title="Indicators Latest",
        category="indicators",
        pricing_rule_id="indicators_latest_paid",
        resource_description=(
            "Latest Stock Trends indicator snapshot for a symbol, including trend state, trend age, "
            "relative performance, volume tag, and signal fields used by Stock Trends research workflows."
        ),
        bazaar_output_description=(
            "Returns latest Stock Trends indicator fields for a requested symbol, including "
            "symbol_exchange, weekdate, trend, trend_cnt, mt_cnt, rsi, rsi_updn, vol_tag, and related signal metadata."
        ),
        purpose="Retrieve the latest weekly Stock Trends indicator row for one instrument.",
        investment_agent_value=(
            "Lets an investment agent inspect current structural direction, trend persistence, "
            "trend maturity, relative performance, and volume context before deciding whether to "
            "request history, ST-IM distributions, or portfolio evaluation."
        ),
        workflow_role="Single-symbol signal confirmation.",
        input_rule="Provide symbol_exchange, or provide both symbol and exchange.",
        required_inputs=copy.deepcopy(_REQ_SYMBOL),
        optional_inputs={**copy.deepcopy(_OPT_SYMBOL)},
        safe_example_request={
            "method": "GET",
            "path": "/v1/indicators/latest",
            "query": {"symbol_exchange": "IBM-N", "cs_only": True},
        },
        response_shape=[
            "request_id", "symbol_exchange", "weekdate", "exchange", "symbol", "type",
            "currency_code", "trend", "trend_cnt", "mt_cnt", "prev_mtcnt", "rsi",
            "rsi_updn", "vol_tag", "rvol", "atv", "fpr_chg1", "fpr_chg2",
            "fpr_chg4", "fpr_chg13", "fpr_chg40", "pr_chg13", "pr_change",
            "shortavg", "longavg", "yr_hi", "yr_lo",
        ],
        example_object={
            "request_id": "req_demo",
            "symbol_exchange": "SAMPLE-N",
            "weekdate": "YYYY-MM-DD",
            "trend": "^+",
            "trend_cnt": 8,
            "mt_cnt": 12,
            "rsi": 118,
            "rsi_updn": "+",
            "vol_tag": "*",
        },
        output_summary=(
            "Latest indicator snapshot with trend classification, trend_cnt, mt_cnt, rsi, rsi_updn, "
            "vol_tag, price-change fields, moving averages, and weekly high/low context."
        ),
        notes=[
            "trend is the Stock Trends moving-average classification, not a raw price direction.",
            "trend_cnt measures persistence of the current trend classification.",
            "mt_cnt measures broader trend-category maturity.",
            "rsi baseline is 100; values above 100 indicate outperformance versus benchmark.",
        ],
        analytical_role=ROLE_SYMBOL_SIGNAL_INTELLIGENCE,
        related_endpoints=["/v1/indicators/history", "/v1/stim/latest", "/v1/selections/history"],
        next_recommended_calls=["/v1/indicators/history", "/v1/stim/latest"],
        tags=["indicators", "symbol", "signals"],
    ),
    "/v1/indicators/history": _metadata(
        path="/v1/indicators/history",
        method="GET",
        tool_name="indicators_history",
        title="Indicators History",
        category="indicators",
        pricing_rule_id="indicators_history_paid",
        resource_description=(
            "Historical weekly Stock Trends indicator series for a symbol, including trend state, "
            "trend persistence, trend maturity, relative performance, volume tags, and signal metadata."
        ),
        bazaar_output_description=(
            "Returns a weekly indicator history for a requested symbol with rows containing "
            "weekdate, symbol_exchange, trend, trend_cnt, mt_cnt, rsi, rsi_updn, vol_tag, and related signal fields."
        ),
        purpose="Retrieve a bounded weekly history of Stock Trends indicator rows for one instrument.",
        investment_agent_value=(
            "Helps an agent evaluate signal persistence, maturity changes, relative performance behavior, "
            "and volume context over time before paying for deeper ST-IM or portfolio workflow steps."
        ),
        workflow_role="Historical signal context and trend persistence review.",
        input_rule="Provide symbol_exchange, or provide both symbol and exchange.",
        required_inputs=copy.deepcopy(_REQ_SYMBOL),
        optional_inputs={**copy.deepcopy(_OPT_SYMBOL), **_date_inputs(260, 2600)},
        safe_example_request={
            "method": "GET",
            "path": "/v1/indicators/history",
            "query": {"symbol_exchange": "IBM-N", "limit": 52, "cs_only": True},
        },
        response_shape=[
            "request_id", "symbol_exchange", "cs_only", "start", "end", "count",
            "data[].weekdate", "data[].exchange", "data[].symbol", "data[].symbol_exchange",
            "data[].trend", "data[].trend_cnt", "data[].mt_cnt", "data[].rsi",
            "data[].rsi_updn", "data[].vol_tag", "data[].pr_change",
        ],
        example_object={
            "request_id": "req_demo",
            "symbol_exchange": "SAMPLE-N",
            "count": 1,
            "data": [
                {
                    "weekdate": "YYYY-MM-DD",
                    "symbol_exchange": "SAMPLE-N",
                    "trend": "^-",
                    "trend_cnt": 4,
                    "mt_cnt": 11,
                    "rsi": 104,
                    "rsi_updn": "-",
                    "vol_tag": "",
                }
            ],
        },
        output_summary=(
            "Weekly indicator history with trend classification, trend_cnt, mt_cnt, rsi, rsi_updn, "
            "vol_tag, price-change fields, moving averages, and high/low context."
        ),
        notes=[
            "Use a bounded limit for agent workflows; 52 rows is a safe one-year context window.",
            "Rows are returned in ascending order for symbol-focused history.",
        ],
        analytical_role=ROLE_SYMBOL_SIGNAL_INTELLIGENCE,
        related_endpoints=["/v1/indicators/latest", "/v1/stim/history", "/v1/prices/history"],
        next_recommended_calls=["/v1/stim/history", "/v1/decision/evaluate-symbol"],
        tags=["indicators", "history", "signals"],
    ),
    "/v1/stim/latest": _metadata(
        path="/v1/stim/latest",
        method="GET",
        tool_name="stim_latest",
        title="STIM Latest",
        category="stim",
        pricing_rule_id="stim_latest_paid",
        resource_description=(
            "Latest Stock Trends Inference Model (ST-IM) outputs for a symbol: forward return "
            "expectations and statistical distributions across 4-week, 13-week, and 40-week horizons."
        ),
        bazaar_output_description=(
            "Returns latest ST-IM distribution fields for a symbol, including x4wk, x13wk, x40wk "
            "expected returns, lower and upper bounds, standard deviations, and freshness metadata."
        ),
        purpose="Retrieve latest ST-IM forward return distribution outputs for one instrument.",
        investment_agent_value="Provides probabilistic forward return context for ranking and decision workflows.",
        workflow_role="Forward distribution enrichment.",
        input_rule="Provide symbol_exchange, or provide both symbol and exchange.",
        required_inputs=copy.deepcopy(_REQ_SYMBOL),
        optional_inputs={k: copy.deepcopy(v) for k, v in _OPT_SYMBOL.items() if k != "cs_only"},
        safe_example_request={"method": "GET", "path": "/v1/stim/latest", "query": {"symbol_exchange": "IBM-N"}},
        response_shape=[
            "request_id", "symbol_exchange", "weekdate", "exchange", "symbol",
            "x4wk1", "x4wk2", "x4wk", "x4wksd", "x13wk1", "x13wk2", "x13wk", "x13wksd",
            "x40wk1", "x40wk2", "x40wk", "x40wksd", "latest_data_weekdate", "is_stale",
            "missing_reason", "missing_weekdate",
        ],
        example_object={"request_id": "req_demo", "symbol_exchange": "SAMPLE-N", "weekdate": "YYYY-MM-DD", "x13wk": 0.0, "x13wksd": 1.0},
        output_summary="ST-IM forward return expectations and statistical distributions across 4, 13, and 40 weeks.",
        notes=[
            "ST-IM is Stock Trends Inference Model; it is a probabilistic forward-looking model.",
            "xNwk1 is the lower bound, xNwk is expected return, xNwk2 is the upper bound, and xNwksd is standard deviation.",
            "Call /v1/meta/stim before interpreting ST-IM means or probabilities.",
            "Compare ST-IM means to base_period_mean_returns_pct; positive raw means alone are not sufficient.",
            "If is_stale=true or missing_reason is present, disclose that the ST-IM result is a historical fallback.",
        ],
        analytical_role=ROLE_PROBABILISTIC_FORWARD_INFERENCE,
        related_endpoints=["/v1/meta/stim", "/v1/indicators/latest", "/v1/stim/history", "/v1/selections/published/latest"],
        next_recommended_calls=["/v1/meta/stim", "/v1/decision/evaluate-symbol", "/v1/portfolio/construct"],
        interpretation_dependency=STIM_INTERPRETATION_DEPENDENCY,
        interpretation_guidance=STIM_INTERPRETATION_GUIDANCE,
        required_interpretation_steps=STIM_REQUIRED_INTERPRETATION_STEPS,
    ),
    "/v1/stim/history": _metadata(
        path="/v1/stim/history",
        method="GET",
        tool_name="stim_history",
        title="STIM History",
        category="stim",
        pricing_rule_id="stim_history_paid",
        resource_description="Historical ST-IM forward return distribution series for a symbol.",
        bazaar_output_description=(
            "Returns historical ST-IM records with expected returns, bounds, and standard deviations "
            "for 4-week, 13-week, and 40-week horizons."
        ),
        purpose="Retrieve a symbol-level history of ST-IM distribution records.",
        investment_agent_value="Lets agents compare current distribution outputs with prior weekly estimates.",
        workflow_role="Historical forward-distribution context.",
        input_rule="Provide symbol_exchange, or provide both symbol and exchange.",
        required_inputs=copy.deepcopy(_REQ_SYMBOL),
        optional_inputs={**{k: copy.deepcopy(v) for k, v in _OPT_SYMBOL.items() if k != "cs_only"}, **_date_inputs(260, 2600), "include_gaps": {"type": "boolean", "required": False, "safe_default": False}},
        safe_example_request={"method": "GET", "path": "/v1/stim/history", "query": {"symbol_exchange": "IBM-N", "limit": 52}},
        response_shape=["request_id", "symbol_exchange", "start", "end", "count", "data", "include_gaps", "gaps"],
        example_object={"request_id": "req_demo", "symbol_exchange": "SAMPLE-N", "count": 1, "data": [{"weekdate": "YYYY-MM-DD", "x13wk": 0.0, "x13wksd": 1.0}]},
        output_summary="Historical ST-IM distributions across 4, 13, and 40 week horizons.",
        notes=[
            "Use include_gaps=true only when the agent needs to diagnose missing ST-IM weeks.",
            "Call /v1/meta/stim before interpreting ST-IM means or probabilities.",
            "Compare ST-IM means to base_period_mean_returns_pct; positive raw means alone are not sufficient.",
            "If is_stale=true or missing_reason is present, disclose that the ST-IM result is a historical fallback.",
        ],
        analytical_role=ROLE_PROBABILISTIC_FORWARD_INFERENCE,
        related_endpoints=["/v1/meta/stim", "/v1/stim/latest", "/v1/indicators/history"],
        next_recommended_calls=["/v1/meta/stim", "/v1/indicators/history", "/v1/decision/evaluate-symbol"],
        interpretation_dependency=STIM_INTERPRETATION_DEPENDENCY,
        interpretation_guidance=STIM_INTERPRETATION_GUIDANCE,
        required_interpretation_steps=STIM_REQUIRED_INTERPRETATION_STEPS,
    ),
    "/v1/prices/latest": _metadata(
        path="/v1/prices/latest",
        method="GET",
        tool_name="prices_latest",
        title="Prices Latest",
        category="prices",
        pricing_rule_id="prices_latest_paid",
        resource_description="Latest weekly price row for a Stock Trends symbol, including adjusted close, weekly high/low, volume, trades, and price change.",
        bazaar_output_description="Returns latest weekly price fields for a requested symbol, including symbol_exchange, weekdate, price, adj_close, pr_week_hi, pr_week_lo, volume, trades, and pr_change.",
        purpose="Retrieve latest weekly price context for one instrument.",
        investment_agent_value="Provides price and volume context to pair with Stock Trends indicator and ST-IM signal analysis.",
        workflow_role="Price context enrichment.",
        input_rule="Provide symbol_exchange, or provide both symbol and exchange.",
        required_inputs=copy.deepcopy(_REQ_SYMBOL),
        optional_inputs=copy.deepcopy(_OPT_SYMBOL),
        safe_example_request={"method": "GET", "path": "/v1/prices/latest", "query": {"symbol_exchange": "IBM-N", "cs_only": True}},
        response_shape=["request_id", "symbol_exchange", "weekdate", "exchange", "symbol", "type", "currency_code", "price", "adj_close", "pr_week_hi", "pr_week_lo", "volume", "trades", "split_fact", "pr_change"],
        example_object={"request_id": "req_demo", "symbol_exchange": "SAMPLE-N", "weekdate": "YYYY-MM-DD", "price": 0.0, "volume": 0, "pr_change": 0.0},
        output_summary="Latest weekly price, adjusted close, high/low, volume, trades, split factor, and price change.",
        analytical_role=ROLE_PRICE_CONTEXT,
        notes=["Stock Trends is not a raw price system; use prices as context for signal interpretation."],
        related_endpoints=["/v1/indicators/latest", "/v1/prices/history"],
        next_recommended_calls=["/v1/indicators/latest", "/v1/stim/latest"],
    ),
    "/v1/prices/history": _metadata(
        path="/v1/prices/history",
        method="GET",
        tool_name="prices_history",
        title="Prices History",
        category="prices",
        pricing_rule_id="prices_history_paid",
        resource_description="Historical weekly price series for a Stock Trends symbol.",
        bazaar_output_description="Returns bounded weekly price history with weekdate, symbol_exchange, price, adjusted close, weekly high/low, volume, trades, and price change fields.",
        purpose="Retrieve weekly price history for one instrument.",
        investment_agent_value="Gives agents a bounded historical price context to interpret Stock Trends indicator changes.",
        workflow_role="Historical price context.",
        input_rule="Provide symbol_exchange, or provide both symbol and exchange.",
        required_inputs=copy.deepcopy(_REQ_SYMBOL),
        optional_inputs={**copy.deepcopy(_OPT_SYMBOL), **_date_inputs(260, 2600)},
        safe_example_request={"method": "GET", "path": "/v1/prices/history", "query": {"symbol_exchange": "IBM-N", "limit": 52, "cs_only": True}},
        response_shape=["request_id", "symbol_exchange", "cs_only", "start", "end", "count", "data[].weekdate", "data[].symbol_exchange", "data[].price", "data[].adj_close", "data[].volume", "data[].pr_change"],
        example_object={"request_id": "req_demo", "symbol_exchange": "SAMPLE-N", "count": 1, "data": [{"weekdate": "YYYY-MM-DD", "price": 0.0, "volume": 0}]},
        output_summary="Weekly price history for one symbol.",
        analytical_role=ROLE_PRICE_CONTEXT,
        notes=["Use a bounded limit for autonomous workflows."],
        related_endpoints=["/v1/prices/latest", "/v1/indicators/history"],
        next_recommended_calls=["/v1/indicators/history"],
    ),
    "/v1/selections/latest": _metadata(
        path="/v1/selections/latest",
        method="GET",
        tool_name="selections_latest",
        title="Selections Latest",
        category="selections",
        pricing_rule_id="selections_latest_paid",
        resource_description="Latest base st_select list ranked by prob13wk descending, without the published STIM Select threshold filter.",
        bazaar_output_description="Returns the latest st_select records with weekdate, exchange, symbol, prob13wk, and symbol_exchange; optional joins can add Stock Trends signal and instrument metadata.",
        purpose="Retrieve latest base selection records ranked by prob13wk.",
        investment_agent_value="Lets agents inspect the broad selection universe before applying published STIM Select criteria or joining signal fields.",
        workflow_role="Selection universe discovery.",
        optional_inputs={
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
            "min_prob13wk": {"type": "number", "required": False, "example": 0.55, "description": "Optional probability threshold."},
            "limit": {"type": "integer", "required": False, "safe_default": 2000, "minimum": 1, "maximum": 20000},
            "include_data": {"type": "boolean", "required": False, "safe_default": False},
            "include_mast": {"type": "boolean", "required": False, "safe_default": False},
            "cs_only": copy.deepcopy(CS_ONLY_INPUT),
        },
        safe_example_request={"method": "GET", "path": "/v1/selections/latest", "query": {"limit": 25, "include_data": False}},
        response_shape=["request_id", "weekdate", "exchange", "min_prob13wk", "include_data", "include_mast", "cs_only", "count", "data[].weekdate", "data[].exchange", "data[].symbol", "data[].prob13wk", "data[].symbol_exchange"],
        example_object={"request_id": "req_demo", "weekdate": "YYYY-MM-DD", "count": 1, "data": [{"symbol_exchange": "SAMPLE-N", "prob13wk": 0.0}]},
        output_summary="Latest base st_select list ranked by prob13wk descending.",
        analytical_role=ROLE_PROBABILISTIC_SELECTION_UNIVERSE,
        notes=[
            "No published threshold filter is applied unless min_prob13wk is set.",
            "Use /v1/selections/published/latest for the documented three-horizon published STIM Select definition.",
        ],
        related_endpoints=["/v1/selections/published/latest", "/v1/selections/history"],
        next_recommended_calls=["/v1/selections/published/latest", "/v1/indicators/latest"],
    ),
    "/v1/selections/history": _metadata(
        path="/v1/selections/history",
        method="GET",
        tool_name="selections_history",
        title="Selections History",
        category="selections",
        pricing_rule_id="selections_history_paid",
        resource_description="Historical base st_select records for a symbol, exchange, or date range.",
        bazaar_output_description="Returns historical st_select records with weekdate, exchange, symbol, prob13wk, symbol_exchange, and optional joined signal or instrument fields.",
        purpose="Retrieve base selection history for symbol or date-range review.",
        investment_agent_value="Helps agents inspect how selection membership and prob13wk changed over time.",
        workflow_role="Historical selection context.",
        optional_inputs={
            "symbol_exchange": copy.deepcopy(SYMBOL_EXCHANGE_INPUT),
            "symbol": copy.deepcopy(SYMBOL_INPUT),
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
            "start": copy.deepcopy(START_INPUT),
            "end": copy.deepcopy(END_INPUT),
            "min_prob13wk": {"type": "number", "required": False},
            "limit": {"type": "integer", "required": False, "safe_default": 520, "minimum": 1, "maximum": 5200},
            "include_data": {"type": "boolean", "required": False, "safe_default": False},
            "include_mast": {"type": "boolean", "required": False, "safe_default": False},
            "cs_only": copy.deepcopy(CS_ONLY_INPUT),
        },
        safe_example_request={"method": "GET", "path": "/v1/selections/history", "query": {"symbol_exchange": "IBM-N", "limit": 52}},
        response_shape=["request_id", "symbol", "exchange", "symbol_exchange", "start", "end", "min_prob13wk", "include_data", "include_mast", "cs_only", "count", "data[].weekdate", "data[].prob13wk", "data[].symbol_exchange"],
        example_object={"request_id": "req_demo", "symbol_exchange": "SAMPLE-N", "count": 1, "data": [{"weekdate": "YYYY-MM-DD", "symbol_exchange": "SAMPLE-N", "prob13wk": 0.0}]},
        output_summary="Historical base st_select records with prob13wk.",
        analytical_role=ROLE_PROBABILISTIC_SELECTION_UNIVERSE,
        notes=["Use published selection endpoints for the documented three-horizon STIM Select definition."],
        related_endpoints=["/v1/selections/latest", "/v1/selections/published/history"],
        next_recommended_calls=["/v1/selections/published/history"],
    ),
    "/v1/selections/published/latest": _metadata(
        path="/v1/selections/published/latest",
        method="GET",
        tool_name="selections_published_latest",
        title="Published STIM Select Latest",
        category="selections",
        pricing_rule_id="selections_published_latest_paid",
        resource_description=(
            "Latest published STIM Select list using the documented three-horizon ST-IM lower-bound "
            "criteria and prob13wk publication threshold."
        ),
        bazaar_output_description=(
            "Returns published STIM Select records with prob13wk plus ST-IM distribution fields "
            "x4wk, x13wk, and x40wk series; optional joins can add Stock Trends indicator fields."
        ),
        purpose="Retrieve latest published STIM Select records.",
        investment_agent_value="Provides an agent-ready list of securities satisfying the documented STIM Select publication criteria.",
        workflow_role="Published selection discovery.",
        optional_inputs={
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
            "min_prob13wk": {"type": "number", "required": False, "safe_default": 0.55},
            "min_x4wk1": {"type": "number", "required": False, "safe_default": 0.0},
            "min_x13wk1": {"type": "number", "required": False, "safe_default": 2.19},
            "min_x40wk1": {"type": "number", "required": False, "safe_default": 6.45},
            "limit": {"type": "integer", "required": False, "safe_default": 2000, "minimum": 1, "maximum": 20000},
            "include_data": {"type": "boolean", "required": False, "safe_default": False},
            "include_mast": {"type": "boolean", "required": False, "safe_default": False},
            "cs_only": copy.deepcopy(CS_ONLY_INPUT),
        },
        safe_example_request={"method": "GET", "path": "/v1/selections/published/latest", "query": {"limit": 25}},
        response_shape=["request_id", "weekdate", "exchange", "min_prob13wk", "min_x4wk1", "min_x13wk1", "min_x40wk1", "count", "data[].symbol_exchange", "data[].prob13wk", "data[].x4wk1", "data[].x13wk1", "data[].x40wk1"],
        example_object={"request_id": "req_demo", "weekdate": "YYYY-MM-DD", "count": 1, "data": [{"symbol_exchange": "SAMPLE-N", "prob13wk": 0.67, "x4wk1": 1.2, "x13wk1": 3.45, "x40wk1": 8.12}]},
        output_summary="Latest published STIM Select records satisfying three-horizon ST-IM criteria.",
        analytical_role=ROLE_PROBABILISTIC_SELECTION_LIST,
        notes=[
            "Published STIM Select requires x4wk1 > 0%, x13wk1 > 2.19%, x40wk1 > 6.45%, and prob13wk >= 55% by default.",
            "prob13wk is the probability of exceeding the 13-week base-period mean return of 2.19%, assuming normal distribution.",
        ],
        related_endpoints=["/v1/selections/latest", "/v1/selections/published/history", "/v1/stim/latest"],
        next_recommended_calls=["/v1/indicators/latest", "/v1/stim/latest"],
        interpretation_guidance=STIM_SELECT_INTERPRETATION_GUIDANCE,
    ),
    "/v1/selections/published/history": _metadata(
        path="/v1/selections/published/history",
        method="GET",
        tool_name="selections_published_history",
        title="Published STIM Select History",
        category="selections",
        pricing_rule_id="selections_published_history_paid",
        resource_description="Historical published STIM Select records using the documented three-horizon ST-IM criteria.",
        bazaar_output_description="Returns historical published STIM Select records with prob13wk and ST-IM distribution fields for requested filters.",
        purpose="Retrieve historical published STIM Select records.",
        investment_agent_value="Helps agents study historical publication membership and ST-IM threshold behavior.",
        workflow_role="Historical published selection context.",
        optional_inputs={
            "symbol_exchange": copy.deepcopy(SYMBOL_EXCHANGE_INPUT),
            "symbol": copy.deepcopy(SYMBOL_INPUT),
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
            "start": copy.deepcopy(START_INPUT),
            "end": copy.deepcopy(END_INPUT),
            "min_prob13wk": {"type": "number", "required": False, "safe_default": 0.55},
            "min_x4wk1": {"type": "number", "required": False, "safe_default": 0.0},
            "min_x13wk1": {"type": "number", "required": False, "safe_default": 2.19},
            "min_x40wk1": {"type": "number", "required": False, "safe_default": 6.45},
            "limit": {"type": "integer", "required": False, "safe_default": 5200, "minimum": 1, "maximum": 50000},
            "include_data": {"type": "boolean", "required": False, "safe_default": False},
            "include_mast": {"type": "boolean", "required": False, "safe_default": False},
            "cs_only": copy.deepcopy(CS_ONLY_INPUT),
        },
        safe_example_request={"method": "GET", "path": "/v1/selections/published/history", "query": {"symbol_exchange": "IBM-N", "limit": 52}},
        response_shape=["request_id", "symbol", "exchange", "symbol_exchange", "start", "end", "min_prob13wk", "min_x4wk1", "min_x13wk1", "min_x40wk1", "count", "data[].symbol_exchange", "data[].prob13wk", "data[].x4wk1", "data[].x13wk1", "data[].x40wk1"],
        example_object={"request_id": "req_demo", "symbol_exchange": "SAMPLE-N", "count": 1, "data": [{"weekdate": "YYYY-MM-DD", "prob13wk": 0.67, "x13wk1": 3.45}]},
        output_summary="Historical published STIM Select records.",
        analytical_role=ROLE_PROBABILISTIC_SELECTION_LIST,
        notes=["Use date filters and limits to keep autonomous workflows bounded."],
        related_endpoints=["/v1/selections/published/latest", "/v1/selections/history"],
        next_recommended_calls=["/v1/stim/history", "/v1/indicators/history"],
        interpretation_guidance=STIM_SELECT_INTERPRETATION_GUIDANCE,
    ),
    "/v1/market/regime/latest": _metadata(
        path="/v1/market/regime/latest",
        method="GET",
        tool_name="market_regime_latest",
        title="Market Regime Latest",
        category="market",
        pricing_rule_id="market_regime_latest",
        resource_description="Current market regime classification derived from the distribution of Stock Trends trend codes across active signals.",
        bazaar_output_description="Returns regime, confidence, regime_score, bullish_pct, bearish_pct, avg_rsi, avg_mt_cnt, signal_count, and weekdate.",
        purpose="Classify current market regime.",
        investment_agent_value="Gives agents market context before symbol-level or portfolio-level decisions.",
        workflow_role="Market context.",
        safe_example_request={"method": "GET", "path": "/v1/market/regime/latest", "query": {}},
        response_shape=["regime", "confidence", "regime_score", "bullish_pct", "bearish_pct", "avg_rsi", "avg_mt_cnt", "weekdate", "signal_count"],
        example_object={"regime": "mixed", "confidence": 0.0, "regime_score": 0.0, "weekdate": "YYYY-MM-DD"},
        output_summary="Current regime classification and aggregate signal distribution.",
        analytical_role=ROLE_MARKET_REGIME_CLASSIFIER,
        notes=["Bullish codes: ^+, ^-, v^. Bearish codes: v-, v+, ^v."],
        related_endpoints=["/v1/market/regime/history", "/v1/market/regime/forecast"],
        next_recommended_calls=["/v1/market/regime/forecast", "/v1/decision/evaluate-symbol"],
        interpretation_guidance=REGIME_INTERPRETATION_GUIDANCE,
    ),
    "/v1/market/regime/history": _metadata(
        path="/v1/market/regime/history",
        method="GET",
        tool_name="market_regime_history",
        title="Market Regime History",
        category="market",
        pricing_rule_id="market_regime_history",
        resource_description="Historical sequence of weekly market regime classifications.",
        bazaar_output_description="Returns weekly regime history with regime, confidence, regime_score, bullish_pct, bearish_pct, avg_rsi, avg_mt_cnt, and signal_count.",
        purpose="Review recent market regime transitions.",
        investment_agent_value="Helps agents understand whether current regime context is stable or changing.",
        workflow_role="Historical market context.",
        optional_inputs={"limit": {"type": "integer", "required": False, "safe_default": 12, "minimum": 1, "maximum": 52}, "start": copy.deepcopy(START_INPUT)},
        safe_example_request={"method": "GET", "path": "/v1/market/regime/history", "query": {"limit": 12}},
        response_shape=["history[].weekdate", "history[].regime", "history[].confidence", "history[].regime_score", "history[].bullish_pct", "history[].bearish_pct", "history[].avg_rsi", "history[].avg_mt_cnt", "history[].signal_count", "count", "limit", "start_date"],
        example_object={"count": 1, "history": [{"weekdate": "YYYY-MM-DD", "regime": "mixed", "regime_score": 0.0}]},
        output_summary="Recent weekly market regime sequence.",
        analytical_role=ROLE_MARKET_REGIME_CLASSIFIER,
        notes=["Each row uses the same classification logic as /v1/market/regime/latest."],
        related_endpoints=["/v1/market/regime/latest", "/v1/market/regime/forecast"],
        next_recommended_calls=["/v1/market/regime/forecast"],
        interpretation_guidance=REGIME_INTERPRETATION_GUIDANCE,
    ),
    "/v1/market/regime/forecast": _metadata(
        path="/v1/market/regime/forecast",
        method="GET",
        tool_name="market_regime_forecast",
        title="Market Regime Forecast",
        category="market",
        pricing_rule_id="market_regime_forecast",
        resource_description="Deterministic forward regime outlook derived from recent weekly regime score direction and consistency.",
        bazaar_output_description="Returns forecast_regime, forecast_confidence, current_regime, current_regime_score, recent_direction, regime_consistency, projected_regime_score, and weeks_analyzed.",
        purpose="Estimate near-term regime direction using deterministic recent-score logic.",
        investment_agent_value="Helps agents decide whether to run bullish, bearish, or mixed workflows.",
        workflow_role="Regime decision guidance.",
        optional_inputs={"lookback": {"type": "integer", "required": False, "safe_default": 5, "minimum": 2, "maximum": 13}},
        safe_example_request={"method": "GET", "path": "/v1/market/regime/forecast", "query": {"lookback": 5}},
        response_shape=["forecast_regime", "forecast_confidence", "current_regime", "current_regime_score", "recent_direction", "regime_consistency", "projected_regime_score", "avg_weekly_score_delta", "recent_scores", "weeks_analyzed", "lookback", "weekdate"],
        example_object={"forecast_regime": "mixed", "forecast_confidence": 0.0, "recent_direction": "stable"},
        output_summary="Deterministic regime outlook and confidence fields.",
        analytical_role=ROLE_MARKET_REGIME_CLASSIFIER,
        notes=["No ML is used; output is deterministic from recent regime scores."],
        related_endpoints=["/v1/market/regime/latest", "/v1/market/regime/history"],
        next_recommended_calls=["/v1/decision/evaluate-symbol", "/v1/portfolio/construct"],
    ),
    "/v1/decision/evaluate-symbol": _metadata(
        path="/v1/decision/evaluate-symbol",
        method="POST",
        tool_name="evaluate_symbol",
        title="Symbol Decision Evaluation",
        category="decision",
        pricing_rule_id="evaluate_symbol",
        resource_description="Deterministic symbol-level decision evaluation combining Stock Trends signal context with market regime context.",
        bazaar_output_description="Returns bias, confidence, decision_score, alignment, symbol_context, regime_context, and signal_notes for a requested symbol.",
        purpose="Evaluate one symbol for directional decision-support bias in regime context.",
        investment_agent_value="Combines signal and regime context into a compact decision object for agent workflows.",
        workflow_role="Symbol-level decision step.",
        input_rule="POST body may provide symbol_exchange, or symbol plus exchange.",
        required_inputs=copy.deepcopy(_REQ_SYMBOL),
        optional_inputs={k: copy.deepcopy(v) for k, v in _OPT_SYMBOL.items() if k != "cs_only"},
        safe_example_request={"method": "POST", "path": "/v1/decision/evaluate-symbol", "json": {"symbol_exchange": "IBM-N"}},
        response_shape=["request_id", "symbol", "exchange", "weekdate", "bias", "confidence", "decision_score", "alignment", "symbol_context.trend", "symbol_context.trend_cnt", "symbol_context.mt_cnt", "symbol_context.rsi", "symbol_context.rsi_updn", "symbol_context.vol_tag", "symbol_context.symbol_bias", "regime_context.current_regime", "regime_context.regime_score", "signal_notes"],
        example_object={"request_id": "req_demo", "symbol_exchange": "SAMPLE-N", "bias": "hold", "confidence": 0.0, "decision_score": 0.0},
        output_summary="Deterministic symbol decision with Stock Trends signal and regime context.",
        analytical_role=ROLE_SYMBOL_DECISION_ENGINE,
        notes=["Fully deterministic; no ML."],
        related_endpoints=["/v1/market/regime/latest", "/v1/indicators/latest", "/v1/stim/latest"],
        next_recommended_calls=["/v1/portfolio/evaluate", "/v1/portfolio/construct"],
    ),
    "/v1/portfolio/construct": _metadata(
        path="/v1/portfolio/construct",
        method="POST",
        tool_name="portfolio_construct",
        title="Portfolio Construct",
        category="portfolio",
        pricing_rule_id="portfolio_construct",
        resource_description="Constructs a deterministic equal-weight portfolio from eligible Stock Trends signal candidates.",
        bazaar_output_description="Returns a constructed equal-weight portfolio with symbol weights, signal fields, decision scores, ST-IM tiebreaker fields, and regime context.",
        purpose="Build an equal-weight candidate portfolio from Stock Trends signals.",
        investment_agent_value="Turns ranked signals into a bounded portfolio proposal with deterministic scoring context.",
        workflow_role="Portfolio construction.",
        optional_inputs={
            "universe": {
                "type": "string",
                "required": False,
                "enum": ["top"],
                "safe_default": "top",
                "example": "top",
                "description": "Candidate universe selector. Currently supports top-ranked signal candidates.",
            },
            "count": {
                "type": "integer",
                "required": False,
                "minimum": 1,
                "maximum": 10,
                "safe_default": 5,
                "example": 5,
                "description": "Number of equal-weight positions to construct.",
            },
            "bias": {
                "type": "string",
                "required": False,
                "enum": ["auto", "bullish", "bearish"],
                "safe_default": "auto",
                "example": "auto",
                "description": "Portfolio orientation selector. auto derives context from market regime signals.",
            },
            "exchange": {
                "type": "string",
                "required": False,
                "enum": US_EXCHANGE_ENUM + ["T"],
                "example": "N",
                "description": "Optional exchange filter for candidate selection.",
            },
        },
        safe_example_request={"method": "POST", "path": "/v1/portfolio/construct", "json": {"universe": "top", "count": 5, "bias": "auto"}},
        response_shape=[
            "request_id", "weekdate",
            "portfolio[].rank", "portfolio[].weight", "portfolio[].symbol",
            "portfolio[].exchange", "portfolio[].symbol_exchange",
            "portfolio[].trend", "portfolio[].trend_cnt", "portfolio[].mt_cnt",
            "portfolio[].rsi", "portfolio[].bias", "portfolio[].confidence",
            "portfolio[].decision_score",
            "portfolio[].stim_expected_return_13wk",
            "portfolio[].stim_volatility_13wk",
            "portfolio[].stim_risk_adjusted_13wk",
            "portfolio[].stim_percentile_13wk",
            "portfolio[].stim_covered",
            "count", "universe", "exchange_filter",
            "candidates_evaluated", "candidate_selection_method", "candidate_ordering",
            "portfolio_score",
            "bias_requested", "bias_resolved",
            "stim_weekdate", "stim_covered_count", "stim_coverage_pct", "ranking_method",
            "regime_context.current_regime", "regime_context.regime_score",
            "regime_context.regime_confidence", "regime_context.forecast_regime",
            "regime_context.forecast_confidence", "regime_context.recent_direction",
            "regime_context.regime_consistency", "regime_context.weeks_analyzed",
            "construction_notes",
        ],
        example_object={"request_id": "req_demo", "count": 1, "portfolio": [{"rank": 1, "symbol_exchange": "SAMPLE-N", "weight": 1.0, "decision_score": 0.0}]},
        output_summary="Constructed equal-weight portfolio with signal, decision, ST-IM tiebreaker, and regime fields.",
        analytical_role=ROLE_PORTFOLIO_CONSTRUCTION_ENGINE,
        notes=["Primary ranking is decision_score descending; ST-IM 13-week risk-adjusted return is a tiebreaker when available."],
        related_endpoints=["/v1/agent/screener/top", "/v1/portfolio/evaluate", "/v1/portfolio/compare"],
        next_recommended_calls=["/v1/portfolio/evaluate", "/v1/portfolio/compare"],
    ),
    "/v1/portfolio/evaluate": _metadata(
        path="/v1/portfolio/evaluate",
        method="POST",
        tool_name="portfolio_evaluate",
        title="Portfolio Evaluate",
        category="portfolio",
        pricing_rule_id="portfolio_evaluate",
        resource_description="Evaluates a user-supplied portfolio against Stock Trends signal and market regime context.",
        bazaar_output_description="Returns per-position signal and decision fields plus portfolio_score, portfolio_bias, portfolio_confidence, portfolio_alignment, and regime context.",
        purpose="Evaluate an existing or proposed portfolio.",
        investment_agent_value="Lets agents score portfolio alignment to current Stock Trends signal and regime context.",
        workflow_role="Portfolio review.",
        required_inputs={
            "positions": {
                "type": "array",
                "required": True,
                "description": "List of symbol/weight positions.",
                "example": [{"symbol_exchange": "IBM-N", "weight": 1.0}],
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol_exchange": copy.deepcopy(SYMBOL_EXCHANGE_PROPERTY_SCHEMA),
                        "weight": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "example": 1.0,
                            "description": "Portfolio weight for this position.",
                        },
                    },
                    "required": ["symbol_exchange", "weight"],
                    "additionalProperties": False,
                },
            }
        },
        safe_example_request={"method": "POST", "path": "/v1/portfolio/evaluate", "json": {"positions": [{"symbol_exchange": "IBM-N", "weight": 1.0}]}},
        response_shape=["request_id", "weekdate", "positions[].symbol_exchange", "positions[].weight", "positions[].trend", "positions[].decision_score", "positions_found", "positions_missing", "effective_weight", "portfolio_score", "portfolio_bias", "portfolio_confidence", "portfolio_alignment", "regime_context.current_regime", "evaluation_notes"],
        example_object={"request_id": "req_demo", "positions_found": 1, "portfolio_score": 0.0, "positions": [{"symbol_exchange": "SAMPLE-N", "found": True}]},
        output_summary="Portfolio-level and position-level Stock Trends evaluation.",
        analytical_role=ROLE_PORTFOLIO_EVALUATION_ENGINE,
        notes=["Missing symbols are included with found=false and excluded from aggregates."],
        related_endpoints=["/v1/portfolio/construct", "/v1/portfolio/compare"],
        next_recommended_calls=["/v1/portfolio/compare"],
    ),
    "/v1/portfolio/compare": _metadata(
        path="/v1/portfolio/compare",
        method="POST",
        tool_name="portfolio_compare",
        title="Portfolio Compare",
        category="portfolio",
        pricing_rule_id="portfolio_compare",
        resource_description="Compares two user-supplied portfolios using Stock Trends decision scoring, alignment, and regime context.",
        bazaar_output_description="Returns left and right portfolio evaluations plus comparison.winner, score_delta, alignment_advantage, overlap_count, and comparison notes.",
        purpose="Compare current and proposed portfolios.",
        investment_agent_value="Helps agents quantify whether a proposed portfolio improves Stock Trends score and regime alignment.",
        workflow_role="Portfolio comparison.",
        required_inputs={
            "left": {
                "type": "array",
                "required": True,
                "description": "Left portfolio as a direct array of symbol-weight positions.",
                "example": [{"symbol_exchange": "IBM-N", "weight": 1.0}],
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol_exchange": copy.deepcopy(SYMBOL_EXCHANGE_PROPERTY_SCHEMA),
                        "weight": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "example": 1.0,
                            "description": "Portfolio weight for this position.",
                        },
                    },
                    "required": ["symbol_exchange", "weight"],
                    "additionalProperties": False,
                },
            },
            "right": {
                "type": "array",
                "required": True,
                "description": "Right portfolio as a direct array of symbol-weight positions.",
                "example": [{"symbol_exchange": "MSFT-Q", "weight": 1.0}],
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol_exchange": copy.deepcopy(SYMBOL_EXCHANGE_PROPERTY_SCHEMA),
                        "weight": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "example": 1.0,
                            "description": "Portfolio weight for this position.",
                        },
                    },
                    "required": ["symbol_exchange", "weight"],
                    "additionalProperties": False,
                },
            },
        },
        safe_example_request={"method": "POST", "path": "/v1/portfolio/compare", "json": {"left": [{"symbol_exchange": "IBM-N", "weight": 1.0}], "right": [{"symbol_exchange": "MSFT-Q", "weight": 1.0}]}},
        response_shape=["request_id", "weekdate", "left.positions[].symbol_exchange", "left.portfolio_score", "right.positions[].symbol_exchange", "right.portfolio_score", "comparison.winner", "comparison.score_delta", "comparison.alignment_advantage", "comparison.overlap_count", "regime_context.current_regime", "comparison_notes"],
        example_object={"request_id": "req_demo", "comparison": {"winner": "right", "score_delta": 0.0}},
        output_summary="Side-by-side portfolio evaluations and comparison metrics.",
        analytical_role=ROLE_PORTFOLIO_EVALUATION_ENGINE,
        notes=["Use after constructing a proposed alternative or reviewing a user-supplied allocation."],
        related_endpoints=["/v1/portfolio/evaluate", "/v1/portfolio/construct"],
        next_recommended_calls=["/v1/portfolio/evaluate"],
    ),
    "/v1/stwr/reports/latest": _metadata(
        path="/v1/stwr/reports/latest",
        method="GET",
        tool_name="stwr_reports_latest",
        title="STWR Reports Latest",
        category="stwr",
        pricing_rule_id="stwr_reports_latest_paid",
        resource_description="Latest Stock Trends Weekly Reporter screening report for a requested report code and exchange.",
        bazaar_output_description="Returns a requested STWR report with report code, name, exchange, weekdate, count, and data rows containing symbol_exchange and relevant Stock Trends fields.",
        purpose="Retrieve the latest named STWR screening report.",
        investment_agent_value="Provides curated Stock Trends screening lists for agent research workflows.",
        workflow_role="Curated report discovery.",
        required_inputs={
            "rpt": {
                "type": "string",
                "required": True,
                "example": "bullcross",
                "safe_default_for_demo": "bullcross",
                "description": "Report code from /v1/stwr/reports/catalog.",
            },
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
        },
        optional_inputs={
            "weekdate": copy.deepcopy(END_INPUT),
            "include_mast": {"type": "boolean", "required": False, "safe_default": False},
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 50000},
        },
        safe_example_request={"method": "GET", "path": "/v1/stwr/reports/latest", "query": {"rpt": "bullcross", "exchange": "N", "limit": 25}},
        response_shape=["request_id", "rpt", "name", "exchange", "weekdate", "count", "data[].symbol_exchange", "data[].trend", "data[].trend_cnt", "data[].mt_cnt", "data[].rsi", "data[].vol_tag", "note"],
        example_object={"request_id": "req_demo", "rpt": "bullcross", "exchange": "N", "count": 1, "data": [{"symbol_exchange": "SAMPLE-N", "trend": "v^"}]},
        output_summary="Latest named STWR report rows.",
        analytical_role=ROLE_CURATED_SIGNAL_REPORT,
        notes=["Use /v1/stwr/reports/catalog to discover valid report codes before paying for a report."],
        related_endpoints=["/v1/stwr/reports/catalog", "/v1/stwr/reports/history"],
        next_recommended_calls=["/v1/indicators/latest", "/v1/stim/latest"],
    ),
    "/v1/stwr/reports/history": _metadata(
        path="/v1/stwr/reports/history",
        method="GET",
        tool_name="stwr_reports_history",
        title="STWR Reports History",
        category="stwr",
        pricing_rule_id="stwr_reports_history_paid",
        resource_description="Historical Stock Trends Weekly Reporter screening report rows for a requested report code and exchange.",
        bazaar_output_description="Returns historical STWR report rows grouped by weekdate by default, including report code, exchange, week_count, count, and data rows.",
        purpose="Retrieve historical rows for a named STWR report.",
        investment_agent_value="Lets agents study persistence and recurrence of curated report membership over time.",
        workflow_role="Curated report history.",
        required_inputs={
            "rpt": {
                "type": "string",
                "required": True,
                "example": "bullcross",
                "safe_default_for_demo": "bullcross",
                "description": "Report code from /v1/stwr/reports/catalog.",
            },
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
        },
        optional_inputs={
            "start": copy.deepcopy(START_INPUT),
            "end": copy.deepcopy(END_INPUT),
            "group_by_week": {"type": "boolean", "required": False, "safe_default": True},
            "include_mast": {"type": "boolean", "required": False, "safe_default": False},
            "limit": {"type": "integer", "required": False, "safe_default": 200000, "minimum": 1, "maximum": 500000},
        },
        safe_example_request={"method": "GET", "path": "/v1/stwr/reports/history", "query": {"rpt": "bullcross", "exchange": "N", "limit": 500}},
        response_shape=["request_id", "rpt", "name", "exchange", "start", "end", "week_count", "count", "weeks[].weekdate", "weeks[].count", "weeks[].data[].symbol_exchange", "note"],
        example_object={"request_id": "req_demo", "rpt": "bullcross", "exchange": "N", "week_count": 1, "weeks": [{"weekdate": "YYYY-MM-DD", "count": 1, "data": [{"symbol_exchange": "SAMPLE-N"}]}]},
        output_summary="Historical STWR report rows, grouped by week by default.",
        analytical_role=ROLE_CURATED_SIGNAL_REPORT,
        notes=["Use bounded date ranges or limits for autonomous workflows."],
        related_endpoints=["/v1/stwr/reports/latest", "/v1/indicators/history"],
        next_recommended_calls=["/v1/indicators/history"],
    ),
    "/v1/breadth/sector/latest": _metadata(
        path="/v1/breadth/sector/latest",
        method="GET",
        tool_name="breadth_sector_latest",
        title="Sector Breadth Latest",
        category="breadth",
        pricing_rule_id="breadth_sector_latest_paid",
        resource_description="Latest sector, industry-group, or industry breadth snapshot derived from Stock Trends signal distribution.",
        bazaar_output_description="Returns the latest breadth groups with bullish/bearish counts and percentages, average RSI, average mt_cnt, and net breadth.",
        purpose="Retrieve the latest breadth context across sector or industry groupings.",
        investment_agent_value="Helps agents identify current market participation, sector strength, and breadth concentration before symbol-level calls.",
        workflow_role="Current market breadth context.",
        optional_inputs={
            "group_level": {"type": "string", "required": False, "enum": ["sector", "industry_group", "industry"], "safe_default": "sector"},
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
            "weekdate": {"type": "string", "required": False, "format": "date", "description": "Override weekdate in YYYY-MM-DD format; defaults to the latest available week."},
            "cs_only": copy.deepcopy(CS_ONLY_INPUT),
            "include_unknown": {"type": "boolean", "required": False, "safe_default": False},
            "min_price": {"type": "number", "required": False, "minimum": 0},
            "min_volume": {"type": "integer", "required": False, "minimum": 0},
            "vol_scale": {"type": "integer", "required": False, "safe_default": 100, "minimum": 1},
            "limit": {"type": "integer", "required": False, "safe_default": 5000, "minimum": 1, "maximum": 50000},
        },
        safe_example_request={"method": "GET", "path": "/v1/breadth/sector/latest", "query": {"group_level": "sector", "limit": 5000}},
        response_shape=["request_id", "group_level", "exchange", "weekdate", "cs_only", "include_unknown", "count", "data[].sector_code", "data[].sector_name", "data[].industry_group_code", "data[].industry_group_name", "data[].industry_code", "data[].industry_name", "data[].bullish_count", "data[].bearish_count", "data[].bullish_pct", "data[].bearish_pct", "data[].avg_rsi", "data[].avg_mt_cnt", "data[].net_breadth"],
        example_object={"request_id": "req_demo", "group_level": "sector", "weekdate": "YYYY-MM-DD", "count": 1, "data": [{"sector_code": "SAMPLE", "sector_name": "Sample Sector", "bullish_count": 0, "bearish_count": 0, "bullish_pct": 0.0, "bearish_pct": 0.0, "avg_rsi": 100, "net_breadth": 0}]},
        output_summary="Latest breadth groups and current signal distribution metrics.",
        analytical_role=ROLE_MARKET_BREADTH_CONTEXT,
        notes=["Use /v1/breadth/sector/history when trend analysis over multiple weeks is needed."],
        related_endpoints=["/v1/breadth/sector/history", "/v1/market/regime/latest"],
        next_recommended_calls=["/v1/market/regime/latest", "/v1/leadership/summary/latest"],
    ),
    "/v1/breadth/sector/history": _metadata(
        path="/v1/breadth/sector/history",
        method="GET",
        tool_name="breadth_sector_history",
        title="Sector Breadth History",
        category="breadth",
        pricing_rule_id="breadth_sector_history_paid",
        resource_description="Historical sector, industry-group, or industry breadth series derived from Stock Trends signal distribution.",
        bazaar_output_description="Returns historical breadth groups with bullish/bearish counts and percentages, average RSI, average mt_cnt, and optional weekly grouping.",
        purpose="Retrieve historical breadth context across sector or industry groupings.",
        investment_agent_value="Helps agents identify whether leadership and breadth are broadening or narrowing over time.",
        workflow_role="Market breadth context.",
        optional_inputs={
            "group_level": {"type": "string", "required": False, "enum": ["sector", "industry_group", "industry"], "safe_default": "sector"},
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
            "start": copy.deepcopy(START_INPUT),
            "end": copy.deepcopy(END_INPUT),
            "group_by_week": {"type": "boolean", "required": False, "safe_default": True},
            "cs_only": copy.deepcopy(CS_ONLY_INPUT),
            "include_unknown": {"type": "boolean", "required": False, "safe_default": False},
            "limit": {"type": "integer", "required": False, "safe_default": 200000, "minimum": 1, "maximum": 500000},
        },
        safe_example_request={"method": "GET", "path": "/v1/breadth/sector/history", "query": {"group_level": "sector", "group_by_week": True, "limit": 5000}},
        response_shape=["request_id", "group_level", "exchange", "start", "end", "cs_only", "include_unknown", "week_count", "count", "weeks[].weekdate", "weeks[].data[].bullish_count", "weeks[].data[].bearish_count", "weeks[].data[].avg_rsi", "note"],
        example_object={"request_id": "req_demo", "group_level": "sector", "week_count": 1, "weeks": [{"weekdate": "YYYY-MM-DD", "count": 1, "data": [{"group_name": "Sample Sector", "bullish_count": 0, "bearish_count": 0, "avg_rsi": 100}]}]},
        output_summary="Historical breadth groups and weekly signal distribution metrics.",
        analytical_role=ROLE_MARKET_BREADTH_CONTEXT,
        notes=["Use /v1/breadth/sector/latest for the current breadth snapshot before requesting multi-week history."],
        related_endpoints=["/v1/breadth/sector/latest", "/v1/market/regime/history"],
        next_recommended_calls=["/v1/market/regime/latest", "/v1/leadership/summary/latest"],
    ),
    "/v1/leadership/definitions": _metadata(
        path="/v1/leadership/definitions",
        method="GET",
        tool_name="leadership_definitions",
        title="Leadership Definitions",
        category="planning_helper",
        pricing_rule_id="leadership_definitions_public",
        supported_rails=[],
        access_type="free",
        requires_payment=False,
        resource_description=(
            "Public planning helper defining Stock Trends leadership screens, ranking fields, "
            "and taxonomy levels before paid leadership intelligence calls."
        ),
        bazaar_output_description=(
            "Returns leadership concept definitions, RSI/trend field meanings, taxonomy levels, "
            "and notes about ranking behavior."
        ),
        purpose="Understand leadership screen definitions and ranking fields before paid leadership calls.",
        investment_agent_value=(
            "Lets agents plan leadership workflows without paying or exposing live leadership data."
        ),
        workflow_role="Leadership planning metadata.",
        safe_example_request={"method": "GET", "path": "/v1/leadership/definitions", "query": {}},
        response_shape=[
            "concept",
            "indicators.rsi",
            "indicators.trend",
            "indicators.trend_cnt",
            "indicators.mt_cnt",
            "taxonomy_source",
            "taxonomy_levels[]",
            "notes.ranking",
        ],
        example_object={
            "concept": "Stock Trends leadership screens identify instruments with strong relative strength and trend alignment.",
            "taxonomy_levels": ["sector", "industry_group", "industry"],
            "notes": {"ranking": "summary/latest uses RSI desc (then mt_cnt desc)."},
        },
        output_summary="Leadership definitions, indicator meanings, taxonomy levels, and ranking notes.",
        notes=["Public helper; no API key or payment required."],
        related_endpoints=["/v1/leadership/summary/latest", "/v1/leadership/rotation/history"],
        next_recommended_calls=["/v1/leadership/summary/latest"],
        tags=["leadership", "planning_helper"],
    ),
    "/v1/leadership/summary/latest": _metadata(
        path="/v1/leadership/summary/latest",
        method="GET",
        tool_name="leadership_summary_latest",
        title="Leadership Summary Latest",
        category="leadership",
        pricing_rule_id="leadership_summary_latest_paid",
        supported_rails=SUPPORTED_RAILS,
        resource_description=(
            "Latest Stock Trends leadership summary identifying overall, sector, and "
            "industry-group leaders using relative performance and trend-alignment filters."
        ),
        bazaar_output_description=(
            "Returns leadership summary fields including weekdate, filters, overall_leaders, "
            "sector_leaders, and industry_group_leaders with symbol, exchange, rsi, mt_cnt, "
            "trend, trend_cnt, and taxonomy metadata."
        ),
        purpose="Retrieve the latest Stock Trends leadership summary across sectors and industry groups.",
        investment_agent_value=(
            "Helps agents identify where relative performance and bullish Stock Trends alignment "
            "are concentrated before combining leadership context with breadth, regime, or symbol workflows."
        ),
        workflow_role="Leadership context enrichment.",
        optional_inputs={
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
            "weekdate": copy.deepcopy(END_INPUT),
            "type": {
                "type": "string",
                "required": False,
                "safe_default": "CS",
                "example": "CS",
                "description": "Instrument type filter. CS is the safe default for common-stock leadership scans.",
            },
            "min_rsi": {
                "type": "integer",
                "required": False,
                "safe_default": 40,
                "minimum": 0,
                "maximum": 500,
                "example": 40,
                "description": "Minimum Stock Trends RSI threshold. RSI baseline is 100.",
            },
            "min_mt_cnt": {
                "type": "integer",
                "required": False,
                "safe_default": 4,
                "minimum": 0,
                "maximum": 500,
                "example": 4,
                "description": "Minimum trend-category maturity filter.",
            },
            "limit_overall": {
                "type": "integer",
                "required": False,
                "safe_default": 50,
                "minimum": 1,
                "maximum": 1000,
                "example": 50,
            },
            "limit_bucket": {
                "type": "integer",
                "required": False,
                "safe_default": 20,
                "minimum": 1,
                "maximum": 200,
                "example": 20,
            },
        },
        safe_example_request={
            "method": "GET",
            "path": "/v1/leadership/summary/latest",
            "query": {"exchange": "N", "type": "CS", "min_rsi": 40, "min_mt_cnt": 4},
        },
        response_shape=[
            "request_id", "weekdate", "exchange", "filters.type", "filters.min_rsi",
            "filters.min_mt_cnt", "overall_leaders[].symbol", "overall_leaders[].exchange",
            "overall_leaders[].rsi", "overall_leaders[].mt_cnt", "overall_leaders[].trend",
            "overall_leaders[].trend_cnt", "overall_leaders[].rsi_updn",
            "overall_leaders[].sector_name", "overall_leaders[].industry_group_name",
            "sector_leaders[].symbol", "sector_leaders[].sector_name",
            "industry_group_leaders[].symbol", "industry_group_leaders[].industry_group_name",
            "note",
        ],
        example_object={
            "request_id": "req_demo",
            "weekdate": "YYYY-MM-DD",
            "exchange": "N",
            "filters": {"type": "CS", "min_rsi": 40, "min_mt_cnt": 4},
            "overall_leaders": [
                {
                    "symbol": "SAMPLE",
                    "exchange": "N",
                    "rsi": 118,
                    "mt_cnt": 10,
                    "trend": "^+",
                    "trend_cnt": 6,
                    "sector_name": "Sample Sector",
                }
            ],
            "sector_leaders": [],
            "industry_group_leaders": [],
        },
        output_summary=(
            "Latest leadership groups with overall, sector, and industry-group leaders ranked by "
            "Stock Trends RSI and trend-category maturity filters."
        ),
        analytical_role=ROLE_LEADERSHIP_INTELLIGENCE,
        notes=[
            "RSI baseline is 100; values above 100 indicate outperformance versus benchmark.",
            "Use with /v1/breadth/sector/latest and /v1/market/regime/latest for context before symbol-level calls.",
        ],
        related_endpoints=["/v1/breadth/sector/latest", "/v1/market/regime/latest", "/v1/leadership/rotation/history"],
        next_recommended_calls=["/v1/market/regime/latest", "/v1/indicators/latest"],
        tags=["leadership", "breadth", "context"],
    ),
    "/v1/leadership/rotation/history": _metadata(
        path="/v1/leadership/rotation/history",
        method="GET",
        tool_name="leadership_rotation_history",
        title="Leadership Rotation History",
        category="leadership",
        pricing_rule_id="leadership_rotation_history_paid",
        supported_rails=SUPPORTED_RAILS,
        resource_description=(
            "Historical sector leadership rotation derived from Stock Trends trend distribution, "
            "relative performance, and trend maturity measures."
        ),
        bazaar_output_description=(
            "Returns weekly leadership rotation rows or grouped weeks with sector, bullish share, "
            "average RSI, average trend maturity, leadership_score, and rank_in_week."
        ),
        purpose="Retrieve historical sector leadership rotation over time.",
        investment_agent_value=(
            "Helps agents identify where leadership is rotating across sectors before combining "
            "that context with regime, breadth, and symbol-level workflows."
        ),
        workflow_role="Historical leadership rotation context.",
        optional_inputs={
            "exchange": copy.deepcopy(EXCHANGE_INPUT),
            "start": copy.deepcopy(START_INPUT),
            "end": copy.deepcopy(END_INPUT),
            "type": {
                "type": "string",
                "required": False,
                "safe_default": "CS",
                "example": "CS",
                "description": "Instrument type filter. CS is the safe default for common-stock leadership rotation.",
            },
            "top_k": {
                "type": "integer",
                "required": False,
                "safe_default": 5,
                "minimum": 1,
                "maximum": 50,
                "example": 5,
                "description": "Top sectors per week. Omit for all sectors.",
            },
            "min_constituents": {
                "type": "integer",
                "required": False,
                "safe_default": 25,
                "minimum": 1,
                "maximum": 5000,
                "example": 25,
            },
            "group_by_week": {
                "type": "boolean",
                "required": False,
                "safe_default": True,
                "example": True,
            },
        },
        safe_example_request={
            "method": "GET",
            "path": "/v1/leadership/rotation/history",
            "query": {"exchange": "N", "type": "CS", "top_k": 5, "group_by_week": True},
        },
        response_shape=[
            "request_id", "exchange", "start", "end", "filters.type",
            "filters.min_constituents", "filters.top_k", "week_count", "count",
            "weeks[].weekdate", "weeks[].count", "weeks[].data[].sector_code",
            "weeks[].data[].sector_name", "weeks[].data[].bull_pct",
            "weeks[].data[].avg_rsi", "weeks[].data[].avg_mt_cnt",
            "weeks[].data[].leadership_score", "weeks[].data[].rank_in_week",
            "note",
        ],
        example_object={
            "request_id": "req_demo",
            "exchange": "N",
            "filters": {"type": "CS", "min_constituents": 25, "top_k": 5},
            "week_count": 1,
            "count": 1,
            "weeks": [
                {
                    "weekdate": "YYYY-MM-DD",
                    "count": 1,
                    "data": [
                        {
                            "sector_name": "Sample Sector",
                            "bull_pct": 0.65,
                            "avg_rsi": 108.0,
                            "avg_mt_cnt": 9.0,
                            "leadership_score": 72.45,
                            "rank_in_week": 1,
                        }
                    ],
                }
            ],
        },
        output_summary=(
            "Historical sector leadership rotation with bullish share, average RSI, "
            "trend maturity, leadership_score, and weekly rank."
        ),
        analytical_role=ROLE_LEADERSHIP_INTELLIGENCE,
        notes=[
            "RSI baseline is 100; values above 100 indicate outperformance versus benchmark.",
            "Use bounded date ranges for autonomous workflows.",
        ],
        related_endpoints=["/v1/leadership/summary/latest", "/v1/breadth/sector/history", "/v1/market/regime/history"],
        next_recommended_calls=["/v1/market/regime/history", "/v1/indicators/history"],
        tags=["leadership", "rotation", "history"],
    ),
}


def _fallback_resource_description(path: str) -> str:
    return f"Stock Trends API JSON resource for {path}."


def _fallback_bazaar_output(path: str) -> dict[str, Any]:
    return {
        "type": "json",
        "description": (
            f"Returns a Stock Trends API JSON resource for {path}. "
            "Use /v1/ai/tools, /v1/workflows, and /v1/pricing/catalog for planning metadata."
        ),
        "example": {"request_id": "req_demo"},
    }


def get_endpoint_metadata(path: str, method: str | None = None) -> dict[str, Any] | None:
    entry = _ENDPOINT_METADATA_BY_PATH.get(path)
    if entry is None:
        return None
    if method is not None and entry.get("method") != method.upper():
        return None
    return copy.deepcopy(entry)


def iter_endpoint_metadata() -> list[dict[str, Any]]:
    return [copy.deepcopy(entry) for entry in _ENDPOINT_METADATA_BY_PATH.values()]


def get_resource_description(path: str) -> str:
    entry = _ENDPOINT_METADATA_BY_PATH.get(path)
    if not entry:
        return _fallback_resource_description(path)
    return str(entry.get("resource_description") or _fallback_resource_description(path))


def get_bazaar_output(path: str) -> dict[str, Any]:
    entry = _ENDPOINT_METADATA_BY_PATH.get(path)
    if not entry:
        return _fallback_bazaar_output(path)
    output = entry.get("bazaar_output")
    if not isinstance(output, dict):
        return _fallback_bazaar_output(path)
    return copy.deepcopy(output)


def build_endpoint_preview(
    path: str,
    *,
    pricing_rule_id: str | None = None,
    stc_cost: str | None = None,
    effective_price_usd: str | None = None,
) -> dict[str, Any] | None:
    entry = _ENDPOINT_METADATA_BY_PATH.get(path)
    if entry is None:
        return None

    resolved_pricing_rule_id = pricing_rule_id or entry.get("pricing_rule_id")
    input_location = input_location_for_method(entry["method"])
    preview = {
        "endpoint": {
            "method": entry["method"],
            "path": entry["path"],
            "purpose": entry["purpose"],
            "category": entry["category"],
            "workflow_role": entry["workflow_role"],
            "access_type": entry.get("access_type", "paid"),
            "requires_payment": bool(entry.get("requires_payment", True)),
        },
        "investment_agent_value": entry["investment_agent_value"],
        "supported_rails": list(entry["supported_rails"]),
        "input_rule": entry.get("input_rule"),
        "input_location": input_location,
        "parameter_source": input_location,
        "required_inputs": _inputs_with_parameter_source(entry.get("required_inputs", {}), input_location),
        "optional_inputs": _inputs_with_parameter_source(entry.get("optional_inputs", {}), input_location),
        "safe_example_request": copy.deepcopy(entry["safe_example_request"]),
        "response_shape": copy.deepcopy(entry["response_shape"]),
        "example_object": copy.deepcopy(entry["example_object"]),
        "output_summary": entry["output_summary"],
        "notes": copy.deepcopy(entry.get("notes", [])),
        "related_endpoints": copy.deepcopy(entry.get("related_endpoints", [])),
        "next_recommended_calls": copy.deepcopy(entry.get("next_recommended_calls", [])),
        "pricing": {
            "pricing_rule_id": resolved_pricing_rule_id,
            "stc_cost": stc_cost,
            "effective_price_usd": effective_price_usd,
            "unit": "request",
            "cost_source": "/v1/pricing/catalog",
        },
    }
    for field in ("analytical_role", "interpretation_dependency", "interpretation_guidance", "required_interpretation_steps"):
        if field in entry:
            preview[field] = copy.deepcopy(entry[field])
    return preview


def build_compact_endpoint_preview(
    path: str,
    *,
    pricing_rule_id: str | None = None,
    stc_cost: str | None = None,
    effective_price_usd: str | None = None,
) -> dict[str, Any] | None:
    entry = _ENDPOINT_METADATA_BY_PATH.get(path)
    if entry is None:
        return None

    resolved_pricing_rule_id = pricing_rule_id or entry.get("pricing_rule_id")
    input_location = input_location_for_method(entry["method"])
    preview: dict[str, Any] = {
        "endpoint": {
            "method": entry["method"],
            "path": entry["path"],
            "purpose": entry["purpose"],
            "category": entry["category"],
            "access_type": entry.get("access_type", "paid"),
            "requires_payment": bool(entry.get("requires_payment", True)),
        },
        "supported_rails": list(entry["supported_rails"]),
        "input_location": input_location,
        "parameter_source": input_location,
        "input_summary": _compact_input_summary(input_location),
        "parameter_source_summary": _compact_parameter_source_summary(input_location),
        "output_summary": entry["output_summary"],
        "not_investment_advice": True,
        "not_investment_adviser": True,
        "discovery": {
            "tools_manifest": TOOLS_MANIFEST_URL,
            "ai_context": AI_CONTEXT_URL,
            "workflows": WORKFLOWS_URL,
            "pricing_catalog": PRICING_CATALOG_URL,
            "developer_portal": DEVELOPER_PORTAL_URL,
        },
        "pricing": {
            "pricing_rule_id": resolved_pricing_rule_id,
            "stc_cost": stc_cost,
            "effective_price_usd": effective_price_usd,
            "unit": "request",
            "cost_source": "/v1/pricing/catalog",
        },
    }
    safe_example_request = _small_safe_example_request(entry)
    if safe_example_request is not None:
        preview["safe_example_request"] = safe_example_request
    if entry.get("analytical_role"):
        preview["analytical_role"] = entry["analytical_role"]
    return preview


def input_location_for_method(method: str) -> str:
    return "query" if method.upper() in {"GET", "HEAD", "DELETE"} else "body"


def schema_to_parameters(schema: dict, location: str) -> list[dict[str, Any]]:
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    parameters = []
    for name, prop_schema in properties.items():
        param = {
            "name": name,
            "in": location,
            "input_location": location,
            "parameter_source": location,
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


def _schema_property_from_input_meta(meta: dict[str, Any]) -> dict[str, Any]:
    schema = {
        key: copy.deepcopy(value)
        for key, value in meta.items()
        if key not in {
            "required",
            "safe_default",
            "safe_default_for_demo",
            "example",
            "description",
            "input_location",
            "parameter_source",
        }
    }
    if "description" in meta:
        schema["description"] = meta["description"]
    if "example" in meta:
        schema["example"] = meta["example"]
    if "safe_default" in meta:
        schema["default"] = meta["safe_default"]
    if "safe_default_for_demo" in meta:
        schema["safe_default_for_demo"] = meta["safe_default_for_demo"]
    if "input_location" in meta:
        schema["x-stocktrends-input-location"] = meta["input_location"]
    if "parameter_source" in meta:
        schema["x-stocktrends-parameter-source"] = meta["parameter_source"]
    return schema


def _build_input_schema_from_entry(entry: dict[str, Any], method: str | None = None) -> dict[str, Any]:
    location = input_location_for_method(method or entry["method"])
    properties: dict[str, Any] = {}
    required: list[str] = []
    for source in ("required_inputs", "optional_inputs"):
        for name, meta in entry.get(source, {}).items():
            property_schema = _schema_property_from_input_meta(meta)
            property_schema.setdefault("x-stocktrends-input-location", location)
            property_schema.setdefault("x-stocktrends-parameter-source", location)
            properties[name] = property_schema
            if meta.get("required") is True:
                required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "x-stocktrends-input-location": location,
        "x-stocktrends-parameter-source": location,
    }
    if entry.get("input_rule"):
        schema["description"] = entry["input_rule"]
    elif not properties:
        schema["description"] = f"No {location} parameters are required for this endpoint."
    return schema


def _inputs_with_parameter_source(inputs: dict[str, Any], location: str) -> dict[str, Any]:
    enriched: dict[str, Any] = {}
    for name, meta in inputs.items():
        item = copy.deepcopy(meta)
        item.setdefault("input_location", location)
        item.setdefault("parameter_source", location)
        enriched[name] = item
    return enriched


def build_input_schema(path: str) -> dict[str, Any] | None:
    entry = _ENDPOINT_METADATA_BY_PATH.get(path)
    if entry is None:
        return None

    return _build_input_schema_from_entry(entry)


def build_tool_parameters(path: str) -> list[dict[str, Any]] | None:
    entry = _ENDPOINT_METADATA_BY_PATH.get(path)
    if entry is None:
        return None

    location = input_location_for_method(entry["method"])
    params: list[dict[str, Any]] = []
    for source in ("required_inputs", "optional_inputs"):
        for name, meta in entry.get(source, {}).items():
            param: dict[str, Any] = {
                "name": name,
                "in": location,
                "input_location": location,
                "parameter_source": location,
                "required": bool(meta.get("required")),
                "schema": _schema_property_from_input_meta(
                    {**meta, "input_location": location, "parameter_source": location}
                ),
            }
            if "description" in meta:
                param["description"] = meta["description"]
            if "example" in meta:
                param["example"] = meta["example"]
            if location == "query":
                param["style"] = "form"
                param["explode"] = True
            params.append(param)
    return params


def build_tool_template(path: str) -> dict[str, Any] | None:
    entry = _ENDPOINT_METADATA_BY_PATH.get(path)
    if entry is None:
        return None

    input_location = input_location_for_method(entry["method"])
    input_schema = build_input_schema(path) or {"type": "object", "properties": {}, "required": []}
    template = {
        "name": entry["tool_name"],
        "title": entry["title"],
        "description": (
            f"{entry['purpose']} {entry['investment_agent_value']} "
            "Fetch /v1/pricing/catalog for current STC cost."
        ),
        "endpoint": entry["path"],
        "method": entry["method"],
        "category": entry["category"],
        "access_type": entry.get("access_type", "paid"),
        "requires_payment": bool(entry.get("requires_payment", True)),
        "input_location": input_location,
        "parameter_source": input_location,
        "input_schema": input_schema,
        "output_summary": entry["output_summary"],
        "workflow_role": entry["workflow_role"],
        "investment_agent_value": entry["investment_agent_value"],
        "required_inputs": _inputs_with_parameter_source(entry.get("required_inputs", {}), input_location),
        "optional_inputs": _inputs_with_parameter_source(entry.get("optional_inputs", {}), input_location),
        "safe_example_request": copy.deepcopy(entry["safe_example_request"]),
        "related_endpoints": copy.deepcopy(entry.get("related_endpoints", [])),
        "next_recommended_calls": copy.deepcopy(entry.get("next_recommended_calls", [])),
    }
    for field in ("analytical_role", "interpretation_dependency", "interpretation_guidance", "required_interpretation_steps"):
        if field in entry:
            template[field] = copy.deepcopy(entry[field])
    if input_location == "query":
        template["parameters"] = build_tool_parameters(path) or []
    else:
        template["request_body_schema"] = input_schema
    return template


def _fallback_input_schema(location: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "description": f"No {location} parameters are declared for this resource.",
        "x-stocktrends-input-location": location,
        "x-stocktrends-parameter-source": location,
    }


def _bazaar_safe_input_schema(input_schema: dict[str, Any], location: str) -> dict[str, Any]:
    schema = copy.deepcopy(input_schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema.setdefault("required", [])
    schema.setdefault("description", f"No {location} parameters are required for this endpoint.")
    schema.setdefault("additionalProperties", False)
    schema["x-stocktrends-input-location"] = location
    schema["x-stocktrends-parameter-source"] = location
    return schema


def _bazaar_output_schema(entry: dict[str, Any] | None, bazaar_output: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "description": str(
            (entry or {}).get("output_summary")
            or bazaar_output.get("description")
            or "JSON response returned after successful payment."
        ),
        "properties": {
            "type": {"type": "string", "const": "json"},
            "description": {"type": "string"},
            "example": {},
            "response_shape": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["type", "description", "example"],
        "additionalProperties": True,
    }


def _bazaar_endpoint_description(entry: dict[str, Any] | None, path: str) -> str:
    if not entry:
        return (
            f"Stock Trends API market-research resource for {path}. "
            "Use with budget controls; not investment advice."
        )
    return (
        f"{entry['purpose']} Analytical role: {entry.get('analytical_role', entry['category'])}. "
        f"Workflow context: {entry['workflow_role']} Expected output: {entry['output_summary']} "
        "Use for market-research and decision-support context; not investment advice."
    )


def _compact_input_summary(location: str) -> str:
    if location == "query":
        return "Supply endpoint parameters as query parameters; fetch tools_manifest for the full schema."
    return "Supply endpoint parameters as a JSON request body; fetch tools_manifest for the full schema."


def _compact_parameter_source_summary(location: str) -> str:
    return f"Parameter source is {location}; full names, defaults, and limits are in tools_manifest."


def _small_safe_example_request(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry:
        return None
    example = copy.deepcopy(entry.get("safe_example_request"))
    if not isinstance(example, dict):
        return None
    encoded = json.dumps(example, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > COMPACT_SAFE_EXAMPLE_MAX_BYTES:
        return None
    return example


def _compact_bazaar_input_example(
    entry: dict[str, Any] | None,
    *,
    http_method: str,
    location: str,
    path: str,
) -> dict[str, Any]:
    example = _small_safe_example_request(entry) or {
        "method": http_method,
        "path": path,
        "query" if location == "query" else "json": {},
    }
    example_key = "query" if location == "query" else "json"
    example_input = example.get(example_key)
    return copy.deepcopy(example_input) if isinstance(example_input, dict) else {}


def _compact_json_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_compact_json_schema(item) for item in schema]

    if not isinstance(schema, dict):
        return copy.deepcopy(schema)

    keep_keys = {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "const",
        "format",
        "pattern",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "additionalProperties",
        "default",
    }
    compact: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            compact[key] = {
                prop_name: _compact_json_schema(prop_schema)
                for prop_name, prop_schema in value.items()
            }
            continue
        if key in keep_keys:
            compact[key] = _compact_json_schema(value)

    if compact.get("type") == "object":
        compact.setdefault("properties", {})
        compact.setdefault("required", [])
        compact.setdefault("additionalProperties", False)
    return compact


def _compact_bazaar_output_info(
    entry: dict[str, Any] | None,
    bazaar_output: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "json",
        "format": "application/json",
        "example": {"request_id": "req_demo"},
    }


def _compact_bazaar_output_schema(
    entry: dict[str, Any] | None,
    bazaar_output: dict[str, Any],
) -> dict[str, Any]:
    description = str(
        (entry or {}).get("output_summary")
        or bazaar_output.get("description")
        or "JSON response returned after successful payment."
    )
    return {
        "type": "object",
        "description": description,
        "properties": {
            "type": {"type": "string", "const": "json"},
            "format": {"type": "string"},
            "example": {},
        },
        "required": ["type"],
        "additionalProperties": True,
    }


def _compact_bazaar_info_schema(
    *,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    http_method: str,
    location: str,
) -> dict[str, Any]:
    input_schema_property = "queryParams" if location == "query" else "body"
    input_properties: dict[str, Any] = {
        "type": {"type": "string", "const": "http"},
        "method": {"type": "string", "enum": [http_method]},
        input_schema_property: input_schema,
    }
    input_required = ["type", "method"]
    if location == "body":
        input_properties["bodyType"] = {"type": "string", "enum": ["json"]}
        input_required.extend(["bodyType", "body"])

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "input": {
                "type": "object",
                "properties": input_properties,
                "required": input_required,
                "additionalProperties": False,
            },
            "output": output_schema,
            "title": {"type": "string"},
            "description": {"type": "string"},
            "category": {"type": "string"},
            "family": {"type": "string"},
            "tools_manifest": {"type": "string"},
            "metadataUrl": {"type": "string"},
            "schemaUrl": {"type": "string"},
            "pricing_catalog": {"type": "string"},
        },
        "required": ["input"],
        "additionalProperties": True,
    }


def build_bazaar_extension(path: str, method: str | None = None) -> dict[str, Any]:
    """
    Build Coinbase/x402 Bazaar v2 discovery metadata for a Payment Required response.

    The structure is intentionally metadata-only. It consumes the central endpoint
    registry and never resolves prices, verifies payments, or changes endpoint
    execution behavior.
    """
    entry = _ENDPOINT_METADATA_BY_PATH.get(path)
    http_method = (method or (entry or {}).get("method") or "GET").upper()
    location = input_location_for_method(http_method)
    input_schema = (
        _build_input_schema_from_entry(entry, http_method)
        if entry is not None
        else _fallback_input_schema(location)
    )
    input_schema = _bazaar_safe_input_schema(input_schema, location)

    bazaar_output = get_bazaar_output(path)
    output_schema = _bazaar_output_schema(entry, bazaar_output)
    output_info = copy.deepcopy(bazaar_output)
    output_info.setdefault("type", "json")
    output_info.setdefault("description", output_schema["description"])
    output_info.setdefault("example", {"request_id": "req_demo"})
    if entry is not None:
        output_info["response_shape"] = copy.deepcopy(entry.get("response_shape", []))
    output_info["schema"] = output_schema

    safe_example_request = copy.deepcopy(
        (entry or {}).get(
            "safe_example_request",
            {"method": http_method, "path": path, "query" if location == "query" else "json": {}},
        )
    )

    input_info: dict[str, Any] = {
        "type": "http",
        "method": http_method,
        # Generic stable accessor; query/body below exposes the method-specific location.
        "schema": input_schema,
        "parameters": schema_to_parameters(input_schema, location),
        "example": safe_example_request,
    }
    input_schema_property_name = "query" if location == "query" else "body"
    input_info[input_schema_property_name] = input_schema
    if location == "body":
        input_info["bodyType"] = "json"

    info: dict[str, Any] = {
        "service_name": SERVICE_NAME,
        "service_category": SERVICE_CATEGORY,
        "title": str((entry or {}).get("title") or path),
        "description": _bazaar_endpoint_description(entry, path),
        "analytical_role": (entry or {}).get("analytical_role"),
        "research_goal": (entry or {}).get("purpose"),
        "endpoint_family": (entry or {}).get("category"),
        "workflow_context": (entry or {}).get("workflow_role"),
        "interpretation_dependencies": {
            "dependency": copy.deepcopy((entry or {}).get("interpretation_dependency")),
            "guidance": copy.deepcopy((entry or {}).get("interpretation_guidance")),
            "required_steps": copy.deepcopy((entry or {}).get("required_interpretation_steps")),
        },
        "related_endpoints": copy.deepcopy((entry or {}).get("related_endpoints", [])),
        "next_recommended_calls": copy.deepcopy((entry or {}).get("next_recommended_calls", [])),
        "safe_for_autonomous_execution_with_budget_controls": True,
        "state_mutation": False,
        "market_research_context": True,
        "decision_support_context": True,
        "not_investment_advice": True,
        "not_investment_adviser": True,
        "developer_portal": DEVELOPER_PORTAL_URL,
        "ai_context": AI_CONTEXT_URL,
        "tools_manifest": TOOLS_MANIFEST_URL,
        "workflows": WORKFLOWS_URL,
        "pricing_catalog": PRICING_CATALOG_URL,
        "input": input_info,
        "output": output_info,
        "examples": [safe_example_request],
    }

    # schema.properties.input must be the direct callable-parameter schema so that
    # Bazaar's indexer finds named parameters at the top level of input.properties.
    # The protocol envelope (type/method/query/body) lives in info.input only.
    return {
        "bazaar": {
            "info": info,
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "title": str((entry or {}).get("title") or path),
                "description": _bazaar_endpoint_description(entry, path),
                "properties": {
                    "input": input_schema,
                    "output": output_schema,
                },
                "required": ["input", "output"],
            },
        }
    }


def build_compact_bazaar_extension(path: str, method: str | None = None) -> dict[str, Any]:
    """
    Build compact Bazaar metadata for x402 challenge objects.

    Challenge objects must stay small enough for x402 agents, crawlers,
    proxies, and JS fetch clients. It still preserves the official Bazaar
    info/schema contract inline so CDP can validate and index callable input.
    Rich Stock Trends semantics live in stocktrends_preview and discovery
    manifests.
    """
    entry = _ENDPOINT_METADATA_BY_PATH.get(path)
    http_method = (method or (entry or {}).get("method") or "GET").upper()
    location = input_location_for_method(http_method)
    category = (entry or {}).get("category")

    input_schema = (
        _build_input_schema_from_entry(entry, http_method)
        if entry is not None
        else _fallback_input_schema(location)
    )
    input_schema = _compact_json_schema(_bazaar_safe_input_schema(input_schema, location))
    input_example = _compact_bazaar_input_example(
        entry,
        http_method=http_method,
        location=location,
        path=path,
    )
    bazaar_output = get_bazaar_output(path)
    output_info = _compact_bazaar_output_info(entry, bazaar_output)
    output_schema = _compact_bazaar_output_schema(entry, bazaar_output)

    input_info: dict[str, Any] = {
        "type": "http",
        "method": http_method,
    }
    if location == "query":
        input_info["queryParams"] = input_example
    else:
        input_info["bodyType"] = "json"
        input_info["body"] = input_example

    info: dict[str, Any] = {
        "title": str((entry or {}).get("title") or path),
        "description": get_resource_description(path),
        "category": category,
        "family": category,
        "tools_manifest": TOOLS_MANIFEST_URL,
        "metadataUrl": AI_CONTEXT_URL,
        "schemaUrl": TOOLS_MANIFEST_URL,
        "pricing_catalog": PRICING_CATALOG_URL,
        "input": input_info,
        "output": output_info,
    }

    if entry is not None and entry.get("analytical_role"):
        info["role"] = entry["analytical_role"]

    return {
        "bazaar": {
            "info": info,
            "schema": _compact_bazaar_info_schema(
                input_schema=input_schema,
                output_schema=output_schema,
                http_method=http_method,
                location=location,
            ),
        }
    }
