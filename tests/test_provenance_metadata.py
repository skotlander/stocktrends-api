from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from discovery.provenance import INDICATORS_PROVENANCE_TEXT, data_provenance


REPO_ROOT = Path(__file__).resolve().parents[1]
INTERNAL_TABLE_NAMES = (
    "st_data",
    "st_mast",
    "st_select",
    "st_returnmeans",
    "st_listsectorsandindustries",
    "api_pricing_rules",
)
PROHIBITED_AFFIRMATIVE_CLAIMS = (
    "guaranteed alpha",
    "guaranteed return",
    "guaranteed returns",
    "guaranteed outperformance",
    "investment advice service",
    "price target recommendation",
    "direct buy recommendation",
    "direct sell recommendation",
)


def _serialized(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _assert_no_internal_tables(value: object) -> None:
    serialized = _serialized(value)
    violations = [
        name
        for name in INTERNAL_TABLE_NAMES
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", serialized)
    ]
    contexts = []
    for name in violations:
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])"
        match = re.search(pattern, serialized)
        if match:
            contexts.append(serialized[max(0, match.start() - 120): match.end() + 120])
    assert not violations, f"Internal table names leaked: {violations}\n" + "\n".join(contexts)


def _assert_no_prohibited_affirmative_claims(value: object) -> None:
    serialized = _serialized(value).lower()
    violations = [phrase for phrase in PROHIBITED_AFFIRMATIVE_CLAIMS if phrase in serialized]
    assert not violations, f"Prohibited affirmative claims found: {violations}"


def _assert_review_vocabulary(value: object) -> None:
    serialized = _serialized(value)
    assert "momentum direction" not in serialized
    assert "trend duration" not in serialized
    assert "relative performance direction" in serialized
    assert "trend persistence" in serialized


def test_central_data_provenance_shape():
    provenance = data_provenance()

    assert provenance["historical_coverage_start_year"] == 1980
    assert provenance["approximate_observation_count"] == "16M+"
    assert provenance["update_frequency"] == "weekly"
    assert "trend classification" in provenance["native_signal_domains"]
    assert "relative performance direction" in provenance["native_signal_domains"]
    assert "regime analysis" in provenance["research_value"]
    assert any("does not guarantee future performance" in item for item in provenance["important_limits"])
    assert any("not investment advice" in item for item in provenance["important_limits"])
    _assert_review_vocabulary({"provenance": provenance, "indicators": INDICATORS_PROVENANCE_TEXT})


def test_ai_context_and_tools_expose_full_provenance(monkeypatch):
    import routers.ai as ai_router

    monkeypatch.setattr(ai_router, "get_last_update", lambda: None)

    context = ai_router.ai_context()
    tools = ai_router.ai_tools()

    for surface in (context, tools):
        assert surface["data_provenance"]["historical_coverage_start_year"] == 1980
        assert surface["data_provenance"]["approximate_observation_count"] == "16M+"
        assert "1980" in surface["provenance_summary"]
        assert "16M+" in surface["provenance_summary"]
        _assert_no_internal_tables(surface)
        _assert_no_prohibited_affirmative_claims(surface)

    assert all("data_provenance" not in tool for tool in tools["tools"])
    assert all("data_provenance" not in workflow for workflow in tools["workflows"])
    assert any("provenance_reference" in tool for tool in tools["tools"])

    indicators_family = context["endpoint_family_relationships"]["indicators"]
    assert indicators_family["semantics"] == (
        "trend classification, persistence, maturity, RSI baseline 100, volume tags"
    )
    assert "1980" in indicators_family["research_provenance"]
    assert "trend persistence" in indicators_family["research_provenance"]


def test_meta_indicator_and_stim_profiles_include_provenance_and_limits():
    from routers.meta import meta_indicators, meta_stim

    request = SimpleNamespace(state=SimpleNamespace(request_id="req_test"))
    indicators = meta_indicators(request)
    stim = meta_stim(request)

    assert indicators["data_provenance"]["historical_coverage_start_year"] == 1980
    assert indicators["data_provenance"]["approximate_observation_count"] == "16M+"
    assert "multi-decade classification framework" in indicators["provenance_summary"]

    assert stim["data_provenance"]["historical_coverage_start_year"] == 1980
    assert stim["data_provenance"]["approximate_observation_count"] == "16M+"
    assert "ST-IM outputs are not guarantees" in stim["description"]
    assert "price targets" in stim["description"]
    assert "buy/sell commands" in stim["description"]

    _assert_no_internal_tables(indicators)
    _assert_no_internal_tables(stim)
    _assert_no_prohibited_affirmative_claims(indicators)
    _assert_no_prohibited_affirmative_claims(stim)


def test_endpoint_metadata_and_previews_use_compact_provenance_reference_without_stim_repetition():
    from discovery.endpoint_metadata import build_tool_template
    from discovery.preview import get_endpoint_preview

    for path in ("/v1/indicators/latest", "/v1/market/regime/latest"):
        template = build_tool_template(path)
        preview = get_endpoint_preview(path)
        assert template is not None
        assert preview is not None
        assert template["provenance_reference"]["historical_coverage_start_year"] == 1980
        assert preview["provenance_reference"]["approximate_observation_count"] == "16M+"
        assert "data_provenance" not in template
        assert "data_provenance" not in preview

    stim_template = build_tool_template("/v1/stim/latest")
    stim_preview = get_endpoint_preview("/v1/stim/latest")
    assert stim_template is not None
    assert stim_preview is not None
    assert "provenance_reference" not in stim_template
    assert "provenance_reference" not in stim_preview
    assert stim_template["inference_provider"]["provider_profile_endpoint"] == "/v1/meta/stim"


def test_workflows_surface_exposes_single_full_provenance_block(monkeypatch):
    import routers.workflows as workflows_router
    from routers.workflows import WORKFLOW_REGISTRY

    class _Result:
        def __init__(self, rows: list[dict]):
            self._rows = rows

        def mappings(self):
            return self

        def all(self):
            return self._rows

    class _Connection:
        def __init__(self, rows: list[dict]):
            self._rows = rows

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            return _Result(self._rows)

    class _Engine:
        def __init__(self, rows: list[dict]):
            self._rows = rows

        def begin(self):
            return _Connection(self._rows)

    rule_ids = {
        step["pricing_rule_id"]
        for workflow in WORKFLOW_REGISTRY
        for step in workflow["steps"]
        if step.get("pricing_rule_id")
    }
    rows = [{"rule_name": rule_id, "cost_per_request": 0.25} for rule_id in sorted(rule_ids)]

    monkeypatch.setattr(
        workflows_router,
        "get_metering_engine",
        lambda: _Engine(rows),
    )

    response = workflows_router.get_workflows()
    body = json.loads(response.body)

    assert body["data_provenance"]["historical_coverage_start_year"] == 1980
    assert body["data_provenance"]["approximate_observation_count"] == "16M+"
    assert "provenance_reference" not in body["agent_guidance"]
    assert all("data_provenance" not in workflow for workflow in body["workflows"])
    assert all("provenance_reference" not in workflow for workflow in body["workflows"])
    _assert_no_internal_tables(body)


def test_openapi_has_top_level_provenance_and_no_internal_table_names():
    import main

    main.v1.openapi_schema = None
    schema = main.apply_api_key_security_to_openapi(main.v1)

    assert schema["x-stocktrends-data-provenance"]["historical_coverage_start_year"] == 1980
    assert schema["x-stocktrends-data-provenance"]["approximate_observation_count"] == "16M+"

    for path in ("/meta/indicators", "/meta/stim", "/indicators/latest", "/workflows"):
        operation = schema["paths"][path]["get"]
        reference = operation["x-stocktrends-data-provenance-reference"]
        assert reference["historical_coverage_start_year"] == 1980
        assert reference["approximate_observation_count"] == "16M+"

    stim_operation = schema["paths"]["/stim/latest"]["get"]
    assert "x-stocktrends-data-provenance-reference" not in stim_operation
    assert stim_operation["x-stocktrends-provider-profile"] == "/v1/meta/stim"

    _assert_no_internal_tables(schema)
    _assert_no_prohibited_affirmative_claims(schema)


def test_static_tools_json_and_llms_txt_include_provenance_without_table_leaks():
    tools = json.loads((REPO_ROOT / "static" / "tools.json").read_text(encoding="utf-8"))
    llms = (REPO_ROOT / "static" / "llms.txt").read_text(encoding="utf-8")

    assert tools["data_provenance"]["historical_coverage_start_year"] == 1980
    assert tools["data_provenance"]["approximate_observation_count"] == "16M+"
    assert "Historical Provenance" in llms
    assert "1980" in llms
    assert "16M+" in llms
    _assert_review_vocabulary(tools)
    assert "relative performance direction" in llms
    assert "momentum direction" not in llms
    assert "trend duration" not in llms

    _assert_no_internal_tables(tools)
    for table_name in INTERNAL_TABLE_NAMES:
        assert not re.search(rf"(?<![A-Za-z0-9_]){re.escape(table_name)}(?![A-Za-z0-9_])", llms)
    _assert_no_prohibited_affirmative_claims(tools)
    _assert_no_prohibited_affirmative_claims({"llms": llms})


def test_leadership_public_definitions_do_not_expose_internal_tables():
    from routers.leadership import leadership_definitions

    definitions = leadership_definitions()

    assert definitions["taxonomy_source"] == "Stock Trends sector and industry taxonomy"
    _assert_no_internal_tables(definitions)
