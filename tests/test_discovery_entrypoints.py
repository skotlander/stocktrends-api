from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

# Module stubs for sqlalchemy/db/etc. are provided by tests/conftest.py.
import main
import middleware.api_key as api_key_module
import middleware.metering as metering_module


def _stub_runtime_side_effects(monkeypatch):
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(metering_module, "log_api_request_economics", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda *args, **kwargs: (Decimal("0"), Decimal("0"), Decimal("0")),
    )
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda *args, **kwargs: None)


def _expected_not_found_payload(path: str) -> dict[str, str]:
    return {
        "detail": "Not Found",
        "requested_path": path,
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


def test_root_guides_to_ai_tools_first(client):
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Start with the machine-readable tools manifest for agent discovery."
    assert "Autonomous portfolio intelligence API for AI agents" in body["description"]
    assert body["start_here"] == "/v1/ai/tools"
    assert body["secondary"] == "/v1/ai/context"
    assert body["docs"] == "/v1/docs"
    assert body["openapi"] == "/v1/openapi.json"
    assert "/v1/workflows" in body["planning_helpers"]


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

    @main.v1.get("/_test-route-level-404")
    def route_level_404():
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Route-level missing resource")

    response = client.get("/v1/_test-route-level-404", headers={"X-API-Key": "test-key"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Route-level missing resource"}


def test_unauthenticated_unknown_v1_path_still_returns_401(client):
    response = client.get("/v1/missing-route")

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing API key"}
