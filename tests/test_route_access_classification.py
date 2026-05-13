from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import main
import middleware.api_key as api_key_module
import middleware.metering as metering_module
import pricing.classifier as classifier_module
import routers.instruments as instruments_router
import routers.workflows as workflows_router
from payments.enforcement import PaymentEnforcementResult
from routers.workflows import WORKFLOW_REGISTRY


class _Result:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


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

    def connect(self):
        return _Connection(self._rows)


def _stub_metering_side_effects(monkeypatch, *, cost: Decimal = Decimal("0")):
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *a, **kw: None)
    monkeypatch.setattr(metering_module, "log_api_request_economics", lambda *a, **kw: None)
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda *_args, **_kwargs: (cost, cost, cost),
    )


def _workflow_costs(cost: float = 0.25) -> dict[str, float]:
    return {
        step["pricing_rule_id"]: cost
        for workflow in WORKFLOW_REGISTRY
        for step in workflow["steps"]
    }


@pytest.fixture
def public_helper_client(monkeypatch):
    _stub_metering_side_effects(monkeypatch)
    monkeypatch.setattr(workflows_router, "_fetch_active_pricing_costs", lambda: _workflow_costs())
    monkeypatch.setattr(
        instruments_router,
        "get_market_engine",
        lambda: _Engine(
            [
                {
                    "symbol": "IBM",
                    "exchange": "N",
                    "type": "CS",
                    "currency": "USD",
                    "name": "International Business Machines",
                    "shortname": "IBM",
                    "gm_industry_id": "tech",
                    "x_sector_name": "Technology",
                }
            ]
        ),
    )

    with TestClient(main.app) as client:
        yield client


@pytest.mark.parametrize(
    "path",
    [
        "/v1/cost-estimate?workflow_id=portfolio_build",
        "/v1/instruments/lookup?symbol=IBM&cs_only=true",
        "/v1/instruments/resolve?symbol_exchange=IBM-N",
        "/v1/stwr/reports/catalog",
        "/v1/meta/indicators",
        "/v1/meta/stim",
        "/v1/meta/stwr",
        "/v1/leadership/definitions",
        "/v1/ai/context",
    ],
)
def test_public_planning_helpers_return_200_without_api_key(public_helper_client, path):
    response = public_helper_client.get(path)

    assert response.status_code == 200
    assert response.headers.get("x-stocktrends-payment-required") == "false"
    assert "payment-required" not in response.headers


@pytest.mark.parametrize(
    "path",
    [
        "/v1/instruments/lookup?symbol=IBM&cs_only=true",
        "/v1/cost-estimate?workflow_id=portfolio_build",
    ],
)
def test_public_helper_bypass_uses_path_without_query_string(public_helper_client, path):
    response = public_helper_client.get(path)

    assert response.status_code == 200
    assert response.headers.get("x-stocktrends-payment-required") == "false"
    assert "payment-required" not in response.headers


def _challenge_result(path: str) -> PaymentEnforcementResult:
    return PaymentEnforcementResult(
        outcome="challenge",
        challenge_body={
            "error": "payment_required",
            "detail": "Payment is required to access this endpoint.",
            "protocol": "x402",
            "resource": path,
            "pricing": {"amount_usd": "0.250000", "unit": "request"},
            "accepted_payment_methods": ["x402"],
            "payment_required": {"x402Version": 2, "accepts": []},
        },
        payment_required_header="eyJ0ZXN0Ijp0cnVlfQ==",
        payment_network="eip155:8453",
        payment_token="0xtoken",
    )


@pytest.fixture
def agent_pay_client(monkeypatch):
    _stub_metering_side_effects(monkeypatch, cost=Decimal("0.25"))
    monkeypatch.setattr(api_key_module, "_ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "ENFORCE_AGENT_PAY", True)
    monkeypatch.setattr(classifier_module, "ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(classifier_module, "ENFORCE_AGENT_PAY", True)
    monkeypatch.setattr(
        metering_module,
        "enforce_payment_rail",
        lambda **kwargs: _challenge_result(kwargs["path"]),
    )

    with TestClient(main.app) as client:
        yield client


@pytest.mark.parametrize(
    ("path", "pricing_rule_id"),
    [
        ("/v1/breadth/sector/latest", "breadth_sector_latest_paid"),
        ("/v1/leadership/summary/latest", "leadership_summary_latest_paid"),
        ("/v1/leadership/rotation/history", "leadership_rotation_history_paid"),
    ],
)
def test_paid_agent_pay_endpoints_return_402_without_api_key_or_payment(
    agent_pay_client,
    path,
    pricing_rule_id,
):
    response = agent_pay_client.get(path)

    assert response.status_code == 402
    assert response.headers["x-stocktrends-pricing-rule"] == pricing_rule_id
    assert response.headers["x-stocktrends-payment-required"] == "true"
    assert response.headers["x-stocktrends-accepted-payment-methods"] == "subscription,x402,mpp"
    body = response.json()
    assert body["accepted_payment_methods"] == ["subscription", "x402", "mpp"]
    assert body["stocktrends_preview"]["pricing"]["pricing_rule_id"] == pricing_rule_id
