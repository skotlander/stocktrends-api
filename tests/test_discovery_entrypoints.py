from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

# Module stubs for sqlalchemy/db/etc. are provided by tests/conftest.py.
import main
import middleware.api_key as api_key_module
import middleware.metering as metering_module
from api.routing import install_payment_execution_boundary


def _stub_runtime_side_effects(monkeypatch):
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(metering_module, "log_api_request_economics", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda *args, **kwargs: (Decimal("0"), Decimal("0")),
    )
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda *args, **kwargs: None)


def _expected_not_found_payload(path: str) -> dict[str, str]:
    return {
        "detail": "Not Found",
        "requested_path": path,
        "x402_discovery": "/.well-known/x402",
        "start_here": "/v1/ai/tools",
        "secondary": "/v1/ai/context",
        "docs": "/v1/docs",
        "openapi": "/v1/openapi.json",
    }


@pytest.fixture
def client(monkeypatch):
    _stub_runtime_side_effects(monkeypatch)
    with TestClient(main.app) as test_client:
        yield test_client


def test_root_guides_to_canonical_openapi_and_task_discovery(client):
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == (
        "Use x402_discovery for payable-resource discovery, start_here for task "
        "discovery, and the canonical OpenAPI contract for exact request schemas."
    )
    assert "Autonomous portfolio intelligence API for AI agents" in body["description"]
    assert body["developer_portal"] == "https://developer.stocktrends.com/"
    assert body["x402_discovery"] == "https://api.stocktrends.com/.well-known/x402"
    assert body["start_here"] == "https://api.stocktrends.com/v1/ai/tools"
    assert body["secondary_context"] == "https://api.stocktrends.com/v1/ai/context"
    assert body["secondary"] == "https://api.stocktrends.com/v1/ai/context"
    assert body["workflows"] == "https://api.stocktrends.com/v1/workflows"
    assert body["pricing_catalog"] == "https://api.stocktrends.com/v1/pricing/catalog"
    assert body["docs"] == "https://api.stocktrends.com/v1/docs"
    assert body["openapi"] == "https://api.stocktrends.com/openapi.json"
    assert "https://api.stocktrends.com/v1/workflows" in body["planning_helpers"]


def test_public_not_found_guides_to_ai_tools_first(client):
    response = client.get("/missing-route")

    assert response.status_code == 404
    assert response.json() == _expected_not_found_payload("/missing-route")


def test_authenticated_v1_not_found_returns_structured_guidance(client, monkeypatch):
    def fake_authenticate(self, path: str, raw_key: str):
        return True, {
            "api_key_id": "test-key-id",
            "customer_id": "test-customer-id",
            "subscription_id": "test-subscription-id",
            "plan_code": "pro",
            "actor_type": "external_customer",
            "monthly_quota": 1000,
        }

    monkeypatch.setattr(api_key_module.ApiKeyMiddleware, "_authenticate_api_key", fake_authenticate)

    response = client.get("/v1/missing-route", headers={"X-API-Key": "test-key"})

    assert response.status_code == 404
    assert response.json() == _expected_not_found_payload("/v1/missing-route")


def test_route_level_404_keeps_existing_detail_schema(client, monkeypatch):
    def fake_authenticate(self, path: str, raw_key: str):
        return True, {
            "api_key_id": "test-key-id",
            "customer_id": "test-customer-id",
            "subscription_id": "test-subscription-id",
            "plan_code": "pro",
            "actor_type": "external_customer",
            "monthly_quota": 1000,
        }

    monkeypatch.setattr(api_key_module.ApiKeyMiddleware, "_authenticate_api_key", fake_authenticate)

    # The probe route is registered on the shared v1 application, so it has to
    # be withdrawn again: leaving it behind gives every later test module a
    # route that was never put through the payment execution boundary, which
    # the universal-coverage guard correctly reports as a hole.
    original_routes = list(main.v1.routes)
    original_schema = main.v1.openapi_schema
    try:
        @main.v1.get("/_test-route-level-404")
        def route_level_404():
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Route-level missing resource")

        install_payment_execution_boundary(main.v1)

        response = client.get("/v1/_test-route-level-404", headers={"X-API-Key": "test-key"})
    finally:
        main.v1.router.routes[:] = original_routes
        # A schema generated while the probe route existed would otherwise stay
        # cached after the route is gone, advertising an endpoint that no longer
        # exists to every later test in the session.
        main.v1.openapi_schema = original_schema

    assert response.status_code == 404
    assert response.json() == {"detail": "Route-level missing resource"}


def test_cost_estimate_workflow_id_openapi_parameter_is_valid(client):
    response = client.get("/v1/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    parameters = schema["paths"]["/cost-estimate"]["get"]["parameters"]
    workflow_id = next(param for param in parameters if param["name"] == "workflow_id")

    assert "examples" not in workflow_id
    assert workflow_id["schema"]["enum"] == [
        "regime_analysis",
        "symbol_decision",
        "stim_forecast_review",
        "portfolio_build",
        "portfolio_compare_review",
    ]


def test_openapi_exposes_service_level_agent_guidance(client):
    main.v1.openapi_schema = None
    response = client.get("/v1/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["openapi"].startswith("3.")

    info = schema["info"]
    assert info["description"] == main.APP_DESCRIPTION
    assert info["description"].strip()
    assert info["contact"]["email"] == "api@stocktrends.com"
    assert schema["externalDocs"]["url"] == "https://developer.stocktrends.com/"

    guidance = info["x-guidance"]
    assert isinstance(guidance, str)
    assert 75 <= len(guidance.split()) <= 250
    assert "ST-IM (Stock Trends Inference Model)" in guidance

    guidance_lower = guidance.lower()
    for semantic_anchor in (
        "processed",
        "raw price data",
        "probabilistic inference",
        "forward-return distributions",
        "provider-agnostic inference contract",
        "current baseline inference provider",
        "conditional historical tendencies",
        "uncertainty",
        "symbol_exchange",
        "/v1/ai/tools",
        "/v1/workflows",
        "/v1/meta/inference",
        "/v1/meta/stim",
        "/v1/pricing/catalog",
        "/v1/cost-estimate",
        "accepted payment methods",
    ):
        assert semantic_anchor in guidance_lower


def test_openapi_preserves_stim_latest_operation_documentation(client):
    response = client.get("/v1/openapi.json")

    assert response.status_code == 200
    stim_latest = response.json()["paths"]["/stim/latest"]["get"]
    summary = stim_latest["summary"]
    assert summary.strip()
    assert "ST-IM" in summary
    assert "return distributions" in summary.lower()

    description = stim_latest["description"]
    assert description.strip()
    for semantic_anchor in (
        "Stock Trends Inference Model (ST-IM)",
        "4-week",
        "13-week",
        "40-week",
        "uncertainty",
        "staleness detection",
        "/v1/meta/inference",
        "/v1/meta/stim",
        "/v1/pricing/catalog",
    ):
        assert semantic_anchor in description


def test_openapi_exposes_inference_cognition_extensions(client):
    response = client.get("/v1/openapi.json")

    assert response.status_code == 200
    schema = response.json()

    meta_inference = schema["paths"]["/meta/inference"]["get"]
    assert meta_inference["x-stocktrends-cognition-contract"] == "provider_agnostic_inference_contract"
    assert meta_inference["x-stocktrends-inference-provider-agnostic"] is True

    stim_latest = schema["paths"]["/stim/latest"]["get"]
    assert stim_latest["x-stocktrends-inference-provider"] == "stim"
    assert stim_latest["x-stocktrends-inference-provider-role"] == "current_baseline_inference_provider"
    assert stim_latest["x-stocktrends-not-final-intelligence-layer"] is True
    assert stim_latest["x-stocktrends-inference-contract"] == "/v1/meta/inference"


def test_root_openapi_aliases_canonical_v1_contract(client):
    main.v1.openapi_schema = None

    root_response = client.get("/openapi.json")
    v1_response = client.get("/v1/openapi.json")

    assert root_response.status_code == 200
    assert v1_response.status_code == 200

    root_schema = root_response.json()
    v1_schema = v1_response.json()
    assert root_schema == v1_schema

    info = root_schema["info"]
    assert info["description"] == main.APP_DESCRIPTION
    assert info["x-guidance"].strip()
    assert info["contact"]["email"] == "api@stocktrends.com"
    assert root_schema["externalDocs"]["url"] == "https://developer.stocktrends.com/"
    assert root_schema["servers"][0]["url"] == "/v1"

    paths = root_schema["paths"]
    for expected_path in (
        "/stim/latest",
        "/ai/tools",
        "/workflows",
        "/decision/evaluate-symbol",
        "/portfolio/construct",
    ):
        assert expected_path in paths

    assert "/health" not in paths
    assert len(paths) > 1


@pytest.mark.parametrize(
    ("docs_path", "openapi_path"),
    [
        ("/docs", "/openapi.json"),
        ("/v1/docs", "/v1/openapi.json"),
    ],
)
def test_docs_load_and_reference_expected_canonical_schema(client, docs_path, openapi_path):
    response = client.get(docs_path)

    assert response.status_code == 200
    assert f"url: '{openapi_path}'" in response.text


def test_openapi_and_ai_tools_agree_on_target_get_parameter_locations(client):
    response = client.get("/v1/openapi.json")
    assert response.status_code == 200
    openapi = response.json()

    tools_response = client.get("/v1/ai/tools")
    assert tools_response.status_code == 200
    tools = {
        (tool["endpoint"], tool["method"]): tool
        for tool in tools_response.json()["tools"]
    }

    expected_parameters = {
        "/v1/stim/latest": "symbol_exchange",
        "/v1/stim/history": "symbol_exchange",
        "/v1/indicators/latest": "symbol_exchange",
        "/v1/indicators/history": "symbol_exchange",
        "/v1/prices/latest": "symbol_exchange",
        "/v1/prices/history": "symbol_exchange",
        "/v1/stwr/reports/latest": "rpt",
        "/v1/stwr/reports/history": "rpt",
    }

    def resolve_openapi_parameter(param: dict) -> dict:
        if "$ref" not in param:
            return param
        ref = param["$ref"].removeprefix("#/")
        resolved = openapi
        for part in ref.split("/"):
            resolved = resolved[part]
        return resolved

    for endpoint, param_name in expected_parameters.items():
        openapi_path = endpoint.removeprefix("/v1")
        openapi_params = {
            resolved_param["name"]: resolved_param
            for param in openapi["paths"][openapi_path]["get"]["parameters"]
            for resolved_param in (resolve_openapi_parameter(param),)
        }
        tool = tools[(endpoint, "GET")]
        tool_params = {param["name"]: param for param in tool["parameters"]}

        assert openapi_params[param_name]["in"] == "query"
        assert tool["input_location"] == "query"
        assert tool["parameter_source"] == "query"
        assert tool_params[param_name]["in"] == "query"
        assert tool_params[param_name]["parameter_source"] == "query"


def test_unauthenticated_unknown_v1_path_still_returns_401(client):
    response = client.get("/v1/missing-route")

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing API key"}
