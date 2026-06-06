"""
Tests for GET /v1/ai/tools — MCP tools manifest endpoint.

DB-importing modules are mocked at the sys.modules level so these tests
run without a database connection or sqlalchemy installed.

Validates:
1. Endpoint is registered in public paths (auth bypass)
2. Endpoint is registered in non-metered paths (no billing)
3. Response structure is stable and complete
4. Tool definitions reference real endpoints only
5. Workflows derive from WORKFLOW_REGISTRY (no duplication)
6. Pricing section references STC model without hardcoded costs
7. Auth section reflects actual system behavior
8. Runtime-derived metadata matches classifier / policy sources
"""

import sys
import importlib
from decimal import Decimal
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Mock DB / ORM dependencies before any project imports that need them.
# The endpoint itself is fully static (no DB calls), so these mocks exist
# only to satisfy module-level imports in router and db modules.
# ---------------------------------------------------------------------------
_DB_MOCK = MagicMock()
_SQLALCHEMY_MOCK = MagicMock()

sys.modules.setdefault("sqlalchemy", _SQLALCHEMY_MOCK)
sys.modules.setdefault("sqlalchemy.orm", _SQLALCHEMY_MOCK)
sys.modules.setdefault("db", _DB_MOCK)

# Now safe to import project modules
import routers.ai as ai_router
from routers.ai import _TOOL_TEMPLATES, _MANIFEST_PUBLIC_PATHS, _build_workflow_summary, ai_context, ai_tools
from routers.workflows import WORKFLOW_REGISTRY
from discovery.endpoint_metadata import build_input_schema
from pricing.classifier import NON_METERED_PATHS, classify_request
from payments.policy_provider import (
    is_free_metered_path,
    is_agent_pay_route,
    get_agent_pay_auth_bypass_methods,
)

_PUBLIC_METERED_PLANNING_EXCEPTIONS = frozenset({
    # Current runtime behavior: public in ApiKeyMiddleware, but classified as
    # metered planning metadata. This PR only corrects discovery metadata;
    # enforcement/classifier reconciliation is a follow-up.
    "/v1/pricing/catalog",
})


# ---------------------------------------------------------------------------
# 1. Public path registration
# ---------------------------------------------------------------------------

def test_ai_tools_in_api_key_middleware_source():
    """Confirm the actual middleware source file includes /v1/ai/tools."""
    import pathlib
    source = pathlib.Path("middleware/api_key.py").read_text(encoding="utf-8")
    assert '"/v1/ai/tools"' in source, "/v1/ai/tools not found in ApiKeyMiddleware public_paths"


# ---------------------------------------------------------------------------
# 2. Non-metered path registration
# ---------------------------------------------------------------------------

def test_ai_tools_in_non_metered_paths():
    """/v1/ai/tools must be in NON_METERED_PATHS (free, no tracking)."""
    assert "/v1/ai/tools" in NON_METERED_PATHS


def test_classify_request_free_decision_for_ai_tools():
    """classify_request must return is_metered=0 and access_granted=True."""
    decision = classify_request(
        path="/v1/ai/tools",
        has_paid_auth=False,
        payment_method_header=None,
        plan_code=None,
        agent_identifier=None,
    )
    assert decision.is_metered == 0
    assert decision.access_granted is True
    assert decision.log_pricing_rule_id == "default_free"
    assert decision.econ_payment_required == 0


def test_classify_request_free_even_with_auth():
    """Providing a paid auth context must NOT make this endpoint metered."""
    decision = classify_request(
        path="/v1/ai/tools",
        has_paid_auth=True,
        payment_method_header=None,
        plan_code="pro",
        agent_identifier=None,
    )
    assert decision.is_metered == 0
    assert decision.econ_payment_required == 0


# ---------------------------------------------------------------------------
# 3. Response structure
# ---------------------------------------------------------------------------

def test_ai_tools_response_top_level_keys():
    """Response must include all required top-level keys."""
    result = ai_tools()
    required_keys = {
        "provider", "version", "tools", "workflows", "pricing", "auth", "notes",
        # onboarding guidance fields
        "discovery_entrypoints", "recommended_first_call", "quickstart",
        "recommended_first_workflows", "agent_onboarding_notes",
    }
    assert required_keys.issubset(result.keys()), (
        f"Missing keys: {required_keys - result.keys()}"
    )


def test_ai_tools_provider_and_version():
    result = ai_tools()
    assert result["provider"] == "stocktrends"
    assert result["version"] == "v1"


def test_ai_tools_notes_is_non_empty_list():
    result = ai_tools()
    assert isinstance(result["notes"], list)
    assert len(result["notes"]) > 0


def test_ai_tools_notes_prioritize_tools_manifest():
    result = ai_tools()
    assert result["notes"][0].startswith("Start with /v1/ai/tools")


def test_ai_context_discovery_entrypoints_prioritize_ai_tools():
    result = ai_context()
    assert result["discovery_entrypoints"] == {
        "primary_machine_readable": "/v1/ai/tools",
        "secondary_explanatory": "/v1/ai/context",
        "provider_agnostic_inference_contract": "/v1/meta/inference",
        "stim_provider_profile": "/v1/meta/stim",
        "docs": "/v1/docs",
        "openapi": "/v1/openapi.json",
    }


def test_ai_context_discovery_lists_put_ai_tools_first():
    result = ai_context()
    assert result["endpoint_groups"]["discovery"][0] == "/v1/ai/tools"
    assert result["access_model"]["public_discovery"][0] == "/v1/ai/tools"
    assert result["recommended_first_flows"]["agent"][:2] == [
        "/v1/ai/tools",
        "/v1/ai/context",
    ]


def test_ai_context_usage_guidance_references_primary_tools_manifest():
    result = ai_context()
    assert result["usage_guidance"][0].startswith("Start with /v1/ai/tools")
    assert "/v1/ai/context" in result["usage_guidance"][1]


def test_ai_context_tools_manifest_points_to_primary_entrypoint():
    result = ai_context()
    assert result["tools_manifest"] == "https://api.stocktrends.com/v1/ai/tools"


# ---------------------------------------------------------------------------
# 4. Tool definitions
# ---------------------------------------------------------------------------

REQUIRED_TOOL_FIELDS = {
    "name", "title", "description", "endpoint", "method",
    "category", "auth_required", "metered", "pricing_rule_id", "access_type",
    "requires_payment",
    "supported_rails", "input_schema", "input_location", "output_summary",
    "parameter_source",
    "stc_cost", "estimated_usd_cost", "pricing_note", "pricing",
}


def test_each_tool_has_required_fields():
    result = ai_tools()
    for tool in result["tools"]:
        missing = REQUIRED_TOOL_FIELDS - tool.keys()
        assert not missing, f"Tool '{tool.get('name')}' missing fields: {missing}"


def test_tool_names_are_unique():
    result = ai_tools()
    names = [t["name"] for t in result["tools"]]
    assert len(names) == len(set(names)), "Duplicate tool names found"


def test_tool_endpoints_are_unique():
    """Each (endpoint, method) pair must appear exactly once."""
    result = ai_tools()
    pairs = [(t["endpoint"], t["method"]) for t in result["tools"]]
    assert len(pairs) == len(set(pairs)), "Duplicate (endpoint, method) pairs found"


def test_required_tools_present():
    """Minimum required tools from the task spec must be present."""
    result = ai_tools()
    endpoints = {(t["endpoint"], t["method"]) for t in result["tools"]}
    required = {
        ("/v1/ai/context", "GET"),
        ("/v1/indicators/latest", "GET"),
        ("/v1/indicators/history", "GET"),
        ("/v1/decision/evaluate-symbol", "POST"),
        ("/v1/workflows", "GET"),
        ("/v1/cost-estimate", "GET"),
        ("/v1/leadership/definitions", "GET"),
        ("/v1/leadership/summary/latest", "GET"),
        ("/v1/leadership/rotation/history", "GET"),
    }
    missing = required - endpoints
    assert not missing, f"Required tools missing: {missing}"


def test_metered_endpoint_policy_tools_have_pricing_rule_id():
    """
    Tools with an exact endpoint_payment_policy must declare a pricing_rule_id.
    Agent-pay routes (stim, indicators, prices, selections, stwr, breadth) all have
    explicit per-endpoint policies now and therefore carry stable pricing_rule_ids.
    """
    result = ai_tools()
    for tool in result["tools"]:
        if not tool["metered"]:
            continue
        # Free-metered paths carry a stable rule ID.
        if is_free_metered_path(tool["endpoint"]):
            assert tool["pricing_rule_id"] is not None, (
                f"Free-metered tool '{tool['name']}' has no pricing_rule_id"
            )
            continue
        assert tool["pricing_rule_id"] is not None, (
            f"Metered tool '{tool['name']}' (with endpoint policy) has no pricing_rule_id"
        )


def test_stim_tools_metered_and_have_pricing_rule_id():
    """
    STIM tools must be metered=True and carry a stable per-endpoint pricing_rule_id.
    /v1/stim/latest → stim_latest_paid
    /v1/stim/history → stim_history_paid
    (Before per-endpoint policies were added, stim tools had pricing_rule_id=None
    because the fallback stim_paid wildcard rule was used.  Explicit policies fix this.)
    """
    result = ai_tools()
    stim_tools = [t for t in result["tools"] if t["endpoint"].startswith("/v1/stim")]
    assert stim_tools, "No STIM tools found in manifest"
    for tool in stim_tools:
        assert tool["metered"] is True, (
            f"STIM tool '{tool['name']}' must be metered=True"
        )
        assert tool["pricing_rule_id"] is not None, (
            f"STIM tool '{tool['name']}' must have a pricing_rule_id (per-endpoint active DB rule)"
        )


def test_stim_tools_supported_rails_match_policy():
    """
    STIM tool supported_rails must be derived from runtime policy, not hardcoded.
    Expected: ["subscription"] + get_agent_pay_auth_bypass_methods(path, method).
    This test would catch a hardcoded list diverging from the config.
    """
    result = ai_tools()
    stim_tools = [t for t in result["tools"] if t["endpoint"].startswith("/v1/stim")]
    assert stim_tools, "No STIM tools found in manifest"
    for tool in stim_tools:
        agent_rails = list(get_agent_pay_auth_bypass_methods(tool["endpoint"], tool["method"]))
        expected_rails = ["subscription"] + agent_rails
        assert tool["supported_rails"] == expected_rails, (
            f"STIM tool '{tool['name']}' supported_rails={tool['supported_rails']!r} "
            f"expected {expected_rails!r} (derived from runtime policy)"
        )


def test_registry_overrides_shadowed_tool_templates():
    """
    Older hand-authored templates for these endpoints were thinner than the
    central registry. The manifest should now expose the registry input schema.
    """
    result = ai_tools()
    tools_by_endpoint = {(tool["endpoint"], tool["method"]): tool for tool in result["tools"]}
    shadowed = {
        ("/v1/agent/screener/top", "GET"),
        ("/v1/portfolio/construct", "POST"),
        ("/v1/portfolio/evaluate", "POST"),
        ("/v1/portfolio/compare", "POST"),
        ("/v1/stim/latest", "GET"),
        ("/v1/stim/history", "GET"),
    }

    for endpoint, method in shadowed:
        tool = tools_by_endpoint[(endpoint, method)]
        assert tool["input_schema"] == build_input_schema(endpoint)
        assert tool["safe_example_request"]["path"] == endpoint
        assert tool["workflow_role"]
        assert "related_endpoints" in tool


def test_get_tools_expose_query_parameters_not_body_parameters():
    """Observed Bazaar issue: GET tool params must be query params, not body params."""
    result = ai_tools()
    tools_by_endpoint = {(tool["endpoint"], tool["method"]): tool for tool in result["tools"]}
    endpoints = [
        "/v1/stim/latest",
        "/v1/stim/history",
        "/v1/indicators/latest",
        "/v1/indicators/history",
        "/v1/prices/latest",
        "/v1/prices/history",
        "/v1/stwr/reports/latest",
        "/v1/stwr/reports/history",
    ]

    for endpoint in endpoints:
        tool = tools_by_endpoint[(endpoint, "GET")]
        assert tool["input_location"] == "query"
        assert tool["parameter_source"] == "query"
        assert tool["input_schema"]["x-stocktrends-input-location"] == "query"
        assert tool["input_schema"]["x-stocktrends-parameter-source"] == "query"
        assert "request_body_schema" not in tool
        assert "json" not in tool["safe_example_request"]
        assert "query" in tool["safe_example_request"]
        for inputs_field in ("required_inputs", "optional_inputs"):
            for name, meta in tool.get(inputs_field, {}).items():
                assert meta["input_location"] == "query", f"{endpoint} {name} input_location is not query"
                assert meta["parameter_source"] == "query", f"{endpoint} {name} parameter_source is not query"
        for param in tool.get("parameters", []):
            assert param["in"] == "query", f"{endpoint} parameter {param['name']} is not query"
            assert param["input_location"] == "query", f"{endpoint} parameter {param['name']} missing input_location=query"
            assert param["parameter_source"] == "query", f"{endpoint} parameter {param['name']} missing parameter_source=query"


def test_target_get_tools_expose_expected_query_parameter_names():
    result = ai_tools()
    tools_by_endpoint = {(tool["endpoint"], tool["method"]): tool for tool in result["tools"]}
    expected = {
        "/v1/stim/latest": "symbol_exchange",
        "/v1/stim/history": "symbol_exchange",
        "/v1/indicators/latest": "symbol_exchange",
        "/v1/indicators/history": "symbol_exchange",
        "/v1/prices/latest": "symbol_exchange",
        "/v1/prices/history": "symbol_exchange",
        "/v1/stwr/reports/latest": "rpt",
        "/v1/stwr/reports/history": "rpt",
    }

    for endpoint, parameter_name in expected.items():
        tool = tools_by_endpoint[(endpoint, "GET")]
        params = {param["name"]: param for param in tool["parameters"]}
        assert parameter_name in params
        assert params[parameter_name]["in"] == "query"
        assert params[parameter_name]["parameter_source"] == "query"


def test_portfolio_compare_safe_example_matches_api_shape():
    result = ai_tools()
    tool = next(t for t in result["tools"] if t["endpoint"] == "/v1/portfolio/compare")
    payload = tool["safe_example_request"]["json"]
    assert payload == {
        "left": [{"symbol_exchange": "IBM-N", "weight": 1.0}],
        "right": [{"symbol_exchange": "MSFT-Q", "weight": 1.0}],
    }
    assert isinstance(payload["left"], list)
    assert isinstance(payload["right"], list)
    assert "positions" not in payload["left"][0]


def test_non_metered_tools_have_no_pricing_rule_id():
    """Non-metered tools must not declare a pricing_rule_id."""
    result = ai_tools()
    for tool in result["tools"]:
        if not tool["metered"]:
            assert tool["pricing_rule_id"] is None, (
                f"Non-metered tool '{tool['name']}' unexpectedly has pricing_rule_id"
            )


def test_auth_required_tools_have_supported_rails():
    """Auth-required tools must declare at least one supported rail."""
    result = ai_tools()
    for tool in result["tools"]:
        if tool["auth_required"]:
            assert tool["supported_rails"], (
                f"Auth-required tool '{tool['name']}' has no supported_rails"
            )


def test_tool_endpoints_start_with_v1():
    """All tool endpoints must be under /v1/."""
    result = ai_tools()
    for tool in result["tools"]:
        assert tool["endpoint"].startswith("/v1/"), (
            f"Tool '{tool['name']}' endpoint '{tool['endpoint']}' not under /v1/"
        )


def test_tool_pricing_fields_are_manifest_metadata_not_hardcoded_price_usd():
    """Tools may expose estimated_usd_cost, but not legacy hardcoded price_usd fields."""
    result = ai_tools()
    import json

    serialized = json.dumps(result)
    assert "price_usd" not in serialized
    assert '"usd_cost"' not in serialized
    for tool in result["tools"]:
        assert "estimated_usd_cost" in tool
        assert tool["pricing"]["estimated_usd_cost"] == tool["estimated_usd_cost"]


def test_tools_built_fresh_per_request():
    """
    ai_tools() must build the tools list at request time, not return a
    frozen module-level constant.  Two calls must return equal lists that
    are not the same object — proving runtime policy is re-evaluated
    on each invocation.
    """
    first = ai_tools()["tools"]
    second = ai_tools()["tools"]
    assert first == second, "Two calls to ai_tools() returned different tool lists"
    assert first is not second, (
        "ai_tools() returned the same list object on successive calls — "
        "tools appear to be a frozen module-level constant rather than "
        "being built fresh per request"
    )


# ---------------------------------------------------------------------------
# 5. Workflows section
# ---------------------------------------------------------------------------

def test_workflows_count_matches_registry():
    """Workflow count must match WORKFLOW_REGISTRY."""
    result = ai_tools()
    assert len(result["workflows"]) == len(WORKFLOW_REGISTRY)


def test_workflow_ids_match_registry():
    result = ai_tools()
    manifest_ids = {w["workflow_id"] for w in result["workflows"]}
    registry_ids = {w["workflow_id"] for w in WORKFLOW_REGISTRY}
    assert manifest_ids == registry_ids


def test_workflow_summary_required_fields():
    """Each workflow summary must include required fields."""
    required = {
        "workflow_id", "name", "description", "tags", "supported_rails",
        "step_count", "pricing_rule_ids", "note",
    }
    result = ai_tools()
    for wf in result["workflows"]:
        missing = required - wf.keys()
        assert not missing, f"Workflow '{wf.get('workflow_id')}' missing fields: {missing}"


def test_workflow_step_count_is_accurate():
    """step_count must equal actual number of steps in the registry."""
    result = ai_tools()
    for manifest_wf in result["workflows"]:
        registry_wf = next(
            w for w in WORKFLOW_REGISTRY if w["workflow_id"] == manifest_wf["workflow_id"]
        )
        assert manifest_wf["step_count"] == len(registry_wf["steps"])


def test_workflow_pricing_rule_ids_accurate():
    """pricing_rule_ids must list the actual rule IDs from the registry."""
    result = ai_tools()
    for manifest_wf in result["workflows"]:
        registry_wf = next(
            w for w in WORKFLOW_REGISTRY if w["workflow_id"] == manifest_wf["workflow_id"]
        )
        expected = [s["pricing_rule_id"] for s in registry_wf["steps"] if s.get("pricing_rule_id")]
        assert manifest_wf["pricing_rule_ids"] == expected


def test_workflow_note_references_live_endpoint():
    """Workflow summary note must point agents to GET /v1/workflows for live costs."""
    result = ai_tools()
    for wf in result["workflows"]:
        assert "/v1/workflows" in wf["note"]


# ---------------------------------------------------------------------------
# agent_conversion_path block
# ---------------------------------------------------------------------------

def test_ai_tools_includes_agent_conversion_path():
    """Response must include agent_conversion_path top-level key."""
    result = ai_tools()
    assert "agent_conversion_path" in result, (
        "agent_conversion_path key missing from /v1/ai/tools response"
    )


def test_ai_tools_agent_conversion_path_proof_endpoint():
    """proof_endpoint must point to the free proof-of-value endpoint."""
    result = ai_tools()
    acp = result["agent_conversion_path"]
    assert acp.get("proof_endpoint") == "/v1/ai/proof/market-edge"


def test_ai_tools_agent_conversion_path_payment_methods():
    """payment_methods_supported must include all three active rails."""
    result = ai_tools()
    methods = set(result["agent_conversion_path"]["payment_methods_supported"])
    assert methods == {"subscription", "x402", "mpp"}


def test_ai_tools_agent_conversion_path_has_conversion_steps():
    """conversion_steps must be a non-empty ordered list."""
    result = ai_tools()
    steps = result["agent_conversion_path"]["conversion_steps"]
    assert isinstance(steps, list)
    assert len(steps) >= 1


def test_ai_tools_agent_conversion_path_steps_include_proof_endpoint():
    """At least one conversion step must reference the proof endpoint."""
    result = ai_tools()
    steps = result["agent_conversion_path"]["conversion_steps"]
    calls = [s.get("call", "") for s in steps]
    assert any("/v1/ai/proof/market-edge" in c for c in calls), (
        "No conversion step references /v1/ai/proof/market-edge"
    )


def test_ai_tools_agent_conversion_path_on_payment_required_present():
    """on_payment_required must explain the 402 flow."""
    result = ai_tools()
    note = result["agent_conversion_path"].get("on_payment_required", "")
    assert "402" in note or "payment" in note.lower(), (
        "on_payment_required must describe the 402 payment flow"
    )


# ---------------------------------------------------------------------------
# 6. Pricing section
# ---------------------------------------------------------------------------

def test_pricing_unit_is_stc():
    result = ai_tools()
    assert result["pricing"]["unit"] == "STC"


def test_pricing_catalog_endpoint_present():
    result = ai_tools()
    assert result["pricing"]["catalog_endpoint"] == "/v1/pricing/catalog"


def test_pricing_cost_estimate_endpoint_present():
    result = ai_tools()
    assert result["pricing"]["cost_estimate_endpoint"] == "/v1/cost-estimate"


def test_cost_estimate_tool_mentions_mpp_rail():
    result = ai_tools()
    tool = next(t for t in result["tools"] if t["endpoint"] == "/v1/cost-estimate")
    rail_enum = tool["input_schema"]["properties"]["rail_preference"]["enum"]
    assert "mpp" in rail_enum


def test_cost_estimate_tool_exposes_workflow_id_examples():
    result = ai_tools()
    tool = next(t for t in result["tools"] if t["endpoint"] == "/v1/cost-estimate")
    workflow_id_schema = tool["input_schema"]["properties"]["workflow_id"]
    expected = {
        "regime_analysis",
        "symbol_decision",
        "stim_forecast_review",
        "portfolio_build",
        "portfolio_compare_review",
    }
    assert set(workflow_id_schema["enum"]) == expected
    assert workflow_id_schema["example"] == "portfolio_build"
    assert tool["safe_example_request"]["query"]["workflow_id"] == "portfolio_build"


def test_stim_forecast_review_workflow_exposes_interpretation_sequence():
    result = ai_tools()
    workflow = next(w for w in result["workflows"] if w["workflow_id"] == "stim_forecast_review")

    assert workflow["interpretation_guidance"]
    assert "/v1/meta/inference" in workflow["interpretation_guidance"]
    assert "/v1/meta/stim" in workflow["interpretation_guidance"]
    assert "base_period_mean_returns_pct" in workflow["interpretation_guidance"]
    assert workflow["required_interpretation_steps"]

    registry_workflow = next(w for w in WORKFLOW_REGISTRY if w["workflow_id"] == "stim_forecast_review")
    endpoints = [step["endpoint"] for step in registry_workflow["steps"]]
    assert endpoints.index("GET /v1/meta/inference") < endpoints.index("GET /v1/meta/stim")
    assert endpoints.index("GET /v1/meta/stim") < endpoints.index("GET /v1/stim/latest")


def test_stim_tools_expose_machine_readable_interpretation_guidance():
    result = ai_tools()
    tools = {tool["endpoint"]: tool for tool in result["tools"]}

    for endpoint in ("/v1/stim/latest", "/v1/stim/history"):
        tool = tools[endpoint]
        dependency = tool["interpretation_dependency"]
        guidance = tool["interpretation_guidance"]
        steps = tool["required_interpretation_steps"]

        assert dependency["endpoint"] == "/v1/meta/stim"
        assert dependency["inference_contract_endpoint"] == "/v1/meta/inference"
        assert dependency["required_before_interpretation"] is True
        assert guidance["inference_contract_endpoint"] == "/v1/meta/inference"
        assert guidance["inference_provider"]["provider_id"] == "stim"
        assert guidance["inference_provider"]["not_final_intelligence_layer"] is True
        assert "base_period_mean_returns_pct" in guidance
        assert guidance["calculation"]["delta_vs_base"] == "stim_mean - base_mean"
        assert guidance["calculation"]["probability_outperform"] == "1 - normal_cdf(z)"
        assert guidance["stim_select_style_logic"]["prob13wk_minimum"] == 0.55
        assert any("/v1/meta/inference" in step for step in steps)
        assert any("/v1/meta/stim" in step for step in steps)
        assert "is_stale" in " ".join(guidance["interpretation_rules"])


def test_paid_tools_include_manifest_pricing_fields(monkeypatch):
    monkeypatch.setattr(
        ai_router,
        "_fetch_pricing_cost_map",
        lambda: {
            "stim_latest_paid": Decimal("0.25"),
            "portfolio_compare": Decimal("1.50"),
            "breadth_sector_latest_paid": Decimal("0.100000"),
        },
    )
    result = ai_tools()
    tools = {tool["endpoint"]: tool for tool in result["tools"]}

    for endpoint, expected_rule, expected_cost in (
        ("/v1/stim/latest", "stim_latest_paid", 0.25),
        ("/v1/portfolio/compare", "portfolio_compare", 1.5),
        ("/v1/breadth/sector/latest", "breadth_sector_latest_paid", 0.1),
    ):
        tool = tools[endpoint]
        assert tool["pricing_rule_id"] == expected_rule
        assert tool["stc_cost"] == expected_cost
        assert tool["estimated_usd_cost"] == expected_cost
        assert tool["pricing"]["cost_source"] == "/v1/pricing/catalog"
        assert set(tool["supported_rails"]) == {"subscription", "x402", "mpp"}
        assert "STC is the source of truth" in tool["pricing_note"]


def test_pricing_section_has_no_hardcoded_usd_amounts():
    """Pricing section must not hardcode USD cost amounts."""
    import json
    result = ai_tools()
    pricing_str = json.dumps(result["pricing"])
    assert "cost_usd" not in pricing_str
    assert "price_usd" not in pricing_str


# ---------------------------------------------------------------------------
# 7. Auth section
# ---------------------------------------------------------------------------

def test_auth_section_has_modes():
    result = ai_tools()
    modes = result["auth"]["modes"]
    assert isinstance(modes, list)
    assert len(modes) >= 2  # subscription + x402 minimum


def test_auth_modes_include_subscription_and_x402():
    result = ai_tools()
    mode_names = {m["mode"] for m in result["auth"]["modes"]}
    assert "subscription" in mode_names
    assert "x402" in mode_names


def test_auth_section_has_agent_identity_headers():
    result = ai_tools()
    agent_headers = result["auth"]["agent_identity_headers"]
    assert "X-StockTrends-Agent-Id" in agent_headers


# ---------------------------------------------------------------------------
# 8. _build_workflow_summary helper
# ---------------------------------------------------------------------------

def test_build_workflow_summary_shape():
    sample = WORKFLOW_REGISTRY[0]
    summary = _build_workflow_summary(sample)
    assert summary["workflow_id"] == sample["workflow_id"]
    assert summary["step_count"] == len(sample["steps"])
    assert summary["pricing_rule_ids"] == [s["pricing_rule_id"] for s in sample["steps"]]


def test_build_workflow_summary_does_not_include_live_costs():
    """The simplified summary must NOT attempt to resolve live STC costs."""
    sample = WORKFLOW_REGISTRY[0]
    summary = _build_workflow_summary(sample)
    assert "stc_cost" not in summary
    assert "total_stc_cost" not in summary


# ---------------------------------------------------------------------------
# 9. Runtime metadata regression tests
# ---------------------------------------------------------------------------

def test_regression_ai_context_tool():
    """
    /v1/ai/context is a free-metered path.
    Manifest must reflect: auth_required=False, metered=True,
    pricing_rule_id="default_free_metered".
    """
    result = ai_tools()
    tool = next(t for t in result["tools"] if t["endpoint"] == "/v1/ai/context")
    assert tool["auth_required"] is False, "/v1/ai/context must be auth_required=False"
    assert tool["metered"] is True, "/v1/ai/context must be metered=True (free-metered)"
    assert tool["pricing_rule_id"] == "default_free_metered", (
        f"/v1/ai/context must have pricing_rule_id='default_free_metered', got {tool['pricing_rule_id']!r}"
    )


def test_regression_pricing_catalog_tool():
    """
    /v1/pricing/catalog is public under current ApiKeyMiddleware behavior, but
    classifier metadata still reports it as metered with a subscription pricing rule.
    This PR only corrects discovery metadata to match current public behavior.
    """
    result = ai_tools()
    tool = next(t for t in result["tools"] if t["endpoint"] == "/v1/pricing/catalog")
    assert tool["auth_required"] is False, "/v1/pricing/catalog is public under current behavior"
    assert tool["metered"] is True, "/v1/pricing/catalog must be metered=True"
    assert tool["pricing_rule_id"] is not None, (
        "/v1/pricing/catalog must keep its classifier-derived pricing_rule_id"
    )


def test_regression_stim_tools():
    """
    /v1/stim/* paths have explicit per-endpoint payment policies.
    Manifest must reflect: auth_required=True, metered=True,
    pricing_rule_id is not None (stable per-endpoint active DB rule).
    Previously stim tools used pricing_rule_id=None due to a prefix-based fallback
    that mapped to the now-inactive 'stim_paid' wildcard rule; explicit policies fix this.
    """
    result = ai_tools()
    stim_tools = [t for t in result["tools"] if t["endpoint"].startswith("/v1/stim")]
    assert stim_tools, "No STIM tools found in manifest"
    for tool in stim_tools:
        assert tool["auth_required"] is True, (
            f"STIM tool '{tool['name']}' must be auth_required=True"
        )
        assert tool["metered"] is True, (
            f"STIM tool '{tool['name']}' must be metered=True"
        )
        assert tool["pricing_rule_id"] is not None, (
            f"STIM tool '{tool['name']}' must have a pricing_rule_id (per-endpoint active DB rule)"
        )


def test_regression_public_manifest_tools_not_auth_required():
    """
    Tools whose endpoints are in _MANIFEST_PUBLIC_PATHS must be auth_required=False.
    """
    result = ai_tools()
    for tool in result["tools"]:
        if tool["endpoint"] in _MANIFEST_PUBLIC_PATHS:
            assert tool["auth_required"] is False, (
                f"Tool '{tool['name']}' ({tool['endpoint']}) is a public path "
                f"but manifest has auth_required=True"
            )


# ---------------------------------------------------------------------------
# 10. Runtime cross-checks against classifier
# ---------------------------------------------------------------------------

def test_tool_metered_matches_classifier():
    """
    For every tool, manifest metered flag must match classify_request()
    with has_paid_auth=True (simulating an authenticated pro caller).
    This is the same probe used by _access_metadata() at module load.
    """
    result = ai_tools()
    for tool in result["tools"]:
        path = tool["endpoint"]
        method = tool["method"]
        decision = classify_request(
            path=path,
            has_paid_auth=True,
            payment_method_header=None,
            plan_code="pro",
            agent_identifier=None,
            method=method,
        )
        expected_metered = bool(decision.is_metered)
        assert tool["metered"] == expected_metered, (
            f"Tool '{tool['name']}' ({path}): manifest metered={tool['metered']!r} "
            f"but classifier returned is_metered={decision.is_metered}"
        )


def test_tool_auth_required_matches_manifest_public_paths():
    """
    Tools under _MANIFEST_PUBLIC_PATHS must have auth_required=False.
    Tools NOT in _MANIFEST_PUBLIC_PATHS must have auth_required=True.
    This verifies _access_metadata() logic is consistent.
    """
    result = ai_tools()
    for tool in result["tools"]:
        expected_auth_required = tool["endpoint"] not in _MANIFEST_PUBLIC_PATHS
        assert tool["auth_required"] == expected_auth_required, (
            f"Tool '{tool['name']}' ({tool['endpoint']}): "
            f"auth_required={tool['auth_required']!r} but expected {expected_auth_required!r} "
            f"based on _MANIFEST_PUBLIC_PATHS"
        )


# ---------------------------------------------------------------------------
# 11. Sync checks: manifest constants vs middleware/classifier
# ---------------------------------------------------------------------------

def test_manifest_public_paths_subset_of_middleware():
    """
    Every path in _MANIFEST_PUBLIC_PATHS must also appear in
    ApiKeyMiddleware.public_paths. If a path is public in the manifest
    but not in the middleware, agents will receive 401s.
    """
    import pathlib
    source = pathlib.Path("middleware/api_key.py").read_text(encoding="utf-8")
    for path in _MANIFEST_PUBLIC_PATHS:
        assert f'"{path}"' in source, (
            f"_MANIFEST_PUBLIC_PATHS path '{path}' not found in "
            f"ApiKeyMiddleware.public_paths (middleware/api_key.py)"
        )


def test_manifest_public_paths_subset_of_non_metered_paths():
    """
    Every path in _MANIFEST_PUBLIC_PATHS must also appear in NON_METERED_PATHS
    (or is a free-metered path), except documented planning metadata paths whose
    current public behavior is intentionally left unchanged in this PR.
    """
    for path in _MANIFEST_PUBLIC_PATHS:
        is_non_metered = path in NON_METERED_PATHS
        is_free_metered = is_free_metered_path(path)
        is_current_public_metered_exception = path in _PUBLIC_METERED_PLANNING_EXCEPTIONS
        assert is_non_metered or is_free_metered or is_current_public_metered_exception, (
            f"_MANIFEST_PUBLIC_PATHS path '{path}' is neither in NON_METERED_PATHS "
            f"nor a free_metered_path — public callers would be metered or denied"
        )


# ---------------------------------------------------------------------------
# 12. Onboarding / guidance fields
# ---------------------------------------------------------------------------

def test_discovery_entrypoints_in_ai_tools():
    """ai_tools must expose discovery_entrypoints mirroring ai_context."""
    result = ai_tools()
    de = result["discovery_entrypoints"]
    assert de["primary_machine_readable"] == "/v1/ai/tools"
    assert de["secondary_explanatory"] == "/v1/ai/context"
    assert de["docs"] == "/v1/docs"
    assert de["openapi"] == "/v1/openapi.json"


def test_discovery_entrypoints_consistent_between_tools_and_context():
    """discovery_entrypoints must be identical in ai_tools and ai_context."""
    tools_result = ai_tools()
    context_result = ai_context()
    assert tools_result["discovery_entrypoints"] == context_result["discovery_entrypoints"]


def test_recommended_first_call_structure():
    result = ai_tools()
    rfc = result["recommended_first_call"]
    assert rfc["endpoint"].startswith("/v1/")
    assert rfc["method"] in {"GET", "POST"}
    assert isinstance(rfc["auth_required"], bool)
    assert isinstance(rfc["supported_rails"], list)
    assert len(rfc["supported_rails"]) > 0
    assert isinstance(rfc["expected_flow"], list)
    assert len(rfc["expected_flow"]) > 0
    assert "reason" in rfc


def test_recommended_first_call_endpoint_exists_in_tools():
    """recommended_first_call.endpoint must reference a real tool in the manifest."""
    result = ai_tools()
    rfc_endpoint = result["recommended_first_call"]["endpoint"]
    tool_endpoints = {t["endpoint"] for t in result["tools"]}
    assert rfc_endpoint in tool_endpoints, (
        f"recommended_first_call.endpoint '{rfc_endpoint}' not found in tools list"
    )


def test_quickstart_is_ordered_steps():
    result = ai_tools()
    qs = result["quickstart"]
    assert isinstance(qs, list)
    assert len(qs) >= 3
    steps = [s["step"] for s in qs]
    assert steps == sorted(steps), "quickstart steps must be in ascending order"
    for step in qs:
        assert "step" in step
        assert "action" in step
        assert "path" in step
        assert step["path"].startswith("/v1/")


def test_recommended_first_workflows_subset_of_registry():
    """recommended_first_workflows must only reference workflow_ids from WORKFLOW_REGISTRY."""
    result = ai_tools()
    registry_ids = {w["workflow_id"] for w in WORKFLOW_REGISTRY}
    for wf in result["recommended_first_workflows"]:
        assert wf["workflow_id"] in registry_ids, (
            f"recommended_first_workflows references unknown workflow_id '{wf['workflow_id']}'"
        )


def test_recommended_first_workflows_non_empty():
    result = ai_tools()
    assert isinstance(result["recommended_first_workflows"], list)
    assert len(result["recommended_first_workflows"]) >= 1


def test_agent_onboarding_notes_non_empty_list():
    result = ai_tools()
    notes = result["agent_onboarding_notes"]
    assert isinstance(notes, list)
    assert len(notes) > 0


def test_agent_onboarding_notes_no_hardcode_instruction():
    """Must instruct agents not to hardcode STC costs."""
    result = ai_tools()
    combined = " ".join(result["agent_onboarding_notes"]).lower()
    assert "hardcode" in combined or "do not hardcode" in combined


def test_agent_onboarding_notes_references_pricing_catalog():
    result = ai_tools()
    combined = " ".join(result["agent_onboarding_notes"])
    assert "/v1/pricing/catalog" in combined


def test_ai_tools_links_planning_surfaces_and_x402_preview():
    result = ai_tools()
    serialized = str(result)
    for expected in (
        "/v1/workflows",
        "/v1/pricing/catalog",
        "/v1/pricing",
        "stocktrends_preview",
    ):
        assert expected in serialized


def test_planning_helpers_are_promoted_in_tools_and_context():
    result = ai_tools()
    tool_endpoints = {tool["endpoint"] for tool in result["tools"]}
    context = ai_context()
    context_helpers = set(context["endpoint_groups"]["planning_helpers"])
    expected = {
        "/v1/openapi.json",
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
        "/v1/ai/proof/market-edge",
    }
    assert expected.issubset(tool_endpoints)
    assert (expected - {"/v1/openapi.json"}).issubset(context_helpers)


def test_service_positioning_present_in_ai_surfaces():
    result = ai_tools()
    context = ai_context()
    expected = "Autonomous portfolio intelligence API for AI agents"
    assert expected in result["service_description"]
    assert expected in context["service_description"]
    assert "Weekly structured market intelligence dataset" in context["description"]


def test_mpp_rail_metadata_still_present_for_paid_tools():
    result = ai_tools()
    for endpoint in (
        "/v1/stim/latest",
        "/v1/portfolio/compare",
        "/v1/agent/screener/top",
        "/v1/leadership/summary/latest",
        "/v1/leadership/rotation/history",
        "/v1/intelligence/guidance/latest",
        "/v1/intelligence/research/latest",
    ):
        tool = next(t for t in result["tools"] if t["endpoint"] == endpoint)
        assert "mpp" in tool["supported_rails"]
        assert "mpp" in tool["pricing"]["supported_rails"]


def test_public_planning_helper_tools_are_free_and_public():
    result = ai_tools()
    tools = {tool["endpoint"]: tool for tool in result["tools"]}
    public_helpers = {
        "/v1/cost-estimate",
        "/v1/instruments/lookup",
        "/v1/instruments/resolve",
        "/v1/stwr/reports/catalog",
        "/v1/meta/indicators",
        "/v1/meta/inference",
        "/v1/meta/stim",
        "/v1/meta/stwr",
        "/v1/leadership/definitions",
    }

    for endpoint in public_helpers:
        tool = tools[endpoint]
        assert tool["auth_required"] is False
        assert tool["metered"] is False
        assert tool["access_type"] == "free"
        assert tool["requires_payment"] is False
        assert tool["supported_rails"] == []


def test_leadership_paid_tools_are_x402_mpp_enabled():
    result = ai_tools()
    tools = {tool["endpoint"]: tool for tool in result["tools"]}
    expected = {
        "/v1/leadership/summary/latest": "leadership_summary_latest_paid",
        "/v1/leadership/rotation/history": "leadership_rotation_history_paid",
    }

    for endpoint, rule_id in expected.items():
        tool = tools[endpoint]
        assert tool["auth_required"] is True
        assert tool["metered"] is True
        assert tool["access_type"] == "paid"
        assert tool["requires_payment"] is True
        assert tool["pricing_rule_id"] == rule_id
        assert tool["supported_rails"] == ["subscription", "x402", "mpp"]
        assert tool["pricing"]["supported_rails"] == ["subscription", "x402", "mpp"]


def test_paid_intelligence_tools_are_x402_mpp_enabled(monkeypatch):
    monkeypatch.setattr(
        ai_router,
        "_fetch_pricing_cost_map",
        lambda: {
            "intelligence_guidance_latest": Decimal("0.25"),
            "intelligence_guidance_by_id": Decimal("0.25"),
            "intelligence_research_latest": Decimal("0.50"),
            "intelligence_research_by_id": Decimal("0.50"),
        },
    )

    result = ai_tools()
    tools = {tool["endpoint"]: tool for tool in result["tools"]}
    expected = {
        "/v1/intelligence/guidance/latest": ("intelligence_guidance_latest", 0.25),
        "/v1/intelligence/guidance/{artifact_id}": ("intelligence_guidance_by_id", 0.25),
        "/v1/intelligence/research/latest": ("intelligence_research_latest", 0.5),
        "/v1/intelligence/research/{artifact_id}": ("intelligence_research_by_id", 0.5),
    }

    for endpoint, (rule_id, stc_cost) in expected.items():
        tool = tools[endpoint]
        assert tool["auth_required"] is True
        assert tool["metered"] is True
        assert tool["access_type"] == "paid"
        assert tool["requires_payment"] is True
        assert tool["pricing_rule_id"] == rule_id
        assert tool["supported_rails"] == ["subscription", "x402", "mpp"]
        assert tool["pricing"]["supported_rails"] == ["subscription", "x402", "mpp"]
        assert tool["stc_cost"] == stc_cost
        assert tool["pricing"]["stc_cost"] == stc_cost


def test_breadth_sector_latest_tool_is_paid_x402_mpp_enabled(monkeypatch):
    monkeypatch.setattr(
        ai_router,
        "_fetch_pricing_cost_map",
        lambda: {"breadth_sector_latest_paid": Decimal("0.100000")},
    )

    result = ai_tools()
    tool = next(t for t in result["tools"] if t["endpoint"] == "/v1/breadth/sector/latest")

    assert tool["auth_required"] is True
    assert tool["metered"] is True
    assert tool["access_type"] == "paid"
    assert tool["requires_payment"] is True
    assert tool["pricing_rule_id"] == "breadth_sector_latest_paid"
    assert tool["supported_rails"] == ["subscription", "x402", "mpp"]
    assert tool["pricing"]["supported_rails"] == ["subscription", "x402", "mpp"]
    assert tool["stc_cost"] == 0.1
    assert tool["pricing"]["stc_cost"] == 0.1


def test_paid_leadership_not_in_public_allowlists():
    import pathlib
    import main as main_module

    source = pathlib.Path("middleware/api_key.py").read_text(encoding="utf-8")
    assert hasattr(main_module, "FREE_METERED_V1_PATHS")
    assert isinstance(main_module.FREE_METERED_V1_PATHS, set)

    paid_paths = {
        "/v1/leadership/summary/latest",
        "/v1/leadership/rotation/history",
    }

    for path in paid_paths:
        assert path not in _MANIFEST_PUBLIC_PATHS
        assert path not in NON_METERED_PATHS
        assert path not in main_module.FREE_METERED_V1_PATHS
        assert path.removeprefix("/v1") not in main_module.FREE_METERED_V1_PATHS
        assert f'"{path}"' not in source


def test_breadth_sector_latest_not_in_public_or_free_allowlists():
    import pathlib
    import main as main_module

    source = pathlib.Path("middleware/api_key.py").read_text(encoding="utf-8")
    path = "/v1/breadth/sector/latest"

    assert path not in _MANIFEST_PUBLIC_PATHS
    assert path not in NON_METERED_PATHS
    assert path not in main_module.FREE_METERED_V1_PATHS
    assert path.removeprefix("/v1") not in main_module.FREE_METERED_V1_PATHS
    assert f'"{path}"' not in source
    assert is_free_metered_path(path) is False


def test_paid_intelligence_not_in_public_or_free_allowlists():
    import pathlib
    import main as main_module

    source = pathlib.Path("middleware/api_key.py").read_text(encoding="utf-8")
    paid_paths = {
        "/v1/intelligence/guidance/latest",
        "/v1/intelligence/research/latest",
    }

    for path in paid_paths:
        assert path not in _MANIFEST_PUBLIC_PATHS
        assert path not in NON_METERED_PATHS
        assert path not in main_module.FREE_METERED_V1_PATHS
        assert path.removeprefix("/v1") not in main_module.FREE_METERED_V1_PATHS
        assert f'"{path}"' not in source
        assert is_free_metered_path(path) is False
