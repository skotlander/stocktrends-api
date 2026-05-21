"""
Tests for static/tools.json.

Verifies:
1. File is valid JSON
2. Required tools present: stim_latest, stim_history, ai_proof_market_edge
3. Nonexistent tool absent: stim_top (/stim/top does not exist as a route)
4. auth_required corrections: /pricing → false, /workflows → false
5. selections_latest present with correct path
6. selections_published_latest uses correct path (not /selections-published/latest)
7. No hardcoded 'STC per call' in tool descriptions
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_JSON = REPO_ROOT / "static" / "tools.json"
LLMS_TXT = REPO_ROOT / "static" / "llms.txt"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(TOOLS_JSON.read_text(encoding="utf-8"))


def _tool_by_name(manifest: dict, name: str) -> dict | None:
    return next((t for t in manifest["tools"] if t.get("name") == name), None)


def _tool_by_path(manifest: dict, path: str) -> dict | None:
    return next((t for t in manifest["tools"] if t.get("path") == path), None)


# ---------------------------------------------------------------------------
# 1. Valid JSON
# ---------------------------------------------------------------------------

def test_tools_json_is_valid_json():
    """static/tools.json must parse without errors."""
    json.loads(TOOLS_JSON.read_text(encoding="utf-8"))


def test_tools_json_has_tools_list(manifest):
    assert isinstance(manifest.get("tools"), list)
    assert len(manifest["tools"]) > 0, "tools list must not be empty"


# ---------------------------------------------------------------------------
# 2. Required tools present
# ---------------------------------------------------------------------------

def test_stim_latest_present(manifest):
    assert _tool_by_name(manifest, "stim_latest") is not None, \
        "stim_latest must be present in static/tools.json"


def test_stim_history_present(manifest):
    assert _tool_by_name(manifest, "stim_history") is not None, \
        "stim_history must be present in static/tools.json"


def test_ai_proof_market_edge_present(manifest):
    tool = _tool_by_name(manifest, "ai_proof_market_edge")
    assert tool is not None, "ai_proof_market_edge must be present in static/tools.json"
    assert tool.get("auth_required") is False, \
        "ai_proof_market_edge must have auth_required: false"


# ---------------------------------------------------------------------------
# 3. Nonexistent tool absent
# ---------------------------------------------------------------------------

def test_stim_top_absent_by_name(manifest):
    """/stim/top does not exist as a route — stim_top must not appear in tools.json."""
    assert _tool_by_name(manifest, "stim_top") is None, \
        "stim_top must not appear in tools.json (endpoint /stim/top does not exist)"


def test_stim_top_absent_by_path(manifest):
    """/stim/top path must not appear in any tool entry."""
    assert _tool_by_path(manifest, "/stim/top") is None, \
        "/stim/top must not appear in tools.json (endpoint does not exist)"


# ---------------------------------------------------------------------------
# 4. auth_required correctness
# ---------------------------------------------------------------------------

# Paths (base-relative, matching tools.json format) confirmed public by
# ApiKeyMiddleware.public_paths in middleware/api_key.py.
# Every tool NOT in this set must have auth_required: true.
_KNOWN_PUBLIC_TOOL_PATHS: frozenset[str] = frozenset({
    "/openapi.json",        # middleware.public_paths via /v1/openapi.json
    "/ai/context",           # middleware.public_paths
    "/ai/proof/market-edge", # middleware.public_paths
    "/pricing",              # middleware.public_paths
    "/pricing/catalog",      # middleware.public_paths; public planning infrastructure
    "/cost-estimate",        # middleware.public_paths; public planning helper
    "/workflows",            # middleware.public_paths
    "/instruments/lookup",   # middleware.public_paths; public planning helper
    "/instruments/resolve",  # middleware.public_paths; public planning helper
    "/stwr/reports/catalog", # middleware.public_paths; public planning helper
    "/meta/indicators",      # middleware.public_paths; public planning helper
    "/meta/inference",       # middleware.public_paths; public planning helper
    "/meta/stim",            # middleware.public_paths; public planning helper
    "/meta/stwr",            # middleware.public_paths; public planning helper
    "/leadership/definitions", # middleware.public_paths; public planning helper
})


def test_pricing_metadata_auth_required_false(manifest):
    """/pricing is a public endpoint — auth_required must be false."""
    tool = _tool_by_path(manifest, "/pricing")
    assert tool is not None, "/pricing tool entry must exist in tools.json"
    assert tool.get("auth_required") is False, \
        f"/pricing must have auth_required: false, got {tool.get('auth_required')!r}"


def test_workflows_auth_required_false(manifest):
    """/workflows is a public endpoint — auth_required must be false."""
    tool = _tool_by_path(manifest, "/workflows")
    assert tool is not None, "/workflows tool entry must exist in tools.json"
    assert tool.get("auth_required") is False, \
        f"/workflows must have auth_required: false, got {tool.get('auth_required')!r}"


def test_pricing_catalog_auth_required_false(manifest):
    """/pricing/catalog is public planning infrastructure under current API behavior."""
    tool = _tool_by_path(manifest, "/pricing/catalog")
    assert tool is not None, "/pricing/catalog tool entry must exist in tools.json"
    assert tool.get("auth_required") is False, \
        f"/pricing/catalog must have auth_required: false, got {tool.get('auth_required')!r}"


def test_auth_required_false_only_for_known_public_tools(manifest):
    """auth_required: false is only valid for paths in the known-public allowlist."""
    violations = []
    for tool in manifest["tools"]:
        if tool.get("auth_required") is False:
            path = tool.get("path", "")
            if path not in _KNOWN_PUBLIC_TOOL_PATHS:
                violations.append(f"{tool.get('name', '<unnamed>')} (path={path!r})")
    assert not violations, (
        "These tools are marked auth_required: false but are NOT in the known-public allowlist "
        "(_KNOWN_PUBLIC_TOOL_PATHS). Set auth_required: true or add the path to the allowlist "
        "after verifying it is in ApiKeyMiddleware.public_paths:\n"
        + "\n".join(violations)
    )


def test_llms_txt_has_agentic_service_positioning():
    text = LLMS_TXT.read_text(encoding="utf-8")
    assert "Autonomous portfolio intelligence API for AI agents" in text
    assert "x402 and MPP payment rails" in text
    assert "not investment advice" in text.lower()


def test_instrument_lookup_auth_required_false(manifest):
    """/instruments/lookup is a public planning helper."""
    tool = _tool_by_path(manifest, "/instruments/lookup")
    assert tool is not None, "/instruments/lookup must exist in tools.json"
    assert tool.get("auth_required") is False, \
        f"/instruments/lookup must have auth_required: false, got {tool.get('auth_required')!r}"


def test_leadership_summary_latest_auth_required_true(manifest):
    """/leadership/summary/latest is not a public path — auth_required must be true."""
    tool = _tool_by_path(manifest, "/leadership/summary/latest")
    assert tool is not None, "/leadership/summary/latest must exist in tools.json"
    assert tool.get("auth_required") is True, \
        f"/leadership/summary/latest must have auth_required: true, got {tool.get('auth_required')!r}"
    assert tool.get("pricing_rule_id") == "leadership_summary_latest_paid"
    assert set(tool.get("supported_rails", [])) == {"subscription", "x402", "mpp"}


# ---------------------------------------------------------------------------
# 5–6. Selections tools
# ---------------------------------------------------------------------------

def test_selections_latest_present_with_correct_path(manifest):
    tool = _tool_by_name(manifest, "selections_latest")
    assert tool is not None, "selections_latest must be present in tools.json"
    assert tool.get("path") == "/selections/latest", \
        f"selections_latest path must be /selections/latest, got {tool.get('path')!r}"


def test_selections_published_latest_correct_path(manifest):
    """Published selections must use /selections/published/latest (not /selections-published/)."""
    tool = _tool_by_name(manifest, "selections_published_latest")
    assert tool is not None, "selections_published_latest must be present in tools.json"
    assert tool.get("path") == "/selections/published/latest", \
        f"Expected /selections/published/latest, got {tool.get('path')!r}"


def test_indicators_tools_present(manifest):
    for name, path in (
        ("indicators_latest", "/indicators/latest"),
        ("indicators_history", "/indicators/history"),
    ):
        tool = _tool_by_name(manifest, name)
        assert tool is not None, f"{name} must be present in static/tools.json"
        assert tool.get("path") == path
        assert "Fetch /v1/pricing/catalog" in tool.get("description", "")


def test_static_get_parameters_have_query_location(manifest):
    for name in (
        "instrument_lookup",
        "indicators_latest",
        "indicators_history",
        "prices_latest",
        "prices_history",
        "stim_latest",
        "stim_history",
        "stwr_reports_latest",
        "stwr_reports_history",
    ):
        tool = _tool_by_name(manifest, name)
        assert tool is not None
        for param in tool.get("parameters", []):
            assert param.get("in") == "query", f"{name} parameter {param.get('name')} missing query location"
            assert param.get("parameter_source") == "query", f"{name} parameter {param.get('name')} missing parameter_source=query"


def test_static_target_get_tools_expose_expected_query_parameter_names(manifest):
    expected = {
        "indicators_latest": "symbol_exchange",
        "indicators_history": "symbol_exchange",
        "prices_latest": "symbol_exchange",
        "prices_history": "symbol_exchange",
        "stim_latest": "symbol_exchange",
        "stim_history": "symbol_exchange",
        "stwr_reports_latest": "rpt",
        "stwr_reports_history": "rpt",
    }
    for name, param_name in expected.items():
        tool = _tool_by_name(manifest, name)
        assert tool is not None
        params = {param["name"]: param for param in tool.get("parameters", [])}
        assert param_name in params
        assert params[param_name]["in"] == "query"
        assert params[param_name]["parameter_source"] == "query"


def test_static_cost_estimate_lists_safe_workflow_examples(manifest):
    tool = _tool_by_name(manifest, "cost_estimate")
    assert tool is not None
    workflow_id = next(param for param in tool["parameters"] if param["name"] == "workflow_id")
    expected = {
        "portfolio_build",
        "symbol_decision",
        "regime_analysis",
        "portfolio_compare_review",
        "stim_forecast_review",
    }
    assert expected.issubset(set(workflow_id["allowed_values"]))
    assert workflow_id["example"] == "portfolio_build"
    assert tool["safe_example_request"]["query"]["workflow_id"] == "portfolio_build"


def test_static_stim_tools_expose_interpretation_guidance(manifest):
    for name in ("stim_latest", "stim_history"):
        tool = _tool_by_name(manifest, name)
        assert tool is not None
        assert tool["interpretation_dependency"]["endpoint"] == "/v1/meta/stim"
        assert tool["interpretation_dependency"]["required_before_interpretation"] is True
        assert "base_period_mean_returns_pct" in tool["interpretation_guidance"]
        assert tool["interpretation_guidance"]["mean_return_fields"] == ["x4wk", "x13wk", "x40wk"]
        assert tool["interpretation_guidance"]["standard_deviation_fields"] == [
            "x4wksd",
            "x13wksd",
            "x40wksd",
        ]
        assert tool["interpretation_guidance"]["calculation"]["probability_outperform"] == "1 - normal_cdf(z)"
        assert tool["interpretation_guidance"]["stim_select_style_logic"]["prob13wk_minimum"] == 0.55
        assert "prob13wk_minimum_description" in tool["interpretation_guidance"]["stim_select_style_logic"]


def test_static_stim_guidance_matches_live_ai_tools_for_key_fields(manifest):
    from routers.ai import ai_tools

    live_tools = {
        tool["name"]: tool
        for tool in ai_tools()["tools"]
        if tool["name"] in {"stim_latest", "stim_history"}
    }
    key_fields = (
        "mean_return_fields",
        "standard_deviation_fields",
        "interpretation_rules",
    )

    for name in ("stim_latest", "stim_history"):
        static_tool = _tool_by_name(manifest, name)
        assert static_tool is not None
        live_tool = live_tools[name]
        static_guidance = static_tool["interpretation_guidance"]
        live_guidance = live_tool["interpretation_guidance"]

        for field in key_fields:
            assert static_guidance[field] == live_guidance[field]

        static_logic = static_guidance["stim_select_style_logic"]
        live_logic = live_guidance["stim_select_style_logic"]
        for key in live_logic:
            assert key in static_logic
            assert static_logic[key] == live_logic[key]


def test_static_planning_helpers_present(manifest):
    for name, path in (
        ("openapi_schema", "/openapi.json"),
        ("instrument_resolve", "/instruments/resolve"),
        ("stwr_reports_catalog", "/stwr/reports/catalog"),
        ("meta_indicators", "/meta/indicators"),
        ("meta_inference", "/meta/inference"),
        ("meta_stim", "/meta/stim"),
        ("meta_stwr", "/meta/stwr"),
        ("leadership_definitions", "/leadership/definitions"),
    ):
        tool = _tool_by_name(manifest, name)
        assert tool is not None
        assert tool.get("path") == path
        assert tool.get("planning_helper") is True


def test_static_paid_entries_have_manifest_pricing(manifest):
    for name, rule_id, rails in (
        ("stim_latest", "stim_latest_paid", {"subscription", "x402", "mpp"}),
        ("indicators_latest", "indicators_latest_paid", {"subscription", "x402", "mpp"}),
        ("compare_portfolios", "portfolio_compare", {"subscription", "x402", "mpp"}),
        ("breadth_sector_latest", "breadth_sector_latest_paid", {"subscription", "x402", "mpp"}),
        ("breadth_sector_history", "breadth_sector_history_paid", {"subscription", "x402", "mpp"}),
        ("leadership_summary_latest", "leadership_summary_latest_paid", {"subscription", "x402", "mpp"}),
        ("leadership_rotation_history", "leadership_rotation_history_paid", {"subscription", "x402", "mpp"}),
    ):
        tool = _tool_by_name(manifest, name)
        assert tool is not None
        assert tool["pricing_rule_id"] == rule_id
        assert "stc_cost" in tool
        assert "estimated_usd_cost" in tool
        assert set(tool["supported_rails"]) == rails
        assert "STC is the source of truth" in tool["pricing_note"]


def test_breadth_sector_latest_static_manifest_is_paid(manifest):
    tool = _tool_by_name(manifest, "breadth_sector_latest")
    assert tool is not None
    assert tool["path"] == "/breadth/sector/latest"
    assert tool["auth_required"] is True
    assert tool["access_type"] == "paid"
    assert tool["requires_payment"] is True
    assert tool["pricing_rule_id"] == "breadth_sector_latest_paid"
    assert tool["supported_rails"] == ["subscription", "x402", "mpp"]


def test_static_portfolio_compare_safe_example_shape(manifest):
    tool = _tool_by_name(manifest, "compare_portfolios")
    payload = tool["safe_example_request"]["json"]
    assert payload == {
        "left": [{"symbol_exchange": "IBM-N", "weight": 1}],
        "right": [{"symbol_exchange": "MSFT-Q", "weight": 1}],
    }


def test_all_registry_endpoints_in_static_tools(manifest):
    from discovery.endpoint_metadata import iter_endpoint_metadata

    static_paths = {tool["path"] for tool in manifest["tools"]}
    missing = []
    for entry in iter_endpoint_metadata():
        expected_path = entry["path"].removeprefix("/v1")
        if expected_path not in static_paths:
            missing.append(f"{entry['method']} {entry['path']}")

    assert not missing, (
        "Registry endpoints missing from static/tools.json:\n" + "\n".join(missing)
    )


# ---------------------------------------------------------------------------
# Semantic fields — analytical_role
# ---------------------------------------------------------------------------

_EXPECTED_STATIC_ANALYTICAL_ROLES: dict[str, str] = {
    # static tool name → expected analytical_role
    "agent_screener_top": "market_intelligence_filter",
    "market_regime_latest": "market_regime_classifier",
    "market_regime_history": "market_regime_classifier",
    "market_regime_forecast": "market_regime_classifier",
    "breadth_sector_latest": "market_breadth_context",
    "breadth_sector_history": "market_breadth_context",
    "leadership_summary_latest": "leadership_intelligence",
    "leadership_rotation_history": "leadership_intelligence",
    "stim_latest": "probabilistic_forward_inference",
    "stim_history": "probabilistic_forward_inference",
    "selections_latest": "probabilistic_selection_universe",
    "selections_history": "probabilistic_selection_universe",
    "selections_published_latest": "probabilistic_selection_list",
    "selections_published_history": "probabilistic_selection_list",
    "evaluate_symbol": "symbol_decision_engine",
    "construct_portfolio": "portfolio_construction_engine",
    "evaluate_portfolio": "portfolio_evaluation_engine",
    "compare_portfolios": "portfolio_evaluation_engine",
    "indicators_latest": "symbol_signal_intelligence",
    "indicators_history": "symbol_signal_intelligence",
    "prices_latest": "price_context",
    "prices_history": "price_context",
    "stwr_reports_latest": "curated_signal_report",
    "stwr_reports_history": "curated_signal_report",
}


def test_static_paid_tools_have_analytical_role(manifest):
    """Paid tools in static/tools.json must carry an analytical_role field."""
    missing = []
    for name in _EXPECTED_STATIC_ANALYTICAL_ROLES:
        tool = _tool_by_name(manifest, name)
        assert tool is not None, f"Tool '{name}' must be present in static/tools.json"
        if "analytical_role" not in tool:
            missing.append(name)
    assert not missing, f"These tools are missing analytical_role in static/tools.json: {missing}"


def test_static_analytical_role_values_correct(manifest):
    """analytical_role values in tools.json must match canonical expected mapping."""
    violations = []
    for name, expected_role in _EXPECTED_STATIC_ANALYTICAL_ROLES.items():
        static_tool = _tool_by_name(manifest, name)
        if static_tool is None:
            continue
        static_role = static_tool.get("analytical_role")
        if static_role != expected_role:
            violations.append(
                f"{name}: got {static_role!r}, expected {expected_role!r}"
            )
    assert not violations, (
        "analytical_role values incorrect in static/tools.json:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Semantic fields — interpretation_guidance drift (static vs live)
# ---------------------------------------------------------------------------

def test_static_regime_tools_have_interpretation_guidance(manifest):
    """market_regime_latest and market_regime_history must carry interpretation_guidance in tools.json."""
    for name in ("market_regime_latest", "market_regime_history"):
        tool = _tool_by_name(manifest, name)
        assert tool is not None, f"{name} must be present in static/tools.json"
        assert "interpretation_guidance" in tool, (
            f"{name} must have interpretation_guidance in static/tools.json"
        )
        guidance = tool["interpretation_guidance"]
        assert "regime_score_scale" in guidance
        assert guidance["regime_score_scale"]["formula"] == "bullish_pct - bearish_pct"
        assert "interpretation_rules" in guidance


def test_static_published_selections_have_interpretation_guidance(manifest):
    """Published STIM Select tools must carry interpretation_guidance with operator-structured criteria."""
    for name in ("selections_published_latest", "selections_published_history"):
        tool = _tool_by_name(manifest, name)
        assert tool is not None, f"{name} must be present in static/tools.json"
        assert "interpretation_guidance" in tool, (
            f"{name} must have interpretation_guidance in static/tools.json"
        )
        guidance = tool["interpretation_guidance"]
        assert "publication_criteria" in guidance
        criteria = guidance["publication_criteria"]
        assert criteria["x13wk1"]["operator"] == ">"
        assert criteria["x13wk1"]["threshold_pct"] == 2.19
        assert criteria["prob13wk"]["operator"] == ">="
        assert criteria["prob13wk"]["threshold"] == 0.55
        assert criteria["all_criteria_required"] is True


def test_static_regime_guidance_matches_live(manifest):
    """Static regime interpretation_guidance must match live /v1/ai/tools for key fields."""
    from routers.ai import ai_tools
    live_tools = {t["name"]: t for t in ai_tools()["tools"]}

    for name in ("market_regime_latest", "market_regime_history"):
        static_tool = _tool_by_name(manifest, name)
        assert static_tool is not None
        live_tool = live_tools.get(name)
        assert live_tool is not None, f"Live tool '{name}' not found in /v1/ai/tools"

        static_guidance = static_tool.get("interpretation_guidance", {})
        live_guidance = live_tool.get("interpretation_guidance", {})

        assert static_guidance.get("regime_score_scale") == live_guidance.get("regime_score_scale"), (
            f"{name}: static regime_score_scale does not match live"
        )
        assert static_guidance.get("interpretation_rules") == live_guidance.get("interpretation_rules"), (
            f"{name}: static interpretation_rules does not match live"
        )


def test_static_stim_select_guidance_matches_live(manifest):
    """Static STIM Select interpretation_guidance must match live /v1/ai/tools for key fields."""
    from routers.ai import ai_tools
    live_tools = {t["name"]: t for t in ai_tools()["tools"]}

    for static_name, live_name in (
        ("selections_published_latest", "selections_published_latest"),
        ("selections_published_history", "selections_published_history"),
    ):
        static_tool = _tool_by_name(manifest, static_name)
        assert static_tool is not None
        live_tool = live_tools.get(live_name)
        assert live_tool is not None, f"Live tool '{live_name}' not found in /v1/ai/tools"

        static_criteria = static_tool.get("interpretation_guidance", {}).get("publication_criteria", {})
        live_criteria = live_tool.get("interpretation_guidance", {}).get("publication_criteria", {})

        for field in ("x4wk1", "x13wk1", "x40wk1", "prob13wk"):
            assert static_criteria.get(field) == live_criteria.get(field), (
                f"{static_name}: static publication_criteria[{field!r}] does not match live"
            )
        assert static_criteria.get("all_criteria_required") == live_criteria.get("all_criteria_required")


# ---------------------------------------------------------------------------
# 7. No hardcoded STC costs
# ---------------------------------------------------------------------------

def test_no_hardcoded_stc_costs_in_tool_descriptions(manifest):
    """Tool descriptions must not contain hardcoded 'STC per call'."""
    violations = [
        t.get("name", "<unnamed>")
        for t in manifest["tools"]
        if "STC per call" in t.get("description", "")
    ]
    assert not violations, (
        "These tools contain hardcoded 'STC per call' — "
        "use 'Fetch /v1/pricing/catalog for current STC cost.' instead:\n"
        + "\n".join(violations)
    )
