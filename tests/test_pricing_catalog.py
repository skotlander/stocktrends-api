from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

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
    rows = [
        _row("intelligence_guidance_latest", "/v1/intelligence/guidance/latest", Decimal("0.25")),
        _row("intelligence_guidance_by_id", "/v1/intelligence/guidance/{artifact_id}", Decimal("0.25")),
        _row("intelligence_research_latest", "/v1/intelligence/research/latest", Decimal("0.50")),
        _row("intelligence_research_by_id", "/v1/intelligence/research/{artifact_id}", Decimal("0.50")),
    ]
    monkeypatch.setattr(pricing_router, "get_metering_engine", lambda: _Engine(rows))

    response = pricing_router.get_pricing_catalog(SimpleNamespace(state=SimpleNamespace(request_id="req_test")))
    body = json.loads(response.body)
    rules = {rule["pricing_rule_id"]: rule for rule in body["rules"]}

    expected_costs = {
        "intelligence_guidance_latest": 0.25,
        "intelligence_guidance_by_id": 0.25,
        "intelligence_research_latest": 0.5,
        "intelligence_research_by_id": 0.5,
    }
    assert expected_costs.keys() <= rules.keys()

    for rule_id, expected_cost in expected_costs.items():
        rule = rules[rule_id]
        assert rule["endpoint_family"] == "intelligence"
        assert rule["access_type"] == "paid"
        assert rule["cost_per_request"] == expected_cost
        assert rule["stc_cost"] == expected_cost
        assert rule["estimated_usd_cost"] == expected_cost
        assert rule["requires_payment"] is True
        assert rule["requires_subscription"] is False
        assert rule["supported_rails"] == ["subscription", "x402", "mpp"]
        assert "STC is the pricing source of truth" in rule["pricing_note"]
