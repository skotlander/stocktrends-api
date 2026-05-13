from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from db import get_metering_engine
from discovery.endpoint_metadata import get_endpoint_metadata

logger = logging.getLogger("stocktrends_api.workflows")

router = APIRouter(tags=["workflows"])

# ---------------------------------------------------------------------------
# STC → USD conversion rate.
# STC ≈ $1.00 USD. This is the single authoritative conversion constant.
# Update here (and only here) if the rate changes.
# ---------------------------------------------------------------------------
STC_TO_USD = Decimal("1.00")

# ---------------------------------------------------------------------------
# WORKFLOW REGISTRY
#
# Defines the static composition of each workflow: which endpoints are
# called, in what order, and which pricing_rule_id applies to each step.
#
# STC costs are NOT stored here. They are resolved at request time from
# api_pricing_rules (metering DB) so that pricing changes are reflected
# automatically without a code deployment. This eliminates the drift risk
# that would exist if costs were hard-coded.
#
# Integrity contract:
#   Every pricing_rule_id listed here must have an active row in
#   api_pricing_rules. If any rule is missing, GET /v1/workflows returns
#   HTTP 500. This surfaces registry drift immediately rather than silently
#   serving incorrect data.
#
# Verify before merging:
#   SELECT rule_name, cost_per_request FROM api_pricing_rules
#   WHERE rule_name IN (<all pricing_rule_ids below>) AND is_active = 1;
#   Expected: one row per rule_id.
#
# Adding a workflow:
#   1. Confirm all step pricing_rule_ids exist in api_pricing_rules.
#   2. Add the entry below.
#   3. Verify GET /v1/workflows response against GET /v1/pricing/catalog.
# ---------------------------------------------------------------------------
WORKFLOW_REGISTRY: list[dict] = [
    {
        "workflow_id": "regime_analysis",
        "name": "Market Regime Analysis",
        "description": (
            "Full market regime intelligence pipeline: current regime classification, "
            "historical regime sequence for context, and probabilistic forward forecast."
        ),
        "tags": ["agent", "research", "regime"],
        "supported_rails": ["subscription", "x402", "mpp"],
        "decision_guidance": (
            "Use when the agent needs market-level context before selecting bullish, bearish, "
            "or mixed symbol and portfolio workflows."
        ),
        "best_for": "Market context before symbol selection, screening, or portfolio construction.",
        "agent_goal_examples": [
            "Decide whether a portfolio agent should use bullish, bearish, or mixed candidate filters.",
            "Summarize current regime and recent regime direction before selecting symbols.",
        ],
        "symbol_selection_guidance": (
            "This workflow does not select symbols directly; use its regime result to choose "
            "bias filters for /v1/agent/screener/top or /v1/portfolio/construct."
        ),
        "next_step_guidance": [
            "Use /v1/agent/screener/top to discover candidates.",
            "Use /v1/decision/evaluate-symbol for a known symbol.",
            "Use /v1/portfolio/construct when the goal is an allocation proposal.",
        ],
        "steps": [
            {
                "step_id": "regime_latest",
                "endpoint": "GET /v1/market/regime/latest",
                "pricing_rule_id": "market_regime_latest",
                "description": "Retrieve the current market regime classification.",
                "optional": False,
            },
            {
                "step_id": "regime_history",
                "endpoint": "GET /v1/market/regime/history",
                "pricing_rule_id": "market_regime_history",
                "description": "Retrieve historical regime sequence for context.",
                "optional": True,
            },
            {
                "step_id": "regime_forecast",
                "endpoint": "GET /v1/market/regime/forecast",
                "pricing_rule_id": "market_regime_forecast",
                "description": "Retrieve probabilistic forward regime forecast.",
                "optional": False,
            },
        ],
    },
    {
        "workflow_id": "symbol_decision",
        "name": "Regime-Aware Symbol Decision",
        "description": (
            "Classify the current market regime then evaluate a single symbol "
            "for a buy/sell/hold decision in that regime context."
        ),
        "tags": ["agent", "research", "decision"],
        "supported_rails": ["subscription", "x402", "mpp"],
        "decision_guidance": (
            "Use when the agent has a target symbol and needs deterministic Stock Trends signal "
            "and regime context before deeper history or portfolio calls."
        ),
        "best_for": "Evaluating a known symbol in current market-regime context.",
        "agent_goal_examples": [
            "Evaluate IBM-N after resolving it from /v1/instruments/resolve.",
            "Score a symbol carried forward from a screener, STWR report, or portfolio position.",
        ],
        "symbol_selection_guidance": (
            "Provide symbol_exchange directly when available. If only a ticker is known, first call "
            "/v1/instruments/lookup or /v1/instruments/resolve and reuse the returned symbol_exchange."
        ),
        "next_step_guidance": [
            "Use /v1/indicators/latest or /v1/stim/latest for deeper single-symbol context.",
            "Use /v1/portfolio/evaluate when comparing the symbol inside an allocation.",
        ],
        "steps": [
            {
                "step_id": "regime_latest",
                "endpoint": "GET /v1/market/regime/latest",
                "pricing_rule_id": "market_regime_latest",
                "description": "Retrieve the current market regime classification.",
                "optional": False,
            },
            {
                "step_id": "evaluate_symbol",
                "endpoint": "POST /v1/decision/evaluate-symbol",
                "pricing_rule_id": "evaluate_symbol",
                "description": "Evaluate a symbol buy/sell/hold decision given the current regime.",
                "optional": False,
            },
        ],
    },
    {
        "workflow_id": "portfolio_build",
        "name": "Screener → Portfolio Build",
        "description": (
            "Screen for qualifying tickers, construct a portfolio from candidates, "
            "then evaluate the constructed portfolio's risk and return profile."
        ),
        "tags": ["agent", "portfolio", "research"],
        "supported_rails": ["subscription", "x402", "mpp"],
        "decision_guidance": (
            "Use when the agent wants a fresh candidate list and a bounded equal-weight portfolio proposal."
        ),
        "best_for": "First portfolio workflow for agents that need to discover symbols and build an allocation.",
        "agent_goal_examples": [
            "Build a 5-stock equal-weight candidate portfolio from fresh Stock Trends signals.",
            "Use screener output symbols as the candidate context for a portfolio construction run.",
        ],
        "symbol_selection_guidance": (
            "Start with /v1/agent/screener/top. Reuse results[].symbol_exchange from the screener "
            "when calling /v1/decision/evaluate-symbol, /v1/stim/latest, /v1/portfolio/evaluate, "
            "or /v1/portfolio/compare. Skip duplicate paid symbol calls for repeated symbol_exchange values."
        ),
        "next_step_guidance": [
            "Use /v1/portfolio/compare to compare the constructed portfolio against an existing allocation.",
            "Use /v1/stim/latest for selected symbols when forward-return distributions are needed.",
        ],
        "steps": [
            {
                "step_id": "screener_top",
                "endpoint": "GET /v1/agent/screener/top",
                "pricing_rule_id": "agent_screener_top",
                "description": "Screen for top qualifying tickers.",
                "optional": False,
            },
            {
                "step_id": "portfolio_construct",
                "endpoint": "POST /v1/portfolio/construct",
                "pricing_rule_id": "portfolio_construct",
                "description": "Construct a portfolio from screened candidates.",
                "optional": False,
            },
            {
                "step_id": "portfolio_evaluate",
                "endpoint": "POST /v1/portfolio/evaluate",
                "pricing_rule_id": "portfolio_evaluate",
                "description": "Evaluate risk and return profile of the constructed portfolio.",
                "optional": False,
            },
        ],
    },
    {
        "workflow_id": "portfolio_compare_review",
        "name": "Portfolio Compare Review",
        "description": (
            "Evaluate an existing portfolio, construct a proposed alternative, "
            "then compare the two portfolios to quantify the difference."
        ),
        "tags": ["agent", "portfolio"],
        "supported_rails": ["subscription", "x402", "mpp"],
        "decision_guidance": (
            "Use when the agent needs to evaluate an existing allocation, construct an alternative, "
            "and compare both under the same Stock Trends decision framework."
        ),
        "best_for": "Comparing a current allocation against a proposed Stock Trends-informed alternative.",
        "agent_goal_examples": [
            "Evaluate a user portfolio, construct a proposed alternative, then compare score and alignment.",
            "Review whether a newly constructed portfolio improves regime alignment versus a baseline.",
        ],
        "symbol_selection_guidance": (
            "The compare endpoint expects left and right as direct arrays of positions, not wrapper "
            "objects. Reuse portfolio arrays returned by /v1/portfolio/construct or positions supplied "
            "to /v1/portfolio/evaluate."
        ),
        "next_step_guidance": [
            "Use comparison.winner, score_delta, and alignment_advantage to summarize differences.",
            "Reuse any symbol_exchange values in losing positions for deeper /v1/decision/evaluate-symbol review.",
        ],
        "steps": [
            {
                "step_id": "evaluate_current",
                "endpoint": "POST /v1/portfolio/evaluate",
                "pricing_rule_id": "portfolio_evaluate",
                "description": "Evaluate current portfolio risk and return profile.",
                "optional": False,
            },
            {
                "step_id": "construct_proposed",
                "endpoint": "POST /v1/portfolio/construct",
                "pricing_rule_id": "portfolio_construct",
                "description": "Construct the proposed alternative portfolio.",
                "optional": False,
            },
            {
                "step_id": "compare_portfolios",
                "endpoint": "POST /v1/portfolio/compare",
                "pricing_rule_id": "portfolio_compare",
                "description": "Compare current and proposed portfolios to quantify the difference.",
                "optional": False,
            },
        ],
    },
]

WORKFLOW_ID_EXAMPLES = [
    "regime_analysis",
    "symbol_decision",
    "portfolio_build",
    "portfolio_compare_review",
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_VALID_RAIL_PREFERENCES = frozenset({"subscription", "x402", "mpp", "auto"})


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    method, path = endpoint.split(" ", 1)
    return method.strip().upper(), path.strip()


def _collect_registry_rule_ids() -> set[str]:
    """Return all unique pricing_rule_ids referenced across the registry."""
    return {
        step["pricing_rule_id"]
        for workflow in WORKFLOW_REGISTRY
        for step in workflow["steps"]
    }


def _fetch_active_pricing_costs() -> dict[str, float]:
    """
    Fetch cost_per_request for all active pricing rules from api_pricing_rules.

    Returns a dict of {rule_name: cost_per_request}.
    Raises on DB error — callers must handle.
    """
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
    return {
        row["rule_name"]: float(row["cost_per_request"])
        for row in rows
        if row["cost_per_request"] is not None
    }


def _resolve_workflow_costs(
    workflow: dict,
    cost_map: dict[str, float],
) -> tuple[list[dict], float]:
    """
    Resolve per-step costs for a workflow using the supplied cost_map.

    Returns (steps_with_costs, total_stc_cost).
    Raises KeyError(rule_id) if any pricing_rule_id is absent from cost_map.
    """
    steps = []
    total = 0.0
    for step in workflow["steps"]:
        rule_id = step["pricing_rule_id"]
        if rule_id not in cost_map:
            raise KeyError(rule_id)
        stc_cost = cost_map[rule_id]
        method, path = _split_endpoint(step["endpoint"])
        endpoint_metadata = get_endpoint_metadata(path, method) or {}
        steps.append(
            {
                "step_id": step["step_id"],
                "endpoint": step["endpoint"],
                "method": method,
                "path": path,
                "pricing_rule_id": rule_id,
                "stc_cost": stc_cost,
                "estimated_usd_cost": float(Decimal(str(stc_cost)) * STC_TO_USD),
                "supported_rails": endpoint_metadata.get("supported_rails", workflow.get("supported_rails", [])),
                "pricing_note": "STC is the source of truth; payment rails translate this STC cost.",
                "description": step["description"],
                "purpose": endpoint_metadata.get("purpose", step["description"]),
                "workflow_role": endpoint_metadata.get("workflow_role"),
                "required_inputs": endpoint_metadata.get("required_inputs", {}),
                "optional_inputs": endpoint_metadata.get("optional_inputs", {}),
                "safe_example_request": endpoint_metadata.get("safe_example_request"),
                "output_summary": endpoint_metadata.get("output_summary"),
                "related_endpoints": endpoint_metadata.get("related_endpoints", []),
                "optional": step["optional"],
            }
        )
        total += stc_cost
    return steps, total


# ---------------------------------------------------------------------------
# GET /v1/workflows
# Public, non-metered. Costs resolved live from api_pricing_rules.
# ---------------------------------------------------------------------------


@router.get(
    "/workflows",
    summary="Workflow registry",
    description=(
        "Returns the static workflow registry with live per-step STC costs resolved "
        "from api_pricing_rules. Costs are authoritative and consistent with "
        "GET /v1/pricing/catalog. No authentication required. "
        "Each step includes method, path, pricing_rule_id, STC cost, estimated USD cost, "
        "input guidance, safe example request, output summary, and decision guidance. "
        "Returns HTTP 500 if any pricing_rule_id in the registry has no active row "
        "in api_pricing_rules — this surfaces drift immediately."
    ),
)
def get_workflows() -> JSONResponse:
    try:
        cost_map = _fetch_active_pricing_costs()
    except Exception as exc:
        logger.error("Workflows: pricing rule fetch failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Pricing data unavailable")

    # First pass: detect any missing pricing rules before building the response.
    results: list[tuple[dict, list[dict], float]] = []
    missing_rules: list[str] = []

    for workflow in WORKFLOW_REGISTRY:
        try:
            steps, total_stc_cost = _resolve_workflow_costs(workflow, cost_map)
            results.append((workflow, steps, total_stc_cost))
        except KeyError as exc:
            missing_rules.append(str(exc))

    if missing_rules:
        logger.error(
            "Workflows: registry integrity error — pricing_rule_id(s) not found "
            "in api_pricing_rules: %s",
            missing_rules,
        )
        raise HTTPException(
            status_code=500,
            detail="Registry integrity error: one or more pricing rules are missing from api_pricing_rules",
        )

    workflows = [
        {
            "workflow_id": w["workflow_id"],
            "name": w["name"],
            "description": w["description"],
            "tags": w["tags"],
            "supported_rails": w["supported_rails"],
            "total_stc_cost": total,
            "total_estimated_usd_cost": float(Decimal(str(total)) * STC_TO_USD),
            "pricing_note": (
                "STC is the pricing source of truth. The estimated USD value uses the current "
                "operational policy of 1 STC approximately 1 USD and is for planning only."
            ),
            "decision_guidance": w.get("decision_guidance"),
            "best_for": w.get("best_for"),
            "agent_goal_examples": w.get("agent_goal_examples", []),
            "symbol_selection_guidance": w.get("symbol_selection_guidance"),
            "next_step_guidance": w.get("next_step_guidance", []),
            "steps": steps,
        }
        for w, steps, total in results
    ]

    return JSONResponse(
        content={
            "recommended_starting_workflow": {
                "workflow_id": "portfolio_build",
                "reason": (
                    "Recommended first workflow for autonomous portfolio agents because it starts "
                    "with symbol discovery, constructs a bounded portfolio, and produces reusable "
                    "symbol_exchange values for downstream evaluation and comparison."
                ),
            },
            "agent_guidance": {
                "ordered_planning_steps": [
                    "Fetch /v1/ai/tools for tool metadata and parameter locations.",
                    "Fetch /v1/pricing/catalog to resolve current STC costs by pricing_rule_id.",
                    "Choose a workflow here, then execute steps in order.",
                    "Reuse symbol_exchange values from screeners, selections, or portfolio_construct outputs.",
                    "Skip duplicate paid symbol calls when a symbol_exchange was already resolved in the workflow.",
                ],
                "payment_rails": ["subscription", "x402", "mpp"],
                "pricing_note": "STC is the pricing source of truth across all rails.",
            },
            "workflows": workflows,
        }
    )


# ---------------------------------------------------------------------------
# GET /v1/cost-estimate
# Public, non-metered.
# Pure arithmetic — no new DB schema, no new pricing model.
# quota_remaining is caller-supplied in v1 (interim design).
# ---------------------------------------------------------------------------


@router.get(
    "/cost-estimate",
    summary="Workflow cost estimate",
    description=(
        "Returns a deterministic cost estimate for a named workflow. "
        "Costs are resolved from live pricing rules (api_pricing_rules). "
        "Public and non-metered: no API key, payment, or usage charge is required for this call. "
        "quota_remaining is caller-supplied in v1 — accuracy depends on the caller's "
        "knowledge of their current usage state. "
        "v2 will resolve quota_remaining server-side for authenticated callers."
    ),
)
def get_cost_estimate(
    request: Request,
    workflow_id: str = Query(
        ...,
        description=(
            "Workflow ID. Safe executable examples: regime_analysis, symbol_decision, "
            "portfolio_build, portfolio_compare_review. See GET /v1/workflows for details."
        ),
        json_schema_extra={"enum": WORKFLOW_ID_EXAMPLES},
    ),
    quota_remaining: Optional[int] = Query(
        None,
        ge=0,
        description=(
            "Caller's current subscription quota remaining. "
            "Used for hybrid subscription/x402 rail assignment. "
            "v1: caller-supplied. Omit for a subscription-only estimate."
        ),
    ),
    rail_preference: Optional[str] = Query(
        None,
        description="Rail preference for assignment: subscription | x402 | mpp | auto (default: auto).",
    ),
) -> JSONResponse:

    # --- Validate rail_preference ---
    effective_rail_pref = (rail_preference or "auto").strip().lower()
    if effective_rail_pref not in _VALID_RAIL_PREFERENCES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid rail_preference '{rail_preference}'. "
                f"Must be one of: {sorted(_VALID_RAIL_PREFERENCES)}"
            ),
        )

    # --- Look up workflow ---
    workflow = next(
        (w for w in WORKFLOW_REGISTRY if w["workflow_id"] == workflow_id),
        None,
    )
    if workflow is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{workflow_id}' not found. See GET /v1/workflows for available IDs.",
        )

    # --- Validate rail_preference against workflow supported_rails ---
    if effective_rail_pref != "auto" and effective_rail_pref not in workflow["supported_rails"]:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Rail '{effective_rail_pref}' is not supported by workflow '{workflow_id}'. "
                f"Supported rails: {workflow['supported_rails']}"
            ),
        )

    # --- Resolve costs from api_pricing_rules ---
    try:
        cost_map = _fetch_active_pricing_costs()
    except Exception as exc:
        logger.error("Cost estimate: pricing rule fetch failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Pricing data unavailable")

    try:
        steps_base, _ = _resolve_workflow_costs(workflow, cost_map)
    except KeyError as exc:
        logger.error(
            "Cost estimate: pricing_rule_id '%s' not found in api_pricing_rules",
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Registry integrity error: pricing rule not found in api_pricing_rules",
        )

    # --- Rail assignment ---
    step_count = len(steps_base)
    notes: list[str] = []

    if effective_rail_pref == "subscription":
        assigned_rails = ["subscription"] * step_count

    elif effective_rail_pref in {"x402", "mpp"}:
        assigned_rails = [effective_rail_pref] * step_count
        if quota_remaining is not None:
            notes.append(
                "quota_remaining was supplied but ignored; "
                f"all steps assigned to {effective_rail_pref} per rail_preference"
            )

    else:  # "auto"
        if quota_remaining is not None:
            n_sub = min(quota_remaining, step_count)
            assigned_rails = ["subscription"] * n_sub + ["x402"] * (step_count - n_sub)
            notes.append(
                "quota_remaining is caller-supplied; subscription assignment is illustrative "
                "and may not reflect actual quota state"
            )
        else:
            assigned_rails = ["subscription"] * step_count
            notes.append(
                "quota_remaining not supplied; subscription assignment is illustrative "
                "and may not reflect actual quota state"
            )

    # --- Build per-step financials ---
    steps_out: list[dict] = []
    total_stc_cost = Decimal("0")
    total_usd_cost = Decimal("0")
    subscription_step_count = 0

    for step, assigned_rail in zip(steps_base, assigned_rails):
        stc_cost = Decimal(str(step["stc_cost"]))
        if assigned_rail == "subscription":
            usd_cost = Decimal("0.00")
            quota_impact = 1
            subscription_step_count += 1
        else:
            usd_cost = stc_cost * STC_TO_USD
            quota_impact = 0

        total_stc_cost += stc_cost
        total_usd_cost += usd_cost

        steps_out.append(
            {
                "step_id": step["step_id"],
                "method": step.get("method"),
                "path": step.get("path"),
                "pricing_rule_id": step["pricing_rule_id"],
                "stc_cost": float(stc_cost),
                "usd_cost": float(usd_cost),
                "assigned_rail": assigned_rail,
                "quota_impact": quota_impact,
                "purpose": step.get("purpose"),
                "safe_example_request": step.get("safe_example_request"),
            }
        )

    # --- Resolve top-level rail label ---
    rails_used = {s["assigned_rail"] for s in steps_out}
    if rails_used == {"subscription"}:
        resolved_rail = "subscription"
    elif rails_used == {"x402"}:
        resolved_rail = "x402"
    elif rails_used == {"mpp"}:
        resolved_rail = "mpp"
    else:
        resolved_rail = "mixed"

    # --- quota_sufficient ---
    # Answers: "is there enough quota to cover the full workflow on subscription?"
    # Compares against total step_count, not subscription_step_count.
    # Using subscription_step_count would collapse to 0 when quota_remaining=0
    # (all steps routed to x402), producing the wrong result 0 >= 0 = True.
    if quota_remaining is not None:
        quota_sufficient: Optional[bool] = quota_remaining >= step_count
    else:
        quota_sufficient = None

    return JSONResponse(
        content={
            "workflow_id": workflow_id,
            "rail": resolved_rail,
            "total_stc_cost": float(total_stc_cost),
            "total_usd_cost": float(total_usd_cost),
            "quota_remaining_supplied": quota_remaining,
            "quota_sufficient": quota_sufficient,
            "steps": steps_out,
            "notes": notes,
        }
    )
