from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

import routers.pricing as pricing_router


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


def _row(rule_name: str, endpoint_pattern: str, cost: Decimal) -> dict:
    return {
        "rule_name": rule_name,
        "endpoint_pattern": endpoint_pattern,
        "endpoint_family": "intelligence",
        "api_version": "v1",
        "access_type": "paid",
        "cost_per_request": cost,
        "cost_unit": "STC",
        "requires_subscription": False,
        "requires_payment": True,
    }


def test_pricing_catalog_includes_paid_intelligence_products(monkeypatch):
    # Distinct fixture amounts verify that each rule is sourced from its row.
    rows = [
        _row("intelligence_guidance_latest", "/v1/intelligence/guidance/latest", Decimal("1.17")),
        _row("intelligence_guidance_by_id", "/v1/intelligence/guidance/{artifact_id}", Decimal("1.23")),
        _row("intelligence_research_latest", "/v1/intelligence/research/latest", Decimal("2.67")),
        _row("intelligence_research_by_id", "/v1/intelligence/research/{artifact_id}", Decimal("2.89")),
    ]
    monkeypatch.setattr(pricing_router, "get_metering_engine", lambda: _Engine(rows))

    response = pricing_router.get_pricing_catalog(SimpleNamespace(state=SimpleNamespace(request_id="req_test")))
    body = json.loads(response.body)
    rules = {rule["pricing_rule_id"]: rule for rule in body["rules"]}

    for row in rows:
        rule = rules[row["rule_name"]]
        expected_cost = float(row["cost_per_request"])
        assert rule["endpoint_family"] == "intelligence"
        assert rule["access_type"] == "paid"
        assert rule["cost_per_request"] == expected_cost
        assert rule["stc_cost"] == expected_cost
        assert rule["estimated_usd_cost"] == expected_cost
        assert rule["requires_payment"] is True
        assert rule["requires_subscription"] is False
        assert rule["supported_rails"] == ["subscription", "x402", "mpp"]
        assert "STC is the pricing source of truth" in rule["pricing_note"]


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "unused"}], ids=["anonymous", "with-key"])
def test_pricing_catalog_is_public_and_non_billable(payment_harness, monkeypatch, headers):
    monkeypatch.setattr(pricing_router, "get_metering_engine", lambda: _Engine([]))

    response = payment_harness.client.get("/v1/pricing/catalog", headers=headers)

    assert response.status_code == 200
    assert response.headers["x-stocktrends-pricing-rule"] == "default_free"
    assert response.headers["x-stocktrends-payment-required"] == "false"
    assert response.headers["x-stocktrends-accepted-payment-methods"] == "none"
    assert "x-stocktrends-quota-limit" not in response.headers
    assert "payment-required" not in response.headers

    event = payment_harness.logs.only_event_row()
    assert event["pricing_rule_id"] == "default_free"
    assert event["is_metered"] == 0
    assert event["is_billable"] == 0
    assert event["payment_rail"] == "none"
    assert event["subscription_id"] is None
    assert not payment_harness.logs.economics
