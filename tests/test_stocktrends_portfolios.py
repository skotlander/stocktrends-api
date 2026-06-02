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
        "buys": 2,
        "sells": 1,
        "held": 10,
        "net_proceeds": Decimal("-1500.25"),
        "realizedgain": Decimal("123.45"),
        "cum_realizedgain": Decimal("1234.56"),
        "totalvaluation": Decimal("12345.67"),
        "unrealizedgain": Decimal("234.56"),
        "cum_totalgain": Decimal("1469.12"),
        "tsxindex": Decimal("21000.11"),
        "spindex": Decimal("4800.22"),
        "private_note": "legacy return audit note should not leak",
    },
    {
        "port_id": 2,
        "weekdate": date(2024, 1, 12),
        "buys": 0,
        "sells": 2,
        "held": 8,
        "net_proceeds": Decimal("2100.00"),
        "realizedgain": Decimal("-45.67"),
        "cum_realizedgain": Decimal("1188.89"),
        "totalvaluation": Decimal("12290.12"),
        "unrealizedgain": Decimal("200.01"),
        "cum_totalgain": Decimal("1388.90"),
        "tsxindex": Decimal("21100.33"),
        "spindex": Decimal("4810.44"),
        "private_note": "legacy return audit note should not leak",
    },
    {
        "port_id": 1,
        "weekdate": date(2024, 1, 5),
        "buys": 1,
        "sells": 0,
        "held": 5,
        "net_proceeds": Decimal("-500.00"),
        "realizedgain": Decimal("88.00"),
        "cum_realizedgain": Decimal("88.00"),
        "totalvaluation": Decimal("10088.00"),
        "unrealizedgain": Decimal("100.00"),
        "cum_totalgain": Decimal("188.00"),
        "tsxindex": Decimal("21000.11"),
        "spindex": Decimal("4800.22"),
        "private_note": "legacy return audit note should not leak",
    },
    {
        "port_id": 99,
        "weekdate": date(2024, 1, 5),
        "buys": 9,
        "sells": 9,
        "held": 9,
        "net_proceeds": Decimal("999.99"),
        "realizedgain": Decimal("999.99"),
        "cum_realizedgain": Decimal("999.99"),
        "totalvaluation": Decimal("999.99"),
        "unrealizedgain": Decimal("999.99"),
        "cum_totalgain": Decimal("999.99"),
        "tsxindex": Decimal("999.99"),
        "spindex": Decimal("999.99"),
        "private_note": "inactive portfolio returns should not leak",
    },
]

_POSITIONS_ROWS = [
    {
        "port_id": 2,
        "position_id": 20,
        "symbol": "IBM",
        "exchange": "N",
        "name": "International Business Machines",
        "date_in": date(2023, 10, 6),
        "price_in": Decimal("100.00"),
        "qty": 100,
        "trcost_in": Decimal("9.99"),
        "cost_adjs": Decimal("0.00"),
        "total_cost": Decimal("10009.99"),
        "stop_loss": Decimal("90.00"),
        "date_out": date(2024, 1, 5),
        "weeks_held": 12,
        "sell_trigger": "BC",
        "price_out": Decimal("110.00"),
        "trcost_out": Decimal("9.99"),
        "sell_adjs": Decimal("0.00"),
        "total_proceeds": Decimal("10990.01"),
        "gain_loss": Decimal("980.02"),
        "gl_percent": Decimal("9.80"),
        "weekdate": date(2024, 1, 5),
        "last_update": "operational metadata should not leak",
    },
    {
        "port_id": 2,
        "position_id": 21,
        "symbol": "MSFT",
        "exchange": "Q",
        "name": "Microsoft",
        "date_in": date(2023, 11, 3),
        "price_in": Decimal("200.00"),
        "qty": 50,
        "trcost_in": Decimal("4.99"),
        "cost_adjs": Decimal("1.25"),
        "total_cost": Decimal("10006.24"),
        "stop_loss": Decimal("180.00"),
        "date_out": date(2024, 1, 12),
        "weeks_held": 10,
        "sell_trigger": "PT",
        "price_out": Decimal("215.00"),
        "trcost_out": Decimal("4.99"),
        "sell_adjs": Decimal("-0.25"),
        "total_proceeds": Decimal("10744.76"),
        "gain_loss": Decimal("738.52"),
        "gl_percent": Decimal("7.38"),
        "weekdate": date(2024, 1, 12),
        "last_update": "operational metadata should not leak",
    },
    {
        "port_id": 2,
        "position_id": 22,
        "symbol": "LIVE",
        "exchange": "N",
        "name": "Current Live Holding",
        "date_in": date(2024, 1, 19),
        "price_in": Decimal("50.00"),
        "qty": 25,
        "trcost_in": Decimal("1.99"),
        "cost_adjs": Decimal("0.00"),
        "total_cost": Decimal("1251.99"),
        "stop_loss": Decimal("45.00"),
        "date_out": date(2024, 1, 26),
        "weeks_held": 1,
        "sell_trigger": "",
        "price_out": Decimal("0.00"),
        "trcost_out": Decimal("0.00"),
        "sell_adjs": Decimal("0.00"),
        "total_proceeds": Decimal("0.00"),
        "gain_loss": Decimal("999999.00"),
        "gl_percent": Decimal("999.99"),
        "weekdate": date(2024, 1, 26),
        "last_update": "current holdings must not leak",
    },
    {
        "port_id": 99,
        "position_id": 99,
        "symbol": "INACTIVE",
        "exchange": "N",
        "name": "Inactive Portfolio Closed Position",
        "date_in": date(2023, 1, 6),
        "price_in": Decimal("10.00"),
        "qty": 10,
        "trcost_in": Decimal("1.00"),
        "cost_adjs": Decimal("0.00"),
        "total_cost": Decimal("101.00"),
        "stop_loss": Decimal("9.00"),
        "date_out": date(2024, 1, 5),
        "weeks_held": 52,
        "sell_trigger": "BC",
        "price_out": Decimal("12.00"),
        "trcost_out": Decimal("1.00"),
        "sell_adjs": Decimal("0.00"),
        "total_proceeds": Decimal("119.00"),
        "gain_loss": Decimal("18.00"),
        "gl_percent": Decimal("17.82"),
        "weekdate": date(2024, 1, 5),
        "last_update": "inactive portfolio data should not leak",
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
        position_rows: list[dict[str, Any]],
        executed: list[tuple[str, dict[str, Any]]],
    ):
        self._portfolio_rows = portfolio_rows
        self._return_rows = return_rows
        self._position_rows = position_rows
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
            if "start_date" in params:
                rows = [row for row in rows if row["weekdate"] >= params["start_date"]]
            if "end_date" in params:
                rows = [row for row in rows if row["weekdate"] <= params["end_date"]]
            rows = sorted(rows, key=lambda row: row["weekdate"])
            if "COUNT(*) AS return_count" in sql:
                return _Result(
                    [
                        {
                            "return_count": len(rows),
                            "first_weekdate": rows[0]["weekdate"] if rows else None,
                            "latest_weekdate": rows[-1]["weekdate"] if rows else None,
                        }
                    ]
                )
            if "ORDER BY weekdate DESC" in sql:
                rows = sorted(rows, key=lambda row: row["weekdate"], reverse=True)[:1]
            return _Result(rows)

        if "FROM stp_positions" in sql:
            rows = [
                row
                for row in self._position_rows
                if row["port_id"] == params.get("port_id") and row.get("sell_trigger") not in (None, "")
            ]
            if "start_date" in params:
                rows = [row for row in rows if row["date_out"] >= params["start_date"]]
            if "end_date" in params:
                rows = [row for row in rows if row["date_out"] <= params["end_date"]]
            rows = sorted(rows, key=lambda row: (row["date_out"], row["position_id"]))
            if "COUNT(*) AS closed_position_count" in sql:
                gains = [row["gain_loss"] for row in rows if row.get("gain_loss") is not None]
                gain_percents = [row["gl_percent"] for row in rows if row.get("gl_percent") is not None]
                total_gain = sum(gains, Decimal("0")) if gains else None
                average_gain_percent = (
                    sum(gain_percents, Decimal("0")) / len(gain_percents)
                    if gain_percents
                    else None
                )
                return _Result(
                    [
                        {
                            "closed_position_count": len(rows),
                            "first_date_in": min((row["date_in"] for row in rows), default=None),
                            "first_date_out": rows[0]["date_out"] if rows else None,
                            "latest_date_out": rows[-1]["date_out"] if rows else None,
                            "total_realized_gain_loss": total_gain,
                            "average_gain_loss_percent": average_gain_percent,
                            "winning_positions": sum(1 for row in rows if row.get("gain_loss", 0) > 0),
                            "losing_positions": sum(1 for row in rows if row.get("gain_loss", 0) < 0),
                        }
                    ]
                )
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
        position_rows: list[dict[str, Any]],
    ):
        self.portfolio_rows = portfolio_rows
        self.return_rows = return_rows
        self.position_rows = position_rows
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def connect(self):
        return _Connection(self.portfolio_rows, self.return_rows, self.position_rows, self.executed)


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


def _schema_has_date_format(schema: Any) -> bool:
    if isinstance(schema, dict):
        if schema.get("format") == "date":
            return True
        return any(_schema_has_date_format(value) for value in schema.values())
    if isinstance(schema, list):
        return any(_schema_has_date_format(value) for value in schema)
    return False


@pytest.fixture
def portfolio_engine(monkeypatch):
    engine = _Engine(_ROWS, _RETURNS_ROWS, _POSITIONS_ROWS)
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
            "buys": 2,
            "sells": 1,
            "held": 10,
            "net_proceeds": -1500.25,
            "realized_gain": 123.45,
            "cumulative_realized_gain": 1234.56,
            "total_valuation": 12345.67,
            "unrealized_gain": 234.56,
            "cumulative_total_gain": 1469.12,
            "tsx_index": 21000.11,
            "sp_index": 4800.22,
        },
        {
            "weekdate": "2024-01-12",
            "buys": 0,
            "sells": 2,
            "held": 8,
            "net_proceeds": 2100.0,
            "realized_gain": -45.67,
            "cumulative_realized_gain": 1188.89,
            "total_valuation": 12290.12,
            "unrealized_gain": 200.01,
            "cumulative_total_gain": 1388.9,
            "tsx_index": 21100.33,
            "sp_index": 4810.44,
        },
    ]
    assert "private_note" not in str(body)
    assert "return_pct" not in str(body)
    assert "value" not in str(body)

    executed_sql = "\n".join(sql for sql, _params in portfolio_engine.executed)
    assert "FROM stp_ports" in executed_sql
    assert "AND status = 1" in executed_sql
    assert "FROM stp_returnslog" in executed_sql
    for expected_column in (
        "buys",
        "sells",
        "held",
        "net_proceeds",
        "realizedgain",
        "cum_realizedgain",
        "totalvaluation",
        "unrealizedgain",
        "cum_totalgain",
        "tsxindex",
        "spindex",
    ):
        assert expected_column in executed_sql
    assert "return_pct" not in executed_sql
    assert "ORDER BY weekdate ASC" in executed_sql
    assert "stp_positions" not in executed_sql


def test_portfolio_returns_history_with_no_rows_returns_empty_list(protected_client, portfolio_engine):
    portfolio_engine.return_rows = [
        row for row in portfolio_engine.return_rows if row["port_id"] != 2
    ]

    response = protected_client.get("/v1/stocktrends/portfolios/2/returns")

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["port_id"] == 2
    assert body["count"] == 0
    assert body["returns"] == []


def test_portfolio_returns_history_start_date_filters_earlier_rows(protected_client, portfolio_engine):
    response = protected_client.get(
        "/v1/stocktrends/portfolios/2/returns?start_date=2024-01-12"
    )

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["count"] == 1
    assert [row["weekdate"] for row in body["returns"]] == ["2024-01-12"]

    executed_sql, params = portfolio_engine.executed[-1]
    assert "weekdate >= :start_date" in executed_sql
    assert params["start_date"] == date(2024, 1, 12)
    assert "ORDER BY weekdate ASC" in executed_sql


def test_portfolio_returns_history_end_date_filters_later_rows(protected_client, portfolio_engine):
    response = protected_client.get(
        "/v1/stocktrends/portfolios/2/returns?end_date=2024-01-05"
    )

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["count"] == 1
    assert [row["weekdate"] for row in body["returns"]] == ["2024-01-05"]

    executed_sql, params = portfolio_engine.executed[-1]
    assert "weekdate <= :end_date" in executed_sql
    assert params["end_date"] == date(2024, 1, 5)
    assert "ORDER BY weekdate ASC" in executed_sql


def test_portfolio_returns_history_start_and_end_date_work_together(protected_client, portfolio_engine):
    response = protected_client.get(
        "/v1/stocktrends/portfolios/2/returns?start_date=2024-01-06&end_date=2024-01-12"
    )

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["count"] == 1
    assert [row["weekdate"] for row in body["returns"]] == ["2024-01-12"]

    executed_sql, params = portfolio_engine.executed[-1]
    assert "weekdate >= :start_date" in executed_sql
    assert "weekdate <= :end_date" in executed_sql
    assert params["start_date"] == date(2024, 1, 6)
    assert params["end_date"] == date(2024, 1, 12)
    assert "ORDER BY weekdate ASC" in executed_sql


def test_portfolio_positions_history_is_public_and_uses_closed_positions(protected_client, portfolio_engine):
    response = protected_client.get("/v1/stocktrends/portfolios/2/positions/history")

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
    assert body["positions"] == [
        {
            "position_id": 20,
            "symbol": "IBM",
            "exchange": "N",
            "name": "International Business Machines",
            "date_in": "2023-10-06",
            "price_in": 100.0,
            "qty": 100,
            "transaction_cost_in": 9.99,
            "cost_adjustments": 0.0,
            "total_cost": 10009.99,
            "stop_loss": 90.0,
            "date_out": "2024-01-05",
            "weeks_held": 12,
            "sell_trigger": "BC",
            "price_out": 110.0,
            "transaction_cost_out": 9.99,
            "sell_adjustments": 0.0,
            "total_proceeds": 10990.01,
            "gain_loss": 980.02,
            "gain_loss_percent": 9.8,
            "weekdate": "2024-01-05",
        },
        {
            "position_id": 21,
            "symbol": "MSFT",
            "exchange": "Q",
            "name": "Microsoft",
            "date_in": "2023-11-03",
            "price_in": 200.0,
            "qty": 50,
            "transaction_cost_in": 4.99,
            "cost_adjustments": 1.25,
            "total_cost": 10006.24,
            "stop_loss": 180.0,
            "date_out": "2024-01-12",
            "weeks_held": 10,
            "sell_trigger": "PT",
            "price_out": 215.0,
            "transaction_cost_out": 4.99,
            "sell_adjustments": -0.25,
            "total_proceeds": 10744.76,
            "gain_loss": 738.52,
            "gain_loss_percent": 7.38,
            "weekdate": "2024-01-12",
        },
    ]
    assert "LIVE" not in str(body)
    assert "last_update" not in str(body)
    assert "trcost_in" not in str(body)
    assert "gl_percent" not in str(body)

    executed_sql = "\n".join(sql for sql, _params in portfolio_engine.executed)
    assert "FROM stp_ports" in executed_sql
    assert "AND status = 1" in executed_sql
    assert "FROM stp_positions" in executed_sql
    assert "sell_trigger <> ''" in executed_sql
    assert "date_out" in executed_sql
    assert "ORDER BY date_out ASC, position_id ASC" in executed_sql
    assert "last_update" not in executed_sql
    assert "FROM stp_returnslog" not in executed_sql


def test_portfolio_positions_history_with_no_rows_returns_empty_list(protected_client):
    response = protected_client.get("/v1/stocktrends/portfolios/1/positions/history")

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["port_id"] == 1
    assert body["count"] == 0
    assert body["positions"] == []


def test_portfolio_positions_history_start_date_filters_earlier_closed_rows(
    protected_client,
    portfolio_engine,
):
    response = protected_client.get(
        "/v1/stocktrends/portfolios/2/positions/history?start_date=2024-01-12"
    )

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["count"] == 1
    assert [row["position_id"] for row in body["positions"]] == [21]

    executed_sql, params = portfolio_engine.executed[-1]
    assert "date_out >= :start_date" in executed_sql
    assert params["start_date"] == date(2024, 1, 12)
    assert "ORDER BY date_out ASC, position_id ASC" in executed_sql


def test_portfolio_positions_history_end_date_filters_later_closed_rows(
    protected_client,
    portfolio_engine,
):
    response = protected_client.get(
        "/v1/stocktrends/portfolios/2/positions/history?end_date=2024-01-05"
    )

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["count"] == 1
    assert [row["position_id"] for row in body["positions"]] == [20]

    executed_sql, params = portfolio_engine.executed[-1]
    assert "date_out <= :end_date" in executed_sql
    assert params["end_date"] == date(2024, 1, 5)
    assert "ORDER BY date_out ASC, position_id ASC" in executed_sql


def test_portfolio_positions_history_start_and_end_date_work_together(
    protected_client,
    portfolio_engine,
):
    response = protected_client.get(
        "/v1/stocktrends/portfolios/2/positions/history?start_date=2024-01-06&end_date=2024-01-12"
    )

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["count"] == 1
    assert [row["position_id"] for row in body["positions"]] == [21]

    executed_sql, params = portfolio_engine.executed[-1]
    assert "date_out >= :start_date" in executed_sql
    assert "date_out <= :end_date" in executed_sql
    assert params["start_date"] == date(2024, 1, 6)
    assert params["end_date"] == date(2024, 1, 12)
    assert "ORDER BY date_out ASC, position_id ASC" in executed_sql


def test_portfolio_summary_returns_public_history_overview(protected_client, portfolio_engine):
    response = protected_client.get("/v1/stocktrends/portfolios/2/summary")

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    body = response.json()
    assert body["port_id"] == 2
    assert body["portfolio"] == {
        "port_id": 2,
        "name": "TSX 60 Portfolio",
        "selection_universe": "SPTX60",
    }
    assert body["summary"] == {
        "returns": {
            "count": 2,
            "first_weekdate": "2024-01-05",
            "latest_weekdate": "2024-01-12",
            "latest_total_valuation": 12290.12,
            "latest_cumulative_total_gain": 1388.9,
            "latest_cumulative_realized_gain": 1188.89,
        },
        "closed_positions": {
            "count": 2,
            "first_date_in": "2023-10-06",
            "first_date_out": "2024-01-05",
            "latest_date_out": "2024-01-12",
            "total_realized_gain_loss": 1718.54,
            "average_gain_loss_percent": 8.59,
            "winning_positions": 2,
            "losing_positions": 0,
        },
        "verification": {
            "returns_endpoint": "/v1/stocktrends/portfolios/2/returns",
            "historical_positions_endpoint": "/v1/stocktrends/portfolios/2/positions/history",
            "current_live_holdings_excluded": True,
        },
    }
    assert "LIVE" not in str(body)
    assert "IBM" not in str(body)
    assert "MSFT" not in str(body)
    assert "last_update" not in str(body)

    executed_sql = "\n".join(sql for sql, _params in portfolio_engine.executed)
    assert "FROM stp_ports" in executed_sql
    assert "AND status = 1" in executed_sql
    assert "FROM stp_returnslog" in executed_sql
    assert "COUNT(*) AS return_count" in executed_sql
    assert "totalvaluation" in executed_sql
    assert "cum_totalgain" in executed_sql
    assert "cum_realizedgain" in executed_sql
    assert "FROM stp_positions" in executed_sql
    assert "COUNT(*) AS closed_position_count" in executed_sql
    assert "sell_trigger IS NOT NULL" in executed_sql
    assert "sell_trigger <> ''" in executed_sql
    assert "last_update" not in executed_sql


def test_portfolio_summary_start_and_end_date_filter_public_history(
    protected_client,
    portfolio_engine,
):
    response = protected_client.get(
        "/v1/stocktrends/portfolios/2/summary?start_date=2024-01-06&end_date=2024-01-12"
    )

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    summary = response.json()["summary"]
    assert summary["returns"]["count"] == 1
    assert summary["returns"]["first_weekdate"] == "2024-01-12"
    assert summary["returns"]["latest_weekdate"] == "2024-01-12"
    assert summary["returns"]["latest_total_valuation"] == 12290.12
    assert summary["closed_positions"]["count"] == 1
    assert summary["closed_positions"]["first_date_out"] == "2024-01-12"
    assert summary["closed_positions"]["latest_date_out"] == "2024-01-12"
    assert summary["closed_positions"]["total_realized_gain_loss"] == 738.52

    executed_sql = "\n".join(sql for sql, _params in portfolio_engine.executed)
    assert "weekdate >= :start_date" in executed_sql
    assert "weekdate <= :end_date" in executed_sql
    assert "date_out >= :start_date" in executed_sql
    assert "date_out <= :end_date" in executed_sql
    assert portfolio_engine.executed[-2][1]["start_date"] == date(2024, 1, 6)
    assert portfolio_engine.executed[-2][1]["end_date"] == date(2024, 1, 12)
    assert portfolio_engine.executed[-1][1]["start_date"] == date(2024, 1, 6)
    assert portfolio_engine.executed[-1][1]["end_date"] == date(2024, 1, 12)


def test_portfolio_summary_with_empty_public_history_uses_zero_and_null_values(
    protected_client,
    portfolio_engine,
):
    portfolio_engine.return_rows = [
        row for row in portfolio_engine.return_rows if row["port_id"] != 2
    ]
    portfolio_engine.position_rows = [
        row for row in portfolio_engine.position_rows if row["port_id"] != 2
    ]

    response = protected_client.get("/v1/stocktrends/portfolios/2/summary")

    assert response.status_code == 200
    _assert_no_payment_challenge(response)
    summary = response.json()["summary"]
    assert summary["returns"] == {
        "count": 0,
        "first_weekdate": None,
        "latest_weekdate": None,
        "latest_total_valuation": None,
        "latest_cumulative_total_gain": None,
        "latest_cumulative_realized_gain": None,
    }
    assert summary["closed_positions"] == {
        "count": 0,
        "first_date_in": None,
        "first_date_out": None,
        "latest_date_out": None,
        "total_realized_gain_loss": 0.0,
        "average_gain_loss_percent": None,
        "winning_positions": 0,
        "losing_positions": 0,
    }
    assert summary["verification"]["current_live_holdings_excluded"] is True


@pytest.mark.parametrize("port_id", [404, 99])
def test_portfolio_summary_returns_404_for_missing_or_inactive(protected_client, port_id):
    response = protected_client.get(f"/v1/stocktrends/portfolios/{port_id}/summary")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "portfolio_not_found"
    assert response.json()["detail"]["port_id"] == port_id
    _assert_no_payment_challenge(response)


@pytest.mark.parametrize("port_id", [404, 99])
def test_portfolio_returns_history_returns_404_for_missing_or_inactive(protected_client, port_id):
    response = protected_client.get(f"/v1/stocktrends/portfolios/{port_id}/returns")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "portfolio_not_found"
    assert response.json()["detail"]["port_id"] == port_id
    _assert_no_payment_challenge(response)


@pytest.mark.parametrize("port_id", [404, 99])
def test_portfolio_positions_history_returns_404_for_missing_or_inactive(
    protected_client,
    port_id,
):
    response = protected_client.get(f"/v1/stocktrends/portfolios/{port_id}/positions/history")

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


def test_portfolio_positions_history_access_classification_layers_are_public_free():
    path = "/v1/stocktrends/portfolios/2/positions/history"

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
    assert policy_provider.is_public_stocktrends_portfolio_positions_history_path(path)
    assert decision.access_granted is True
    assert decision.is_metered == 0
    assert decision.econ_payment_required == 0
    assert accepted == "none"


def test_portfolio_summary_access_classification_layers_are_public_free():
    path = "/v1/stocktrends/portfolios/2/summary"

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
    assert policy_provider.is_public_stocktrends_portfolio_summary_path(path)
    assert decision.access_granted is True
    assert decision.is_metered == 0
    assert decision.econ_payment_required == 0
    assert accepted == "none"


def test_future_stocktrends_portfolio_child_paths_are_not_public_bypasses(protected_client):
    response = protected_client.get("/v1/stocktrends/portfolios/2/positions")

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing API key"}


def test_future_stocktrends_current_positions_path_is_not_public_bypass(protected_client):
    response = protected_client.get("/v1/stocktrends/portfolios/2/positions/current")

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing API key"}


def test_future_stocktrends_summary_child_paths_are_not_public_bypasses(protected_client):
    response = protected_client.get("/v1/stocktrends/portfolios/2/summary/details")

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
    assert "/stocktrends/portfolios/{port_id}/summary" in paths
    assert "/stocktrends/portfolios/{port_id}/positions/history" in paths
    assert "Official Stock Trends model portfolios" in paths["/stocktrends/portfolios"]["get"]["description"]
    assert "Official Stock Trends model portfolio" in paths["/stocktrends/portfolios/{port_id}"]["get"]["description"]
    returns_description = paths["/stocktrends/portfolios/{port_id}/returns"]["get"]["description"]
    assert "Official Stock Trends portfolio returns history" in returns_description
    assert "stp_returnslog" not in returns_description
    returns_parameters = {
        parameter["name"]: parameter
        for parameter in paths["/stocktrends/portfolios/{port_id}/returns"]["get"]["parameters"]
        if "name" in parameter
    }
    assert returns_parameters["start_date"]["in"] == "query"
    assert returns_parameters["start_date"]["required"] is False
    assert _schema_has_date_format(returns_parameters["start_date"]["schema"])
    assert returns_parameters["end_date"]["in"] == "query"
    assert returns_parameters["end_date"]["required"] is False
    assert _schema_has_date_format(returns_parameters["end_date"]["schema"])

    summary_description = paths["/stocktrends/portfolios/{port_id}/summary"]["get"]["description"]
    assert "Official Stock Trends portfolio public history summary" in summary_description
    assert "Current live holdings are intentionally excluded" in summary_description
    assert "stp_" not in summary_description
    summary_parameters = {
        parameter["name"]: parameter
        for parameter in paths["/stocktrends/portfolios/{port_id}/summary"]["get"]["parameters"]
        if "name" in parameter
    }
    assert summary_parameters["start_date"]["in"] == "query"
    assert summary_parameters["start_date"]["required"] is False
    assert _schema_has_date_format(summary_parameters["start_date"]["schema"])
    assert "weekdates" in summary_parameters["start_date"]["description"]
    assert "closed-position close dates" in summary_parameters["start_date"]["description"]
    assert summary_parameters["end_date"]["in"] == "query"
    assert summary_parameters["end_date"]["required"] is False
    assert _schema_has_date_format(summary_parameters["end_date"]["schema"])
    assert "weekdates" in summary_parameters["end_date"]["description"]
    assert "closed-position close dates" in summary_parameters["end_date"]["description"]

    positions_description = paths["/stocktrends/portfolios/{port_id}/positions/history"]["get"]["description"]
    assert "Official Stock Trends historical closed-position records" in positions_description
    assert "Current live holdings are intentionally excluded" in positions_description
    assert "stp_positions" not in positions_description
    positions_parameters = {
        parameter["name"]: parameter
        for parameter in paths["/stocktrends/portfolios/{port_id}/positions/history"]["get"]["parameters"]
        if "name" in parameter
    }
    assert positions_parameters["start_date"]["in"] == "query"
    assert positions_parameters["start_date"]["required"] is False
    assert _schema_has_date_format(positions_parameters["start_date"]["schema"])
    assert positions_parameters["end_date"]["in"] == "query"
    assert positions_parameters["end_date"]["required"] is False
    assert _schema_has_date_format(positions_parameters["end_date"]["schema"])
