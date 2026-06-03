from __future__ import annotations

from datetime import date, datetime
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
from services import stim_select_outcome_summary as outcome_summary_service
from services.stim_select_outcome_summary import build_default_summary_key


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
        "fpr_chg4": Decimal("1.0"),
        "fpr_chg13": Decimal("5.0"),
        "fpr_chg40": Decimal("20.0"),
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
        "fpr_chg4": Decimal("-2.0"),
        "fpr_chg13": Decimal("-1.0"),
        "fpr_chg40": Decimal("-5.0"),
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
        "fpr_chg4": Decimal("4.0"),
        "fpr_chg13": Decimal("10.0"),
        "fpr_chg40": Decimal("30.0"),
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
        "fpr_chg4": Decimal("5.0"),
        "fpr_chg13": None,
        "fpr_chg40": Decimal("50.0"),
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
        "fpr_chg4": Decimal("0.0"),
        "fpr_chg13": Decimal("2.19"),
        "fpr_chg40": Decimal("6.45"),
    },
    {
        "weekdate": date(2024, 1, 19),
        "exchange": "N",
        "symbol": "NO40",
        "x4wk1": Decimal("0.3"),
        "x13wk1": Decimal("2.8"),
        "x40wk1": Decimal("7.0"),
        "x13wk": Decimal("3.0"),
        "x13wksd": Decimal("1.2"),
        "price": Decimal("6.0"),
        "volume": 3000,
        "fpr_chg4": Decimal("2.0"),
        "fpr_chg13": Decimal("4.0"),
        "fpr_chg40": None,
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
        "fpr_chg4": Decimal("99.0"),
        "fpr_chg13": Decimal("99.0"),
        "fpr_chg40": Decimal("99.0"),
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
        "fpr_chg4": Decimal("99.0"),
        "fpr_chg13": Decimal("99.0"),
        "fpr_chg40": Decimal("99.0"),
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
        "fpr_chg4": Decimal("99.0"),
        "fpr_chg13": Decimal("99.0"),
        "fpr_chg40": Decimal("99.0"),
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


class _OrigDatabaseError:
    def __init__(self, errno: int):
        self.errno = errno
        self.args = (errno, "database error")


def _operational_error(errno: int):
    orig = _OrigDatabaseError(errno)
    try:
        exc = outcome_summary_service.OperationalError("SELECT 1", {}, orig)
        if not hasattr(exc, "orig"):
            setattr(exc, "orig", orig)
        return exc
    except TypeError:
        exc = outcome_summary_service.OperationalError("database error")
        setattr(exc, "orig", orig)
        return exc


def _subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, month=2, day=28)


class _Connection:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        summary_rows: list[dict[str, Any]],
        executed: list[tuple[str, dict[str, Any]]],
        *,
        summary_table_missing: bool = False,
        summary_operational_error_number: int | None = None,
    ):
        self._rows = rows
        self._summary_rows = summary_rows
        self._executed = executed
        self._summary_table_missing = summary_table_missing
        self._summary_operational_error_number = summary_operational_error_number

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

        if "stim_select_outcome_summary" in sql or (
            {"summary_key", "signal_id"}.issubset(params)
        ):
            if self._summary_table_missing:
                raise _operational_error(1146)
            if self._summary_operational_error_number is not None:
                raise _operational_error(self._summary_operational_error_number)
            summary_key = params.get("summary_key")
            return _Result(
                [
                    row
                    for row in self._summary_rows
                    if row.get("summary_key") == summary_key
                ][:1]
            )

        rows = self._qualifying_rows(params)
        values = [row["fpr_chg13"] for row in rows]
        values_4wk = [row["fpr_chg4"] for row in rows if row["fpr_chg4"] is not None]
        values_40wk = [row["fpr_chg40"] for row in rows if row["fpr_chg40"] is not None]

        if "latest_mature_outcome_date" in sql:
            return _Result(
                [
                    {
                        "latest_mature_outcome_date": max(
                            (row["weekdate"] for row in rows),
                            default=None,
                        )
                    }
                ]
            )

        if "outcome_count" in sql:
            return _Result(
                [
                    {
                        "outcome_count": len(rows),
                        "count_4wk": len(values_4wk),
                        "count_13wk": len(values),
                        "count_40wk": len(values_40wk),
                        "first_weekdate": min((row["weekdate"] for row in rows), default=None),
                        "latest_weekdate": max((row["weekdate"] for row in rows), default=None),
                        "average_fpr_chg4": (sum(values_4wk, Decimal("0")) / len(values_4wk)) if values_4wk else None,
                        "average_fpr_chg13": (sum(values, Decimal("0")) / len(values)) if values else None,
                        "average_fpr_chg40": (sum(values_40wk, Decimal("0")) / len(values_40wk)) if values_40wk else None,
                        "positive_return_count": sum(1 for value in values if value > 0),
                        "positive_return_count_4wk": sum(1 for value in values_4wk if value > 0),
                        "positive_return_count_13wk": sum(1 for value in values if value > 0),
                        "positive_return_count_40wk": sum(1 for value in values_40wk if value > 0),
                        "outperform_base_count": sum(1 for value in values if value > Decimal("2.19")),
                        "outperform_base_count_4wk": sum(1 for value in values_4wk if value > Decimal("0")),
                        "outperform_base_count_13wk": sum(1 for value in values if value > Decimal("2.19")),
                        "outperform_base_count_40wk": sum(1 for value in values_40wk if value > Decimal("6.45")),
                    }
                ]
            )

        return _Result(
            [
                {
                    "fpr_chg4": row["fpr_chg4"],
                    "fpr_chg13": row["fpr_chg13"],
                    "fpr_chg40": row["fpr_chg40"],
                }
                for row in rows
            ]
        )


def _summary_row_for(
    rows: list[dict[str, Any]],
    *,
    exchange: str | None = None,
    limit_rank: int | None = None,
) -> dict[str, Any]:
    conn = _Connection(rows, [], [])
    latest_mature_weekdate = max(
        (row["weekdate"] for row in conn._qualifying_rows({})),
        default=None,
    )
    assert latest_mature_weekdate is not None
    start_date = _subtract_years(latest_mature_weekdate, 10)
    params: dict[str, Any] = {
        "start_date": start_date,
        "end_date": latest_mature_weekdate,
    }
    if exchange is not None:
        params["exchange"] = exchange
    if limit_rank is not None:
        params["limit_rank"] = limit_rank

    qualified = conn._qualifying_rows(params)
    values = [row["fpr_chg13"] for row in qualified]
    count = len(qualified)
    summary = {
        "summary_key": build_default_summary_key(exchange=exchange, limit_rank=limit_rank),
        "signal_id": "stim_select",
        "start_date": start_date,
        "end_date": latest_mature_weekdate,
        "exchange": exchange,
        "limit_rank": limit_rank,
        "outcome_count": count,
        "first_weekdate": min((row["weekdate"] for row in qualified), default=None),
        "latest_weekdate": max((row["weekdate"] for row in qualified), default=None),
        "generated_at": datetime(2024, 1, 20, 12, 0, 0),
        "source_latest_mature_weekdate": latest_mature_weekdate,
        "base_period_mean_4wk": Decimal("0.00"),
        "base_period_mean_13wk": Decimal("2.19"),
        "base_period_mean_40wk": Decimal("6.45"),
    }
    for horizon, definition in outcome_summary_service.HORIZON_DEFINITIONS.items():
        field = definition["field"]
        suffix = definition["suffix"]
        base = definition["base"]
        horizon_values = [row[field] for row in qualified if row[field] is not None]
        horizon_count = len(horizon_values)
        positive_count = sum(1 for value in horizon_values if value > 0)
        outperform_count = sum(1 for value in horizon_values if value > base)
        summary[f"average_{field}"] = (
            sum(horizon_values, Decimal("0")) / horizon_count if horizon_values else None
        )
        summary[f"median_{field}"] = (
            Decimal(str(selections_router.median(horizon_values))) if horizon_values else None
        )
        summary[f"count_{suffix}"] = horizon_count
        summary[f"positive_return_count_{suffix}"] = positive_count
        summary[f"positive_return_rate_{suffix}"] = (
            Decimal(str(positive_count / horizon_count)) if horizon_count else Decimal("0")
        )
        summary[f"outperform_base_count_{suffix}"] = outperform_count
        summary[f"outperform_base_rate_{suffix}"] = (
            Decimal(str(outperform_count / horizon_count)) if horizon_count else Decimal("0")
        )
    return summary


class _Engine:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.summary_rows = [
            _summary_row_for(rows),
            _summary_row_for(rows, limit_rank=1),
            _summary_row_for(rows, limit_rank=10),
            _summary_row_for(rows, exchange="B"),
        ]
        self.summary_table_missing = False
        self.summary_operational_error_number: int | None = None
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def connect(self):
        return _Connection(
            self.rows,
            self.summary_rows,
            self.executed,
            summary_table_missing=self.summary_table_missing,
            summary_operational_error_number=self.summary_operational_error_number,
        )


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


def _schema_has_property(schema: Any, property_name: str) -> bool:
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict) and property_name in properties:
            return True
        return any(_schema_has_property(item, property_name) for item in schema.values())
    if isinstance(schema, list):
        return any(_schema_has_property(item, property_name) for item in schema)
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
    assert "avgmean_formula" not in body["signal"]["criteria"]
    assert body["signal"]["base_period_mean_13wk"] == 2.19
    assert body["filters"] == {
        "start_date": "2014-01-19",
        "end_date": "2024-01-19",
        "exchange": None,
        "limit_rank": None,
        "default_window_applied": True,
    }

    outcomes = body["outcomes"]
    assert outcomes["horizon"] == "13w"
    assert outcomes["count"] == 5
    assert outcomes["first_weekdate"] == "2024-01-05"
    assert outcomes["latest_weekdate"] == "2024-01-19"
    assert outcomes["average_fpr_chg13"] == pytest.approx(4.038)
    assert outcomes["median_fpr_chg13"] == pytest.approx(4.0)
    assert outcomes["positive_return_count"] == 4
    assert outcomes["positive_return_rate"] == pytest.approx(0.8)
    assert outcomes["outperform_base_count"] == 3
    assert outcomes["outperform_base_rate"] == pytest.approx(0.6)
    assert outcomes["base_period_mean_13wk"] == 2.19
    assert body["outcomes_by_horizon"]["4w"]["realized_return_field"] == "fpr_chg4"
    assert body["outcomes_by_horizon"]["4w"]["count"] == 5
    assert body["outcomes_by_horizon"]["4w"]["average_fpr_chg"] == pytest.approx(1.0)
    assert body["outcomes_by_horizon"]["4w"]["median_fpr_chg"] == pytest.approx(1.0)
    assert body["outcomes_by_horizon"]["4w"]["positive_return_count"] == 3
    assert body["outcomes_by_horizon"]["4w"]["positive_return_rate"] == pytest.approx(0.6)
    assert body["outcomes_by_horizon"]["13w"]["realized_return_field"] == "fpr_chg13"
    assert body["outcomes_by_horizon"]["13w"]["count"] == 5
    assert body["outcomes_by_horizon"]["13w"]["average_fpr_chg"] == pytest.approx(4.038)
    assert body["outcomes_by_horizon"]["40w"]["realized_return_field"] == "fpr_chg40"
    assert body["outcomes_by_horizon"]["40w"]["count"] == 4
    assert body["outcomes_by_horizon"]["40w"]["average_fpr_chg"] == pytest.approx(12.8625)
    assert body["outcomes_by_horizon"]["40w"]["median_fpr_chg"] == pytest.approx(13.225)
    assert body["outcomes_by_horizon"]["40w"]["positive_return_count"] == 3
    assert body["outcomes_by_horizon"]["40w"]["positive_return_rate"] == pytest.approx(0.75)
    assert body["outcomes_by_horizon"]["40w"]["outperform_base_count"] == 2
    assert body["outcomes_by_horizon"]["40w"]["outperform_base_rate"] == pytest.approx(0.5)
    assert body["provenance"]["summary_table"]["served_from_summary_table"] is True
    assert body["provenance"]["summary_table"]["table"] == "stweekly.stim_select_outcome_summary"
    assert body["provenance"]["summary_table"]["summary_key"] == build_default_summary_key(
        exchange=None,
        limit_rank=None,
    )
    assert body["provenance"]["summary_table"]["source_latest_mature_weekdate"] == "2024-01-19"


def test_stim_select_outcomes_summary_unbounded_binds_default_window(client, outcome_engine):
    response = client.get("/v1/selections/stim-select/outcomes/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["filters"]["default_window_applied"] is True
    assert body["filters"]["start_date"] == "2014-01-19"
    assert body["filters"]["end_date"] == "2024-01-19"

    cache_params = next(
        params
        for _sql, params in outcome_engine.executed
        if "summary_key" in params
    )
    assert cache_params["summary_key"] == build_default_summary_key(
        exchange=None,
        limit_rank=None,
    )
    assert not any("base_4wk" in params for _sql, params in outcome_engine.executed)


def test_stim_select_outcomes_summary_missing_summary_row_returns_503(client, outcome_engine):
    outcome_engine.summary_rows = []

    response = client.get("/v1/selections/stim-select/outcomes/summary?limit_rank=10")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail == {
        "error": "outcome_summary_not_available",
        "message": "Historical summary table has not been populated.",
        "refresh_required": True,
        "supported_default_combinations": [
            {"exchange": None, "limit_rank": None},
            {"exchange": None, "limit_rank": 10},
        ],
        "custom_refresh_note": (
            "When start_date and end_date are omitted, the default refresh seeds "
            "all-exchange rows for limit_rank omitted/null and limit_rank=10. "
            "Other no-date exchange or limit_rank combinations require explicit "
            "date filters or a custom summary refresh."
        ),
    }
    assert any("summary_key" in params for _sql, params in outcome_engine.executed)
    assert not any("base_4wk" in params for _sql, params in outcome_engine.executed)


def test_stim_select_outcomes_summary_unseeded_no_date_limit_rank_returns_503(
    client,
    outcome_engine,
):
    response = client.get("/v1/selections/stim-select/outcomes/summary?limit_rank=9")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "outcome_summary_not_available"
    assert {"exchange": None, "limit_rank": None} in detail["supported_default_combinations"]
    assert {"exchange": None, "limit_rank": 10} in detail["supported_default_combinations"]
    assert "custom summary refresh" in detail["custom_refresh_note"]
    cache_params = next(
        params
        for _sql, params in outcome_engine.executed
        if "summary_key" in params
    )
    assert cache_params["summary_key"] == build_default_summary_key(
        exchange=None,
        limit_rank=9,
    )
    assert not any("base_4wk" in params for _sql, params in outcome_engine.executed)


def test_stim_select_outcomes_summary_missing_table_returns_503(client, outcome_engine):
    outcome_engine.summary_table_missing = True

    response = client.get("/v1/selections/stim-select/outcomes/summary?limit_rank=10")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "error": "outcome_summary_not_available",
        "message": "Historical summary table has not been created.",
        "refresh_required": True,
        "supported_default_combinations": [
            {"exchange": None, "limit_rank": None},
            {"exchange": None, "limit_rank": 10},
        ],
        "custom_refresh_note": (
            "When start_date and end_date are omitted, the default refresh seeds "
            "all-exchange rows for limit_rank omitted/null and limit_rank=10. "
            "Other no-date exchange or limit_rank combinations require explicit "
            "date filters or a custom summary refresh."
        ),
    }
    assert any("summary_key" in params for _sql, params in outcome_engine.executed)
    assert not any("base_4wk" in params for _sql, params in outcome_engine.executed)


def test_stim_select_outcomes_summary_non_missing_table_operational_error_is_not_503(
    client,
    outcome_engine,
):
    outcome_engine.summary_operational_error_number = 2003

    response = client.get("/v1/selections/stim-select/outcomes/summary?limit_rank=10")

    assert response.status_code == 500
    assert response.json()["detail"]["error"] == "db_query_failed"


def test_stim_select_outcome_summary_mysql_table_missing_detection_is_narrow():
    assert outcome_summary_service.is_mysql_no_such_table_error(_operational_error(1146))
    assert not outcome_summary_service.is_mysql_no_such_table_error(_operational_error(2003))


def test_stim_select_outcomes_summary_uses_mature_fpr_chg13_only(client):
    response = client.get("/v1/selections/stim-select/outcomes/summary")

    body_text = response.text
    assert response.json()["outcomes"]["count"] == 5
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


def test_stim_select_outcomes_summary_sql_uses_stweekly_market_schema(client, outcome_engine):
    response = client.get(
        "/v1/selections/stim-select/outcomes/summary"
        "?start_date=2024-01-05&end_date=2024-01-19"
    )

    assert response.status_code == 200
    executed_sql = "\n".join(sql for sql, _params in outcome_engine.executed)
    assert "stdata.st_data" not in executed_sql
    assert "FROM stweekly.st_data a" in executed_sql
    assert "JOIN stweekly.st_returnmeans b" in executed_sql


def test_stim_select_outcome_summary_generation_uses_stweekly_sources(monkeypatch):
    monkeypatch.setattr(outcome_summary_service, "text", lambda sql: sql)
    executed: list[tuple[str, dict[str, Any]]] = []
    conn = _Connection(list(_OUTCOME_ROWS), [], executed)

    record = outcome_summary_service.compute_summary_record(
        conn,
        exchange=None,
        limit_rank=10,
        generated_at=datetime(2024, 1, 20, 12, 0, 0),
    )

    executed_sql = "\n".join(sql for sql, _params in executed)
    assert "FROM stweekly.st_data a" in executed_sql
    assert "JOIN stweekly.st_returnmeans b" in executed_sql
    assert "stdata.st_data" not in executed_sql
    assert "ROW_NUMBER() OVER" in executed_sql
    assert record["summary_key"] == build_default_summary_key(exchange=None, limit_rank=10)
    assert record["count_4wk"] == 5
    assert record["count_13wk"] == 5
    assert record["count_40wk"] == 4
    assert record["average_fpr_chg4"] == pytest.approx(Decimal("1.0"))
    assert record["average_fpr_chg13"] == pytest.approx(Decimal("4.038"))
    assert record["average_fpr_chg40"] == pytest.approx(Decimal("12.8625"))
    assert record["positive_return_rate_40wk"] == pytest.approx(0.75)
    assert record["outperform_base_rate_40wk"] == pytest.approx(0.5)


def test_stim_select_outcomes_summary_filters_start_date(client):
    response = client.get("/v1/selections/stim-select/outcomes/summary?start_date=2024-01-12")

    filters = response.json()["filters"]
    assert filters["start_date"] == "2024-01-12"
    assert filters["end_date"] is None
    assert filters["default_window_applied"] is False
    outcomes = response.json()["outcomes"]
    assert outcomes["count"] == 3
    assert outcomes["first_weekdate"] == "2024-01-12"
    assert outcomes["latest_weekdate"] == "2024-01-19"


def test_stim_select_outcomes_summary_filters_end_date(client):
    response = client.get("/v1/selections/stim-select/outcomes/summary?end_date=2024-01-12")

    filters = response.json()["filters"]
    assert filters["start_date"] is None
    assert filters["end_date"] == "2024-01-12"
    assert filters["default_window_applied"] is False
    outcomes = response.json()["outcomes"]
    assert outcomes["count"] == 3
    assert outcomes["first_weekdate"] == "2024-01-05"
    assert outcomes["latest_weekdate"] == "2024-01-12"


def test_stim_select_outcomes_summary_explicit_date_range_does_not_apply_default(client, outcome_engine):
    response = client.get(
        "/v1/selections/stim-select/outcomes/summary"
        "?start_date=2024-01-05&end_date=2024-01-12"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["filters"]["start_date"] == "2024-01-05"
    assert body["filters"]["end_date"] == "2024-01-12"
    assert body["filters"]["default_window_applied"] is False
    assert body["outcomes"]["count"] == 3

    executed_sql = "\n".join(sql for sql, _params in outcome_engine.executed)
    assert "latest_mature_outcome_date" not in executed_sql


def test_stim_select_outcomes_summary_rejects_inverted_date_range(client):
    response = client.get(
        "/v1/selections/stim-select/outcomes/summary"
        "?start_date=2024-01-19&end_date=2024-01-05"
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_date_range"


def test_stim_select_outcomes_summary_rejects_invalid_exchange(client):
    response = client.get("/v1/selections/stim-select/outcomes/summary?exchange=I")

    assert response.status_code == 400
    assert "Invalid exchange" in response.json()["detail"]


def test_stim_select_outcomes_summary_combined_dates_and_exchange_filter(client):
    response = client.get(
        "/v1/selections/stim-select/outcomes/summary"
        "?start_date=2024-01-12&end_date=2024-01-12&exchange=Q"
    )

    body = response.json()
    assert body["filters"]["exchange"] == "Q"
    assert body["filters"]["default_window_applied"] is False
    outcomes = body["outcomes"]
    assert outcomes["count"] == 1
    assert outcomes["average_fpr_chg13"] == pytest.approx(10.0)
    assert outcomes["median_fpr_chg13"] == pytest.approx(10.0)


def test_stim_select_outcomes_summary_limit_rank_is_per_week(client, outcome_engine):
    response = client.get("/v1/selections/stim-select/outcomes/summary?limit_rank=1")

    body = response.json()
    assert body["filters"]["limit_rank"] == 1
    assert body["filters"]["default_window_applied"] is True
    assert body["outcomes"]["count"] == 3
    assert body["outcomes"]["average_fpr_chg13"] == pytest.approx((5 + 10 + 2.19) / 3)
    assert any("summary_key" in params for _sql, params in outcome_engine.executed)
    assert not any("base_4wk" in params for _sql, params in outcome_engine.executed)
    assert body["outcomes_by_horizon"]["4w"]["count"] == 3
    assert body["outcomes_by_horizon"]["40w"]["positive_return_count"] == 3
    assert body["provenance"]["summary_table"]["summary_key"] == build_default_summary_key(
        exchange=None,
        limit_rank=1,
    )


def test_stim_select_outcomes_summary_explicit_date_limit_rank_uses_live_per_week_rank(
    client,
    outcome_engine,
):
    response = client.get(
        "/v1/selections/stim-select/outcomes/summary"
        "?start_date=2024-01-05&end_date=2024-01-19&limit_rank=1"
    )

    body = response.json()
    assert body["filters"]["limit_rank"] == 1
    assert body["filters"]["default_window_applied"] is False
    assert body["outcomes"]["count"] == 3
    assert body["outcomes"]["average_fpr_chg13"] == pytest.approx((5 + 10 + 2.19) / 3)
    assert "cache" not in body["provenance"]
    executed_sql = "\n".join(sql for sql, _params in outcome_engine.executed)
    assert "ROW_NUMBER() OVER" in executed_sql
    assert "PARTITION BY a.weekdate" in executed_sql
    assert "rank_13wk_probability <= :limit_rank" in executed_sql
    assert "stdata.st_data" not in executed_sql
    assert "FROM stweekly.st_data a" in executed_sql
    assert "JOIN stweekly.st_returnmeans b" in executed_sql


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
    assert "fpr_chg4" in description
    assert "fpr_chg40" in description
    assert "stweekly.stim_select_outcome_summary" in description
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
    assert "limit_rank=10" in parameters["limit_rank"]["description"]
    assert "custom summary refresh" in parameters["limit_rank"]["description"]
    assert _schema_has_property(schema, "default_window_applied")
    assert _schema_has_property(schema, "outcomes_by_horizon")
