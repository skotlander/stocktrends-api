from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

import main
import middleware.api_key as api_key_module
import middleware.metering as metering_module
import payments.policy_provider as policy_provider
import pricing.classifier as classifier_module
import routers.stocktrends_portfolios as portfolios_router


_ROWS = [
    {
        "port_id": 1,
        "name": "ST-IM Select Portfolio",
        "strategy_id": 3,
        "exchanges": "N,Q",
        "index_symbols": "STIM",
        "description": "Live ST-IM strategy portfolio.",
        "status": 1,
        "web_content": "<p>legacy html should not leak</p>",
    },
    {
        "port_id": 2,
        "name": "TSX 60 Portfolio",
        "strategy_id": 4,
        "exchanges": "T",
        "index_symbols": "SPTX60",
        "description": "Live Canadian strategy portfolio.",
        "status": 1,
        "web_content": "<p>legacy html should not leak</p>",
    },
    {
        "port_id": 99,
        "name": "Inactive Test Portfolio",
        "strategy_id": 8,
        "exchanges": "N",
        "index_symbols": "DJI",
        "description": "Inactive test record.",
        "status": 0,
        "web_content": "<p>inactive html should not leak</p>",
    },
]

_RETURNS_ROWS = [
    {
        "port_id": 2,
        "weekdate": date(2024, 1, 5),
        "return_pct": Decimal("1.23"),
        "value": Decimal("12345.67"),
        "private_note": "legacy return audit note should not leak",
    },
    {
        "port_id": 2,
        "weekdate": date(2024, 1, 12),
        "return_pct": Decimal("-0.45"),
        "value": Decimal("12290.12"),
        "private_note": "legacy return audit note should not leak",
    },
    {
        "port_id": 1,
        "weekdate": date(2024, 1, 5),
        "return_pct": Decimal("0.88"),
        "value": Decimal("10088.00"),
        "private_note": "legacy return audit note should not leak",
    },
    {
        "port_id": 99,
        "weekdate": date(2024, 1, 5),
        "return_pct": Decimal("9.99"),
        "value": Decimal("999.99"),
        "private_note": "inactive portfolio returns should not leak",
    },
]


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Connection:
    def __init__(
        self,
        portfolio_rows: list[dict[str, Any]],
        return_rows: list[dict[str, Any]],
        executed: list[tuple[str, dict[str, Any]]],
    ):
        self._portfolio_rows = portfolio_rows
        self._return_rows = return_rows
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params: dict[str, Any] | None = None):
        params = params or {}
        sql = str(statement)
        self._executed.append((sql, params))

        if "FROM stp_returnslog" in sql:
            rows = [row for row in self._return_rows if row["port_id"] == params.get("port_id")]
            return _Result(rows)

        rows = [row for row in self._portfolio_rows if row["status"] == 1]
        if "port_id" in params:
            rows = [row for row in rows if row["port_id"] == params["port_id"]]
        return _Result(rows)


class _Engine:
    def __init__(
        self,
        portfolio_rows: list[dict[str, Any]],
        return_rows: list[dict[str, Any]],
    ):
        self.portfolio_rows = portfolio_rows
        self.return_rows = return_rows
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def connect(self):
        return _Connection(self.portfolio_rows, self.return_rows, self.executed)


class _FailingEngine:
    def connect(self):
        raise RuntimeError("SELECT failed: private schema detail")


def _fake_authenticate(self, path: str, raw_key: str):
    return True, {
        "api_key_id": "test-key-id",
        "customer_id": "test-customer-id",
        "subscription_id": "test-subscription-id",
        "plan_code": "pro",
        "actor_type": "external_customer",
        "monthly_quota": 1000,
    }


def _assert_no_payment_challenge(response):
    assert response.headers.get("x-stocktrends-payment-required") == "false"
    assert response.headers.get("x-stocktrends-accepted-payment-methods") == "none"
    assert response.headers.get("x-stocktrends-pricing-rule") == "default_free"
    assert "payment-required" not in response.headers


@pytest.fixture
def portfolio_engine(monkeypatch):
    engine = _Engine(_ROWS, _RETURNS_ROWS)
    monkeypatch.setattr(portfolios_router, "get_engine", lambda: engine)
    monkeypatch.setattr(portfolios_router, "text", lambda sql: sql)
    return engine


@pytest.fixture
def protected_client(monkeypatch, portfolio_engine):
    monkeypatch.setattr(api_key_module.ApiKeyMiddleware, "_authenticate_api_key", _fake_authenticate)
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda *a, **kw: None)
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *a, **kw: None)
    monkeypatch.setattr(metering_module, "log_api_request_economics", lambda *a, **kw: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda *_args, **_kwargs: (Decimal("0"), Decimal("0"), Decimal("0")),
    )
    monkeypatch.setattr(api_key_module, "_ENABLE_AGENT_PAY", False)
    monkeypatch.setattr(metering_module, "ENABLE_AGENT_PAY", False)
    monkeypatch.setattr(metering_module, "ENFORCE_AGENT_PAY", False)
    policy_provider._cached_config = None
    policy_provider._cached_at = 0.0
    policy_provider._last_known_good_config = None
    policy_provider._last_reported_fallback_reason = None

    with TestClient(main.app) as client:
        yield client


def test_portfolio_list_returns_only_live_records_and_expected_fields(protected_client, portfolio_engine):
    response = protected_client.get("/v1/stocktrends/portfolios")

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["count"] == 2
    assert [row["port_id"] for row in body["data"]] == [1, 2]

    first = body["data"][0]
    assert first == {
        "port_id": 1,
        "name": "ST-IM Select Portfolio",
        "strategy_id": 3,
        "exchanges": "N,Q",
        "selection_universe": "STIM",
        "description": "Live ST-IM strategy portfolio.",
        "status": 1,
    }
    assert "index_symbols" not in first
    assert "web_content" not in first

    executed_sql = portfolio_engine.executed[0][0]
    assert "FROM stp_ports" in executed_sql
    assert "WHERE status = 1" in executed_sql
    assert "web_content" not in executed_sql


def test_portfolio_detail_returns_live_portfolio(protected_client, portfolio_engine):
    response = protected_client.get("/v1/stocktrends/portfolios/2")

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["data"]["port_id"] == 2
    assert body["data"]["selection_universe"] == "SPTX60"
    assert "index_symbols" not in body["data"]
    assert "web_content" not in body["data"]
    executed_sql = portfolio_engine.executed[0][0]
    assert "FROM stp_ports" in executed_sql
    assert "WHERE port_id = :port_id" in executed_sql
    assert "AND status = 1" in executed_sql
    assert "web_content" not in executed_sql


@pytest.mark.parametrize("port_id", [404, 99])
def test_portfolio_detail_returns_404_for_missing_or_inactive(protected_client, port_id):
    response = protected_client.get(f"/v1/stocktrends/portfolios/{port_id}")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "portfolio_not_found"
    assert response.json()["detail"]["port_id"] == port_id
    _assert_no_payment_challenge(response)


def test_portfolio_returns_history_is_public_and_uses_returns_log(protected_client, portfolio_engine):
    response = protected_client.get("/v1/stocktrends/portfolios/2/returns")

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["port_id"] == 2
    assert body["portfolio"] == {
        "port_id": 2,
        "name": "TSX 60 Portfolio",
        "selection_universe": "SPTX60",
    }
    assert body["count"] == 2
    assert body["returns"] == [
        {
            "weekdate": "2024-01-05",
            "return_pct": 1.23,
            "value": 12345.67,
        },
        {
            "weekdate": "2024-01-12",
            "return_pct": -0.45,
            "value": 12290.12,
        },
    ]
    assert "private_note" not in str(body)

    executed_sql = "\n".join(sql for sql, _params in portfolio_engine.executed)
    assert "FROM stp_ports" in executed_sql
    assert "AND status = 1" in executed_sql
    assert "FROM stp_returnslog" in executed_sql
    assert "ORDER BY weekdate ASC" in executed_sql
    assert "stp_positions" not in executed_sql


@pytest.mark.parametrize("port_id", [404, 99])
def test_portfolio_returns_history_returns_404_for_missing_or_inactive(protected_client, port_id):
    response = protected_client.get(f"/v1/stocktrends/portfolios/{port_id}/returns")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "portfolio_not_found"
    assert response.json()["detail"]["port_id"] == port_id
    _assert_no_payment_challenge(response)


def test_portfolio_returns_access_classification_layers_are_public_free():
    path = "/v1/stocktrends/portfolios/2/returns"

    decision = classifier_module.classify_request(
        path=path,
        method="GET",
        has_paid_auth=False,
        payment_method_header=None,
        plan_code=None,
        agent_identifier=None,
    )
    accepted = policy_provider.get_accepted_payment_methods_for_path(
        path,
        decision.log_pricing_rule_id,
        method="GET",
    )

    assert policy_provider.get_effective_endpoint_payment_policy(path, "GET") is None
    assert policy_provider.is_public_stocktrends_portfolio_returns_path(path)
    assert decision.access_granted is True
    assert decision.is_metered == 0
    assert decision.econ_payment_required == 0
    assert accepted == "none"


def test_future_stocktrends_portfolio_child_paths_are_not_public_bypasses(protected_client):
    response = protected_client.get("/v1/stocktrends/portfolios/2/positions")

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing API key"}


def test_stocktrends_portfolio_metadata_routes_remain_read_only(protected_client):
    response = protected_client.post("/v1/stocktrends/portfolios")

    assert response.status_code == 405
    _assert_no_payment_challenge(response)


def test_portfolio_detail_db_errors_use_caller_safe_message(protected_client, monkeypatch):
    monkeypatch.setattr(portfolios_router, "get_engine", lambda: _FailingEngine())

    response = protected_client.get(
        "/v1/stocktrends/portfolios/2",
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["error"] == "db_query_failed"
    assert detail["message"] == "Database query failed."
    assert "private schema detail" not in str(detail)


def test_stocktrends_portfolio_endpoints_appear_in_openapi(protected_client):
    response = protected_client.get("/v1/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    paths = schema["paths"]
    assert "/stocktrends/portfolios" in paths
    assert "/stocktrends/portfolios/{port_id}" in paths
    assert "/stocktrends/portfolios/{port_id}/returns" in paths
    assert "Official Stock Trends model portfolios" in paths["/stocktrends/portfolios"]["get"]["description"]
    assert "Official Stock Trends model portfolio" in paths["/stocktrends/portfolios/{port_id}"]["get"]["description"]
    returns_description = paths["/stocktrends/portfolios/{port_id}/returns"]["get"]["description"]
    assert "Official Stock Trends portfolio returns history" in returns_description
    assert "stp_returnslog" not in returns_description
