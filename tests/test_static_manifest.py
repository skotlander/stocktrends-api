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
    "/workflows",            # middleware.public_paths
    "/breadth/sector/latest", # free-metered, anonymously accessible via runtime policy
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


def test_instrument_lookup_auth_required_true(manifest):
    """/instruments/lookup is not a public path — auth_required must be true."""
    tool = _tool_by_path(manifest, "/instruments/lookup")
    assert tool is not None, "/instruments/lookup must exist in tools.json"
    assert tool.get("auth_required") is True, \
        f"/instruments/lookup must have auth_required: true, got {tool.get('auth_required')!r}"


def test_leadership_summary_latest_auth_required_true(manifest):
    """/leadership/summary/latest is not a public path — auth_required must be true."""
    tool = _tool_by_path(manifest, "/leadership/summary/latest")
    assert tool is not None, "/leadership/summary/latest must exist in tools.json"
    assert tool.get("auth_required") is True, \
        f"/leadership/summary/latest must have auth_required: true, got {tool.get('auth_required')!r}"


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
    for name in ("instrument_lookup", "indicators_latest", "indicators_history", "stim_latest", "stim_history"):
        tool = _tool_by_name(manifest, name)
        assert tool is not None
        for param in tool.get("parameters", []):
            assert param.get("in") == "query", f"{name} parameter {param.get('name')} missing query location"


def test_static_planning_helpers_present(manifest):
    for name, path in (
        ("openapi_schema", "/openapi.json"),
        ("instrument_resolve", "/instruments/resolve"),
        ("stwr_reports_catalog", "/stwr/reports/catalog"),
        ("meta_indicators", "/meta/indicators"),
        ("meta_stim", "/meta/stim"),
        ("meta_stwr", "/meta/stwr"),
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
    ):
        tool = _tool_by_name(manifest, name)
        assert tool is not None
        assert tool["pricing_rule_id"] == rule_id
        assert "stc_cost" in tool
        assert "estimated_usd_cost" in tool
        assert set(tool["supported_rails"]) == rails
        assert "STC is the source of truth" in tool["pricing_note"]


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
