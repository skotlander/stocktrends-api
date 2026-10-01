from __future__ import annotations

import inspect
from datetime import date
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import portfolio
from services.market_semantics import CANONICAL_REPORTING_EXCHANGES


_WEEKDATE = date(2026, 9, 25)


def _regime_context() -> dict:
    return {
        "weekdates": [_WEEKDATE],
        "latest_weekdate": _WEEKDATE,
        "scores_by_week": [(_WEEKDATE, 0.5)],
        "current_regime": "bullish",
        "current_regime_score": 0.5,
        "regime_confidence": "high",
        "forecast": {
            "forecast_regime": "bullish",
            "avg_delta": 0.0,
            "recent_direction": "stable",
        },
        "consistency_pct": 1.0,
        "forecast_confidence": "high",
    }


def _candidate() -> dict:
    return {
        "symbol": "CSYM",
        "exchange": "N",
        "trend": "^+",
        "trend_cnt": 6,
        "mt_cnt": 8,
        "rsi": 120,
        "rsi_updn": "U",
        "vol_tag": "",
        "weekdate": _WEEKDATE,
    }


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


def _client(monkeypatch, query_results):
    connection = MagicMock()
    connection.execute.side_effect = [_Result(rows) for rows in query_results]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    engine.connect.return_value.__exit__.return_value = False
    monkeypatch.setattr(portfolio, "get_engine", lambda: engine)
    monkeypatch.setattr(portfolio, "text", lambda sql: sql)
    monkeypatch.setattr(portfolio, "_load_regime_context", lambda *_args: _regime_context())
    app = FastAPI()
    app.include_router(portfolio.router, prefix="/v1")
    return TestClient(app), connection


def test_construct_uses_canonical_context_but_keeps_cs_candidate_query(monkeypatch):
    client, connection = _client(monkeypatch, [[_candidate()], []])

    response = client.post("/v1/portfolio/construct", json={"count": 1})

    assert response.status_code == 200, response.text
    assert response.json()["regime_context"]["regime_score"] == 0.5
    candidate_sql = str(connection.execute.call_args_list[0].args[0])
    assert "type = 'CS'" in candidate_sql


def test_evaluate_and_compare_keep_cs_position_lookups_with_canonical_context(monkeypatch):
    position = {"symbol_exchange": "CSYM-N", "weight": 1.0}
    client, connection = _client(monkeypatch, [[_candidate()], [_candidate()]])

    evaluate = client.post("/v1/portfolio/evaluate", json={"positions": [position]})
    compare = client.post(
        "/v1/portfolio/compare",
        json={"left": [position], "right": [position]},
    )

    assert evaluate.status_code == 200, evaluate.text
    assert compare.status_code == 200, compare.text
    assert evaluate.json()["regime_context"]["regime_score"] == 0.5
    assert compare.json()["regime_context"]["regime_score"] == 0.5
    for call in connection.execute.call_args_list:
        assert "type = 'CS'" in str(call.args[0])


def test_portfolio_context_helper_uses_shared_canonical_query_layer(monkeypatch):
    calls: list[str] = []
    conn = MagicMock()
    raw_rows = [
        {"weekdate": _WEEKDATE, "exchange": "N", "trend": "^+", "cnt": 3},
        {"weekdate": _WEEKDATE, "exchange": "T", "trend": "^v", "cnt": 1},
        {"weekdate": _WEEKDATE, "exchange": "N", "trend": "--", "cnt": 1000},
        {"weekdate": _WEEKDATE, "exchange": "B", "trend": "^v", "cnt": 100},
    ]
    rows = [
        {key: value for key, value in row.items() if key != "exchange"}
        for row in raw_rows
        if row["exchange"] in CANONICAL_REPORTING_EXCHANGES
    ]
    monkeypatch.setattr(
        portfolio.regime_queries,
        "fetch_eligible_regime_weekdates",
        lambda *_args, **_kwargs: calls.append("weeks") or [_WEEKDATE],
    )
    monkeypatch.setattr(
        portfolio.regime_queries,
        "fetch_regime_trend_aggregates",
        lambda *_args, **_kwargs: calls.append("aggregates") or rows,
    )

    context = portfolio._load_regime_context(conn, None)

    assert calls == ["weeks", "aggregates"]
    assert context["current_regime_score"] == 0.5


def test_portfolio_source_has_no_cs_only_regime_sql():
    source = inspect.getsource(portfolio)
    assert source.count("fetch_eligible_regime_weekdates") == 1
    assert source.count("fetch_regime_trend_aggregates") == 1
    assert "Candidate selection is intentionally CS-only" in source
    assert "Supplied portfolio positions remain an explicit CS-only universe" in source
