from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import routers.pricing as pricing_router
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


def _workflow_rule_rows(cost: Decimal = Decimal("0.25")) -> list[dict]:
    rule_ids = {
        step["pricing_rule_id"]
        for workflow in WORKFLOW_REGISTRY
        for step in workflow["steps"]
        if step.get("pricing_rule_id")
    }
    return [
        {
            "rule_name": rule_id,
            "cost_per_request": cost,
        }
        for rule_id in sorted(rule_ids)
    ]


def test_pricing_catalog_exposes_concrete_paid_endpoint_pricing(monkeypatch):
    rows = [
        {
            "rule_name": "indicators_latest_paid",
            "endpoint_pattern": "/v1/indicators/latest",
            "endpoint_family": "indicators",
            "api_version": "v1",
            "access_type": "paid",
            "cost_per_request": Decimal("0.0035"),
            "cost_unit": "STC",
            "requires_subscription": False,
            "requires_payment": True,
        },
        {
            "rule_name": "indicators_history_paid",
            "endpoint_pattern": "/v1/indicators/history",
            "endpoint_family": "indicators",
            "api_version": "v1",
            "access_type": "paid",
            "cost_per_request": Decimal("0.0100"),
            "cost_unit": "STC",
            "requires_subscription": False,
            "requires_payment": True,
        },
    ]
    monkeypatch.setattr(pricing_router, "get_metering_engine", lambda: _Engine(rows))

    request = SimpleNamespace(state=SimpleNamespace(request_id="req_test"))
    response = pricing_router.get_pricing_catalog(request)
    body = json.loads(response.body)

    assert body["planning_role"]["unit"] == "STC"
    assert "budget" in body["planning_role"]["purpose"]
    assert body["planning_role"]["public_behavior_note"]
    rules = {rule["pricing_rule_id"]: rule for rule in body["rules"]}
    assert rules["indicators_latest_paid"]["cost_per_request"] == 0.0035
    assert rules["indicators_latest_paid"]["stc_cost"] == 0.0035
    assert rules["indicators_latest_paid"]["estimated_usd_cost"] == 0.0035
    assert set(rules["indicators_latest_paid"]["supported_rails"]) == {"subscription", "x402", "mpp"}
    assert "STC is the pricing source of truth" in rules["indicators_latest_paid"]["pricing_note"]
    assert rules["indicators_latest_paid"]["requires_payment"] is True
    assert rules["indicators_history_paid"]["endpoint_pattern"] == "/v1/indicators/history"


def test_pricing_metadata_marks_pricing_as_planning_infrastructure():
    body = pricing_router.get_pricing()
    assert body["planning_role"]["related_planning_endpoints"]["pricing_catalog"] == "/v1/pricing/catalog"
    assert body["planning_role"]["related_planning_endpoints"]["workflow_registry"] == "/v1/workflows"
    assert body["payment_identity"]["supported_methods"] == ["subscription", "mpp", "x402"]
    assert "STOK does not replace STC" in body["payment_identity"]["rail_guidance"]["stok"]


def test_workflows_expose_machine_plannable_steps(monkeypatch):
    monkeypatch.setattr(
        workflows_router,
        "get_metering_engine",
        lambda: _Engine(_workflow_rule_rows(Decimal("0.25"))),
    )

    response = workflows_router.get_workflows()
    body = json.loads(response.body)
    workflows = body["workflows"]

    assert body["recommended_starting_workflow"]["workflow_id"] == "portfolio_build"
    assert "mpp" in body["agent_guidance"]["payment_rails"]
    assert workflows
    for workflow in workflows:
        assert workflow["workflow_id"]
        assert workflow["decision_guidance"]
        assert workflow["best_for"]
        assert workflow["agent_goal_examples"]
        assert workflow["next_step_guidance"]
        assert "mpp" in workflow["supported_rails"]
        assert workflow["total_stc_cost"] > 0
        assert workflow["total_estimated_usd_cost"] == workflow["total_stc_cost"]
        assert "1 STC" in workflow["pricing_note"]
        for step in workflow["steps"]:
            for field in (
                "method",
                "path",
                "pricing_rule_id",
                "stc_cost",
                "estimated_usd_cost",
                "supported_rails",
                "pricing_note",
                "purpose",
                "safe_example_request",
                "output_summary",
            ):
                assert field in step, f"{workflow['workflow_id']} step missing {field}"
            assert step["safe_example_request"]["method"] == step["method"]
            assert step["safe_example_request"]["path"] == step["path"]


def test_stim_forecast_review_workflow_has_meta_before_paid_stim(monkeypatch):
    monkeypatch.setattr(
        workflows_router,
        "get_metering_engine",
        lambda: _Engine(_workflow_rule_rows(Decimal("0.25"))),
    )

    response = workflows_router.get_workflows()
    body = json.loads(response.body)
    workflow = next(
        workflow for workflow in body["workflows"] if workflow["workflow_id"] == "stim_forecast_review"
    )
    paths = [step["path"] for step in workflow["steps"]]

    assert paths.index("/v1/meta/inference") < paths.index("/v1/meta/stim")
    assert paths.index("/v1/meta/stim") < paths.index("/v1/stim/latest")
    assert workflow["interpretation_guidance"]
    assert "/v1/meta/inference" in workflow["interpretation_guidance"]
    assert "base_period_mean_returns_pct" in workflow["interpretation_guidance"]
    assert workflow["required_interpretation_steps"]

    meta_step = workflow["steps"][0]
    assert meta_step["path"] == "/v1/meta/inference"
    assert meta_step["pricing_rule_id"] is None
    assert meta_step["stc_cost"] == 0.0


def test_portfolio_workflow_post_steps_include_schema_examples(monkeypatch):
    monkeypatch.setattr(
        workflows_router,
        "get_metering_engine",
        lambda: _Engine(_workflow_rule_rows(Decimal("0.25"))),
    )

    response = workflows_router.get_workflows()
    body = json.loads(response.body)
    portfolio_workflow = next(
        workflow for workflow in body["workflows"] if workflow["workflow_id"] == "portfolio_compare_review"
    )
    post_steps = [step for step in portfolio_workflow["steps"] if step["method"] == "POST"]

    assert post_steps
    for step in post_steps:
        assert "json" in step["safe_example_request"]
        assert step["required_inputs"] or step["optional_inputs"]

    compare_step = next(step for step in post_steps if step["path"] == "/v1/portfolio/compare")
    assert compare_step["safe_example_request"]["json"] == {
        "left": [{"symbol_exchange": "IBM-N", "weight": 1.0}],
        "right": [{"symbol_exchange": "MSFT-Q", "weight": 1.0}],
    }
