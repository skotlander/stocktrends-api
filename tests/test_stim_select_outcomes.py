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
import routers.selections as selections_router


_OUTCOME_ROWS = [
    {
        "weekdate": date(2024, 1, 5),
        "exchange": "N",
        "symbol": "AAA",
        "x4wk1": Decimal("0.4"),
        "x13wk1": Decimal("2.5"),
        "x40wk1": Decimal("7.0"),
        "x13wk": Decimal("6.0"),
        "x13wksd": Decimal("2.0"),
        "price": Decimal("12.0"),
        "volume": 5000,
        "fpr_chg13": Decimal("5.0"),
    },
    {
        "weekdate": date(2024, 1, 5),
        "exchange": "N",
        "symbol": "BBB",
        "x4wk1": Decimal("0.2"),
        "x13wk1": Decimal("2.4"),
        "x40wk1": Decimal("6.8"),
        "x13wk": Decimal("3.0"),
        "x13wksd": Decimal("1.0"),
        "price": Decimal("8.0"),
        "volume": 2500,
        "fpr_chg13": Decimal("-1.0"),
    },
    {
        "weekdate": date(2024, 1, 12),
        "exchange": "Q",
        "symbol": "CCC",
        "x4wk1": Decimal("0.8"),
        "x13wk1": Decimal("3.0"),
        "x40wk1": Decimal("7.4"),
        "x13wk": Decimal("8.0"),
        "x13wksd": Decimal("1.5"),
        "price": Decimal("20.0"),
        "volume": 7000,
        "fpr_chg13": Decimal("10.0"),
    },
    {
        "weekdate": date(2024, 1, 12),
        "exchange": "Q",
        "symbol": "NULL13",
        "x4wk1": Decimal("0.8"),
        "x13wk1": Decimal("3.0"),
        "x40wk1": Decimal("7.4"),
        "x13wk": Decimal("9.0"),
        "x13wksd": Decimal("1.0"),
        "price": Decimal("20.0"),
        "volume": 7000,
        "fpr_chg13": None,
    },
    {
        "weekdate": date(2024, 1, 19),
        "exchange": "T",
        "symbol": "EEE",
        "x4wk1": Decimal("0.3"),
        "x13wk1": Decimal("2.8"),
        "x40wk1": Decimal("7.0"),
        "x13wk": Decimal("4.0"),
        "x13wksd": Decimal("1.2"),
        "price": Decimal("6.0"),
        "volume": 3000,
        "fpr_chg13": Decimal("2.19"),
    },
    {
        "weekdate": date(2024, 1, 19),
        "exchange": "A",
        "symbol": "LOWPRICE",
        "x4wk1": Decimal("0.3"),
        "x13wk1": Decimal("2.8"),
        "x40wk1": Decimal("7.0"),
        "x13wk": Decimal("4.0"),
        "x13wksd": Decimal("1.2"),
        "price": Decimal("1.99"),
        "volume": 3000,
        "fpr_chg13": Decimal("99.0"),
    },
    {
        "weekdate": date(2024, 1, 19),
        "exchange": "A",
        "symbol": "LOWVOLUME",
        "x4wk1": Decimal("0.3"),
        "x13wk1": Decimal("2.8"),
        "x40wk1": Decimal("7.0"),
        "x13wk": Decimal("4.0"),
        "x13wksd": Decimal("1.2"),
        "price": Decimal("6.0"),
        "volume": 1000,
        "fpr_chg13": Decimal("99.0"),
    },
    {
        "weekdate": date(2024, 1, 19),
        "exchange": "A",
        "symbol": "LOWBOUND",
        "x4wk1": Decimal("0.3"),
        "x13wk1": Decimal("2.18"),
        "x40wk1": Decimal("7.0"),
        "x13wk": Decimal("4.0"),
        "x13wksd": Decimal("1.2"),
        "price": Decimal("6.0"),
        "volume": 3000,
        "fpr_chg13": Decimal("99.0"),
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
    def __init__(self, rows: list[dict[str, Any]], executed: list[tuple[str, dict[str, Any]]]):
        self._rows = rows
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def _qualifying_rows(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        base_13wk = Decimal(str(params.get("base_13wk", "2.19")))
        rows = []
        for row in self._rows:
            if row["x4wk1"] <= Decimal(str(params.get("base_4wk", "0"))):
                continue
            if row["x13wk1"] <= base_13wk:
                continue
            if row["x40wk1"] <= Decimal(str(params.get("base_40wk", "6.45"))):
                continue
            if row["price"] < Decimal(str(params.get("min_price", "2"))):
                continue
            if row["volume"] <= int(params.get("min_volume", 1000)):
                continue
            if row["fpr_chg13"] is None:
                continue
            if "start_date" in params and row["weekdate"] < params["start_date"]:
                continue
            if "end_date" in params and row["weekdate"] > params["end_date"]:
                continue
            if "exchange" in params and row["exchange"] != params["exchange"]:
                continue
            rows.append(row)

        if "limit_rank" not in params:
            return rows

        ranked: list[dict[str, Any]] = []
        for weekdate in sorted({row["weekdate"] for row in rows}):
            week_rows = [row for row in rows if row["weekdate"] == weekdate]

            def rank_score(item: dict[str, Any]) -> tuple[Decimal, Decimal, str, str]:
                sd = item["x13wksd"]
                score = Decimal("-999999999999") if sd == 0 else (item["x13wk"] - base_13wk) / sd
                return score, item["x13wk"], item["exchange"], item["symbol"]

            ranked.extend(
                sorted(week_rows, key=rank_score, reverse=True)[: int(params["limit_rank"])]
            )
        return ranked

    def execute(self, statement, params: dict[str, Any] | None = None):
        params = params or {}
        sql = str(statement)
        self._executed.append((sql, params))
        rows = self._qualifying_rows(params)
        values = [row["fpr_chg13"] for row in rows]

        if "outcome_count" in sql:
            return _Result(
                [
                    {
                        "outcome_count": len(rows),
                        "first_weekdate": min((row["weekdate"] for row in rows), default=None),
                        "latest_weekdate": max((row["weekdate"] for row in rows), default=None),
                        "average_fpr_chg13": (sum(values, Decimal("0")) / len(values)) if values else None,
                        "positive_return_count": sum(1 for value in values if value > 0),
                        "outperform_base_count": sum(1 for value in values if value > Decimal("2.19")),
                    }
                ]
            )

        return _Result([{"fpr_chg13": value} for value in values])


class _Engine:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def connect(self):
        return _Connection(self.rows, self.executed)


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


def _schema_has_date_format(schema: Any) -> bool:
    if isinstance(schema, dict):
        if schema.get("format") == "date":
            return True
        return any(_schema_has_date_format(value) for value in schema.values())
    if isinstance(schema, list):
        return any(_schema_has_date_format(value) for value in schema)
    return False


def _schema_has_key_value(schema: Any, key: str, value: Any) -> bool:
    if isinstance(schema, dict):
        if schema.get(key) == value:
            return True
        return any(_schema_has_key_value(item, key, value) for item in schema.values())
    if isinstance(schema, list):
        return any(_schema_has_key_value(item, key, value) for item in schema)
    return False


@pytest.fixture
def outcome_engine(monkeypatch):
    engine = _Engine(list(_OUTCOME_ROWS))
    monkeypatch.setattr(selections_router, "get_engine", lambda: engine)
    monkeypatch.setattr(selections_router, "text", lambda sql: sql)
    return engine


@pytest.fixture
def client(monkeypatch, outcome_engine):
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

    with TestClient(main.app) as test_client:
        yield test_client


def test_stim_select_outcomes_summary_returns_metrics(client):
    response = client.get("/v1/selections/stim-select/outcomes/summary")

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()

    assert body["signal"]["signal_id"] == "stim_select"
    assert body["signal"]["criteria"]["x13wk1_gt"] == 2.19
    assert body["signal"]["base_period_mean_13wk"] == 2.19
    assert body["filters"] == {
        "start_date": None,
        "end_date": None,
        "exchange": None,
        "limit_rank": None,
    }

    outcomes = body["outcomes"]
    assert outcomes["horizon"] == "13w"
    assert outcomes["count"] == 4
    assert outcomes["first_weekdate"] == "2024-01-05"
    assert outcomes["latest_weekdate"] == "2024-01-19"
    assert outcomes["average_fpr_chg13"] == pytest.approx(4.0475)
    assert outcomes["median_fpr_chg13"] == pytest.approx(3.595)
    assert outcomes["positive_return_count"] == 3
    assert outcomes["positive_return_rate"] == pytest.approx(0.75)
    assert outcomes["outperform_base_count"] == 2
    assert outcomes["outperform_base_rate"] == pytest.approx(0.5)
    assert outcomes["base_period_mean_13wk"] == 2.19


def test_stim_select_outcomes_summary_uses_mature_fpr_chg13_only(client):
    response = client.get("/v1/selections/stim-select/outcomes/summary")

    body_text = response.text
    assert response.json()["outcomes"]["count"] == 4
    assert "NULL13" not in body_text
    assert "fpr_chg13" in body_text


def test_stim_select_outcomes_summary_empty_result_is_safe(client):
    response = client.get("/v1/selections/stim-select/outcomes/summary?exchange=B")

    assert response.status_code == 200
    outcomes = response.json()["outcomes"]
    assert outcomes["count"] == 0
    assert outcomes["first_weekdate"] is None
    assert outcomes["latest_weekdate"] is None
    assert outcomes["average_fpr_chg13"] is None
    assert outcomes["median_fpr_chg13"] is None
    assert outcomes["positive_return_count"] == 0
    assert outcomes["positive_return_rate"] == 0.0
    assert outcomes["outperform_base_count"] == 0
    assert outcomes["outperform_base_rate"] == 0.0


def test_stim_select_outcomes_summary_filters_start_date(client):
    response = client.get("/v1/selections/stim-select/outcomes/summary?start_date=2024-01-12")

    outcomes = response.json()["outcomes"]
    assert outcomes["count"] == 2
    assert outcomes["first_weekdate"] == "2024-01-12"
    assert outcomes["latest_weekdate"] == "2024-01-19"


def test_stim_select_outcomes_summary_filters_end_date(client):
    response = client.get("/v1/selections/stim-select/outcomes/summary?end_date=2024-01-12")

    outcomes = response.json()["outcomes"]
    assert outcomes["count"] == 3
    assert outcomes["first_weekdate"] == "2024-01-05"
    assert outcomes["latest_weekdate"] == "2024-01-12"


def test_stim_select_outcomes_summary_combined_dates_and_exchange_filter(client):
    response = client.get(
        "/v1/selections/stim-select/outcomes/summary"
        "?start_date=2024-01-12&end_date=2024-01-12&exchange=Q"
    )

    body = response.json()
    assert body["filters"]["exchange"] == "Q"
    outcomes = body["outcomes"]
    assert outcomes["count"] == 1
    assert outcomes["average_fpr_chg13"] == pytest.approx(10.0)
    assert outcomes["median_fpr_chg13"] == pytest.approx(10.0)


def test_stim_select_outcomes_summary_limit_rank_is_per_week(client, outcome_engine):
    response = client.get("/v1/selections/stim-select/outcomes/summary?limit_rank=1")

    body = response.json()
    assert body["filters"]["limit_rank"] == 1
    assert body["outcomes"]["count"] == 3
    assert body["outcomes"]["average_fpr_chg13"] == pytest.approx((5 + 10 + 2.19) / 3)
    executed_sql = "\n".join(sql for sql, _params in outcome_engine.executed)
    assert "ROW_NUMBER() OVER" in executed_sql
    assert "PARTITION BY a.weekdate" in executed_sql
    assert "rank_13wk_probability <= :limit_rank" in executed_sql


def test_stim_select_outcomes_summary_business_boundary(client):
    response = client.get("/v1/selections/stim-select/outcomes/summary")

    body = response.json()
    assert "data" not in body
    assert "symbols" not in body
    assert "AAA" not in response.text
    assert "BBB" not in response.text
    assert body["provenance"]["published_report_limited"] is False
    assert body["provenance"]["current_live_selections_excluded"] is True
    assert body["provenance"]["current_matching_symbols_excluded"] is True


def test_stim_select_outcomes_summary_access_classification_layers_are_public_free():
    path = "/v1/selections/stim-select/outcomes/summary"

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
    assert policy_provider.is_public_stim_select_outcome_summary_path(path)
    assert policy_provider.is_public_stocktrends_path(path)
    assert decision.access_granted is True
    assert decision.is_metered == 0
    assert decision.log_pricing_rule_id == "default_free"
    assert decision.econ_payment_required == 0
    assert accepted == "none"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/selections/latest",
        "/v1/selections/history",
        "/v1/selections/published/latest",
        "/v1/selections/published/history",
    ],
)
def test_existing_selection_endpoint_classifications_do_not_change(path):
    decision = classifier_module.classify_request(
        path=path,
        method="GET",
        has_paid_auth=False,
        payment_method_header=None,
        plan_code=None,
        agent_identifier=None,
    )

    assert policy_provider.get_effective_endpoint_payment_policy(path, "GET") is not None
    assert not policy_provider.is_public_stocktrends_path(path)
    assert decision.access_granted is False
    assert decision.deny_reason == "authentication_required"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/selections/stim-select/outcomes",
        "/v1/selections/stim-select/outcomes/current",
        "/v1/selections/stim-select/outcomes/symbols",
    ],
)
def test_stim_select_outcome_child_paths_remain_protected(client, path):
    response = client.get(path)

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing API key"}


def test_stim_select_outcomes_summary_openapi_documents_filters_and_boundary(client):
    response = client.get("/v1/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    operation = schema["paths"]["/selections/stim-select/outcomes/summary"]["get"]
    description = operation["description"]
    assert "fpr_chg13" in description
    assert "Public aggregate historical outcome summary" in description
    assert "Does not expose current selections" in description

    parameters = {
        parameter["name"]: parameter
        for parameter in operation["parameters"]
        if "name" in parameter
    }
    assert {"start_date", "end_date", "exchange", "limit_rank"}.issubset(parameters)
    assert _schema_has_date_format(parameters["start_date"]["schema"])
    assert _schema_has_date_format(parameters["end_date"]["schema"])
    assert _schema_has_key_value(parameters["limit_rank"]["schema"], "minimum", 1)
