"""
Tests for semantic discovery metadata — analytical_role, analytical_framework,
probabilistic_semantics, and analytical_chain in live AI surfaces.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ai_tools_response() -> dict:
    from routers.ai import ai_tools
    return ai_tools()


@pytest.fixture(scope="module")
def ai_context_response() -> dict:
    from routers.ai import ai_context
    try:
        return ai_context()
    except Exception:
        # DB unavailable in unit-test environment; call the underlying data assembly directly
        from routers.ai import (
            DATASET_DESCRIPTION,
            SERVICE_POSITIONING,
        )
        # Fallback: construct partial response for field-presence tests
        from routers.ai import ai_context as _ai_context
        import unittest.mock as mock
        with mock.patch("routers.ai.get_last_update", return_value=None):
            return _ai_context()


@pytest.fixture(scope="module")
def live_tools_by_name(ai_tools_response) -> dict:
    return {t["name"]: t for t in ai_tools_response["tools"]}


@pytest.fixture(scope="module")
def live_workflows_by_id(ai_tools_response) -> dict:
    return {w["workflow_id"]: w for w in ai_tools_response.get("workflows", [])}


# ---------------------------------------------------------------------------
# analytical_role — live tools
# ---------------------------------------------------------------------------

_EXPECTED_ANALYTICAL_ROLES = {
    "screener_top": "market_intelligence_filter",
    "market_regime_latest": "market_regime_classifier",
    "market_regime_history": "market_regime_classifier",
    "market_regime_forecast": "market_regime_classifier",
    "breadth_sector_latest": "market_breadth_context",
    "breadth_sector_history": "market_breadth_context",
    "leadership_summary_latest": "leadership_intelligence",
    "leadership_rotation_history": "leadership_intelligence",
    "intelligence_guidance_latest": "published_intelligence_artifact",
    "intelligence_guidance_by_id": "published_intelligence_artifact",
    "intelligence_research_latest": "published_intelligence_artifact",
    "intelligence_research_by_id": "published_intelligence_artifact",
    "stim_latest": "probabilistic_forward_inference",
    "stim_history": "probabilistic_forward_inference",
    "selections_latest": "probabilistic_selection_universe",
    "selections_history": "probabilistic_selection_universe",
    "selections_published_latest": "probabilistic_selection_list",
    "selections_published_history": "probabilistic_selection_list",
    "evaluate_symbol": "symbol_decision_engine",
    "portfolio_construct": "portfolio_construction_engine",
    "portfolio_evaluate": "portfolio_evaluation_engine",
    "portfolio_compare": "portfolio_evaluation_engine",
    "indicators_latest": "symbol_signal_intelligence",
    "indicators_history": "symbol_signal_intelligence",
    "prices_latest": "price_context",
    "prices_history": "price_context",
    "stwr_reports_latest": "curated_signal_report",
    "stwr_reports_history": "curated_signal_report",
}


def test_paid_tools_have_analytical_role(live_tools_by_name):
    """All paid endpoint tools must carry an analytical_role field."""
    missing = []
    for name, expected_role in _EXPECTED_ANALYTICAL_ROLES.items():
        tool = live_tools_by_name.get(name)
        assert tool is not None, f"Tool '{name}' not found in /v1/ai/tools"
        if "analytical_role" not in tool:
            missing.append(name)
    assert not missing, f"These tools are missing analytical_role: {missing}"


def test_analytical_role_values_correct(live_tools_by_name):
    """analytical_role values must match the canonical registry mapping."""
    violations = []
    for name, expected_role in _EXPECTED_ANALYTICAL_ROLES.items():
        tool = live_tools_by_name.get(name)
        if tool is None:
            continue
        actual = tool.get("analytical_role")
        if actual != expected_role:
            violations.append(f"{name}: expected {expected_role!r}, got {actual!r}")
    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------------
# analytical_role — regime endpoints have interpretation_guidance
# ---------------------------------------------------------------------------

def test_regime_endpoints_have_interpretation_guidance(live_tools_by_name):
    for name in ("market_regime_latest", "market_regime_history"):
        tool = live_tools_by_name.get(name)
        assert tool is not None
        assert "interpretation_guidance" in tool, (
            f"{name} must have interpretation_guidance with regime_score formula"
        )
        guidance = tool["interpretation_guidance"]
        assert "regime_score_scale" in guidance
        assert guidance["regime_score_scale"]["formula"] == "bullish_pct - bearish_pct"
        assert "interpretation_rules" in guidance
        assert len(guidance["interpretation_rules"]) > 0


# ---------------------------------------------------------------------------
# analytical_role — STIM Select endpoints have interpretation_guidance
# ---------------------------------------------------------------------------

def test_selections_published_have_interpretation_guidance(live_tools_by_name):
    for name in ("selections_published_latest", "selections_published_history"):
        tool = live_tools_by_name.get(name)
        assert tool is not None
        assert "interpretation_guidance" in tool, (
            f"{name} must have interpretation_guidance with STIM Select criteria"
        )
        guidance = tool["interpretation_guidance"]
        assert "publication_criteria" in guidance
        criteria = guidance["publication_criteria"]
        # Criteria must be operator-structured
        assert criteria["x13wk1"]["operator"] == ">"
        assert criteria["x13wk1"]["threshold_pct"] == 2.19
        assert criteria["prob13wk"]["operator"] == ">="
        assert criteria["prob13wk"]["threshold"] == 0.55
        assert criteria["all_criteria_required"] is True


# ---------------------------------------------------------------------------
# ai_context — analytical_framework
# ---------------------------------------------------------------------------

def test_ai_context_has_analytical_framework(ai_context_response):
    assert "analytical_framework" in ai_context_response, (
        "/v1/ai/context must contain an analytical_framework section"
    )
    framework = ai_context_response["analytical_framework"]
    assert "endpoint_roles" in framework
    roles = framework["endpoint_roles"]
    assert "probabilistic_forward_inference" in roles
    assert "market_regime_classifier" in roles
    assert "portfolio_construction_engine" in roles
    assert "probabilistic_selection_list" in roles


# ---------------------------------------------------------------------------
# ai_context — analytical_chain
# ---------------------------------------------------------------------------

def test_ai_context_has_analytical_chain(ai_context_response):
    assert "analytical_chain" in ai_context_response, (
        "/v1/ai/context must contain an analytical_chain section"
    )
    chain = ai_context_response["analytical_chain"]
    assert "steps" in chain
    steps = chain["steps"]
    assert len(steps) >= 5, "analytical_chain must have at least 5 steps"
    step_roles = [s["role"] for s in steps]
    assert "market_regime_classifier" in step_roles
    assert "probabilistic_forward_inference" in step_roles
    assert "portfolio_construction_engine" in step_roles


# ---------------------------------------------------------------------------
# ai_context — probabilistic_semantics
# ---------------------------------------------------------------------------

def test_ai_context_has_probabilistic_semantics(ai_context_response):
    assert "probabilistic_semantics" in ai_context_response, (
        "/v1/ai/context must contain a probabilistic_semantics section"
    )
    semantics = ai_context_response["probabilistic_semantics"]
    assert "stim_model" in semantics
    stim = semantics["stim_model"]
    assert stim["not_momentum"] is True
    assert stim["horizons_weeks"] == [4, 13, 40]
    assert stim["base_period_means_pct"]["x13wk"] == 2.19
    assert stim["base_period_means_pct"]["x40wk"] == 6.45

    assert "stim_select" in semantics
    sel = semantics["stim_select"]
    assert sel["criteria"]["prob13wk"]["operator"] == ">="
    assert sel["criteria"]["prob13wk"]["threshold"] == 0.55
    assert "not investment advice" in sel["note"].lower()

    assert "regime_score" in semantics
    assert semantics["regime_score"]["formula"] == "bullish_pct - bearish_pct"


# ---------------------------------------------------------------------------
# Workflows — analytical_role and research_goal
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ai_context - MT-4 semantic operating manual sections
# ---------------------------------------------------------------------------

def test_ai_context_has_mt4_semantic_sections(ai_context_response):
    expected = {
        "service_identity",
        "cognition_architecture",
        "analytical_framework",
        "analytical_chain",
        "probabilistic_semantics",
        "endpoint_family_relationships",
        "workflow_guidance",
        "decision_boundaries",
        "interpretation_dependencies",
        "research_context_not_investment_advice",
        "recommended_starting_paths",
    }
    missing = expected - set(ai_context_response)
    assert not missing, f"/v1/ai/context missing MT-4 semantic sections: {sorted(missing)}"


def test_ai_context_cognition_architecture_is_provider_agnostic(ai_context_response):
    cognition = ai_context_response["cognition_architecture"]
    assert cognition["inference_contract_endpoint"] == "/v1/meta/inference"
    assert cognition["current_baseline_provider_profile"] == "/v1/meta/stim"
    assert any("ST-IM is the current baseline" in item for item in cognition["doctrine"])
    assert "inference_provider" in cognition["provider_agnostic_concepts"]
    assert "uncertainty" in cognition["provider_agnostic_concepts"]
    assert "evidence" in cognition["provider_agnostic_concepts"]
    assert cognition["current_baseline_provider_role"] == "current_baseline_inference_provider"


def test_ai_context_service_identity_prevents_flattening(ai_context_response):
    identity = ai_context_response["service_identity"]
    boundaries = ai_context_response["decision_boundaries"]
    assert identity["primary_category"] == "agent_native_probabilistic_market_intelligence_infrastructure"
    assert "raw_market_data_api" in identity["is_not"]
    assert "generic_screener_api" in identity["is_not"]
    assert "investment_adviser" in identity["is_not"]
    assert "investment_advice" in identity["is_not"]
    assert "investment_advice_service" not in identity["is_not"]
    assert boundaries["stock_trends_is_not"] == identity["is_not"]
    assert identity["forecast_horizons_weeks"] == [4, 13, 40]

    dominant = identity["dominant_description"].lower()
    assert "raw stock data api" not in dominant
    assert "generic screener api" not in dominant
    assert "probabilistic market intelligence" in dominant


def test_ai_context_stim_semantics_require_base_period_comparison(ai_context_response):
    stim = ai_context_response["probabilistic_semantics"]["stim_model"]
    assert stim["full_name"] == "Stock Trends Inference Model"
    assert stim["inference_provider_id"] == "stim"
    assert stim["provider_role"] == "current_baseline_inference_provider"
    assert stim["not_final_intelligence_layer"] is True
    assert stim["output_type"] == "probabilistic forward return distribution"
    assert stim["not_momentum"] is True
    assert "momentum_indicator" in stim["not"]
    assert stim["comparison_required"] is True
    assert "base-period mean" in stim["comparison_rule"]
    assert stim["primary_probability_field"] == "prob13wk"
    assert stim["base_period_means_pct"] == {
        "x4wk": 0.0,
        "x13wk": 2.19,
        "x40wk": 6.45,
    }
    assert "conditional historical tendencies" in stim["interpretation_requirement"]
    assert "regime_shifts" in stim["limitations"]


def test_ai_context_stim_dependencies_include_base_and_published_selections(ai_context_response):
    dependency = ai_context_response["interpretation_dependencies"]["stim"]
    required_before = dependency["required_before"]
    assert dependency["required_endpoint"] == "/v1/meta/stim"
    assert "/v1/selections/latest" in required_before
    assert "/v1/selections/published/latest" in required_before

    stim_select = ai_context_response["probabilistic_semantics"]["stim_select"]
    assert stim_select["thresholds_source"] == "/v1/meta/stim"
    assert "Current explanatory" in stim_select["criteria_context"]
    assert "authoritative" in stim_select["criteria_context"]

    published = ai_context_response["endpoint_family_relationships"]["selections_published"]
    assert published["thresholds_source"] == "/v1/meta/stim"
    assert published["authoritative_threshold_context"] == "/v1/meta/stim"


def test_ai_context_rsi_benchmark_relative_semantics(ai_context_response):
    rsi = ai_context_response["interpretation_dependencies"]["rsi"]
    assert rsi["benchmark_baseline"] == 100
    assert rsi["above_baseline"] == "outperformance"
    assert rsi["below_baseline"] == "underperformance"
    assert "Wilder" in rsi["not"]

    framework_rsi = ai_context_response["analytical_framework"]["rsi_benchmark_baseline"]
    assert framework_rsi["baseline"] == 100
    assert "relative performance" in framework_rsi["meaning"]
    assert framework_rsi["primary_definition"] == "/v1/meta/indicators"
    assert framework_rsi["see_also"] == "interpretation_dependencies.rsi"
    assert rsi["see_also"] == "analytical_framework.rsi_benchmark_baseline"


def test_ai_context_post_execution_semantics(ai_context_response):
    semantics = ai_context_response["post_endpoint_execution_semantics"]
    assert semantics["descriptive_metadata_only"] is True
    assert semantics["runtime_behavior_changed"] is False
    assert semantics["common"]["state_mutation"] is False
    assert semantics["common"]["bounded_cost"] is True
    assert semantics["common"]["safe_for_autonomous_execution_with_budget_controls"] is True

    expected_posts = {
        "/v1/decision/evaluate-symbol",
        "/v1/portfolio/construct",
        "/v1/portfolio/evaluate",
        "/v1/portfolio/compare",
    }
    assert expected_posts.issubset(semantics["endpoints"])
    for path in expected_posts:
        endpoint_semantics = semantics["endpoints"][path]
        assert endpoint_semantics["state_mutation"] is False
        assert endpoint_semantics["bounded_cost"] is True
        assert endpoint_semantics["deterministic_for_identical_inputs"] is True
        assert endpoint_semantics["safe_for_autonomous_execution_with_budget_controls"] is True


def test_ai_context_recommended_chain_contains_market_structure_and_synthesis(ai_context_response):
    chain = ai_context_response["analytical_chain"]["steps"]
    paths = [step["endpoint"] for step in chain]
    roles = [step["role"] for step in chain]

    assert paths[:2] == ["/v1/ai/context", "/v1/workflows"]
    assert "/v1/meta/stim" in paths

    for role in (
        "market_regime_classifier",
        "market_breadth_context",
        "leadership_intelligence",
        "probabilistic_forward_inference",
        "symbol_decision_engine",
        "portfolio_construction_engine",
        "portfolio_evaluation_engine",
    ):
        assert role in roles


def test_ai_context_endpoint_family_when_to_use_what(ai_context_response):
    families = ai_context_response["endpoint_family_relationships"]
    assert families["screener_top"]["use_when"] == "broad candidate discovery"
    assert "generic screener" in families["screener_top"]["semantics"]
    assert families["selections_latest"]["use_when"] == "filtered and ranked STIM candidate universe"
    assert families["selections_published"]["use_when"] == "strict STIM Select-style candidates"
    assert families["stim"]["use_when"] == "symbol-specific probabilistic forward-return analysis"
    assert families["indicators"]["use_when"] == "Stock Trends signal context for a symbol"
    assert families["breadth"]["use_when"] == "participation and confirmation"
    assert families["leadership"]["use_when"] == "concentration and rotation analysis"
    assert families["portfolio"]["use_when"] == "portfolio analysis and comparison"


def test_ai_context_workflow_guidance_covers_agent_missions(ai_context_response):
    guidance = ai_context_response["workflow_guidance"]
    for mission in (
        "stim_forecast_review",
        "sector_rotation_analysis",
        "portfolio_build",
        "portfolio_compare_review",
        "symbol_decision",
        "full_research_sweep",
    ):
        assert mission in guidance
        assert guidance[mission]["mission"]
        assert guidance[mission]["core_path"]


def test_ai_context_research_context_not_investment_advice(ai_context_response):
    section = ai_context_response["research_context_not_investment_advice"]
    assert section["not_investment_advice"] is True
    assert "research context" in section["agent_instruction"]
    assert "guaranteed outcomes" in section["no_guarantee"]


def test_ai_context_anti_drift_service_description_not_raw_or_generic(ai_context_response):
    service_description = ai_context_response["service_description"].lower()
    first_sentence = service_description.split(".")[0]
    assert "raw stock data api" not in first_sentence
    assert "generic screener api" not in first_sentence
    assert "probabilistic market intelligence" in service_description


_EXPECTED_WORKFLOW_ROLES = {
    "regime_analysis": "market_context_workflow",
    "symbol_decision": "symbol_evaluation_workflow",
    "stim_forecast_review": "probabilistic_forecast_workflow",
    "portfolio_build": "portfolio_construction_workflow",
    "portfolio_compare_review": "portfolio_comparison_workflow",
}


def test_workflows_have_analytical_role(live_workflows_by_id):
    for wf_id, expected_role in _EXPECTED_WORKFLOW_ROLES.items():
        wf = live_workflows_by_id.get(wf_id)
        assert wf is not None, f"Workflow '{wf_id}' not found in ai_tools response"
        assert wf.get("analytical_role") == expected_role, (
            f"Workflow {wf_id}: expected analytical_role={expected_role!r}, "
            f"got {wf.get('analytical_role')!r}"
        )


def test_workflows_have_research_goal(live_workflows_by_id):
    for wf_id in _EXPECTED_WORKFLOW_ROLES:
        wf = live_workflows_by_id.get(wf_id)
        assert wf is not None
        assert wf.get("research_goal"), (
            f"Workflow '{wf_id}' must have a non-empty research_goal"
        )
