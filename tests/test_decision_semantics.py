from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import decision
from services.market_semantics import CANONICAL_REPORTING_EXCHANGES


_WEEKDATE = date(2026, 9, 25)
_REGIME_ROWS = [
    {"weekdate": _WEEKDATE, "trend": "^+", "cnt": 3},  # CS + UN combined
    {"weekdate": _WEEKDATE, "trend": "^v", "cnt": 1},
    {"weekdate": _WEEKDATE, "trend": "--", "cnt": 1000},
    {"weekdate": _WEEKDATE, "trend": None, "cnt": 1000},
]


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


def _symbol_row(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "exchange": "N",
        "trend": "^+",
        "trend_cnt": 6,
        "mt_cnt": 8,
        "rsi": 120,
        "rsi_updn": "U",
        "vol_tag": "",
        "weekdate": _WEEKDATE,
    }


def _client(monkeypatch, eligible_symbols: set[str]):
    captured_params: list[dict] = []

    class _Connection:
        def execute(self, _sql, params):
            captured_params.append(params)
            symbol = params["symbol"]
            return _Result(_symbol_row(symbol) if symbol in eligible_symbols else None)

    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = _Connection()
    engine.connect.return_value.__exit__.return_value = False
    monkeypatch.setattr(decision, "get_engine", lambda: engine)
    monkeypatch.setattr(
        decision.regime_queries,
        "fetch_eligible_regime_weekdates",
        lambda _conn, **_kwargs: [_WEEKDATE],
    )
    monkeypatch.setattr(
        decision.regime_queries,
        "fetch_regime_trend_aggregates",
        lambda _conn, **_kwargs: list(_REGIME_ROWS),
    )

    app = FastAPI()
    app.include_router(decision.router, prefix="/v1")
    return TestClient(app), captured_params


def test_decision_uses_canonical_regime_and_accepts_cs_and_un_symbols(monkeypatch):
    client, captured_params = _client(monkeypatch, {"CSYM", "UNSYM"})

    cs_response = client.post("/v1/decision/evaluate-symbol", json={"symbol_exchange": "CSYM-N"})
    un_response = client.post("/v1/decision/evaluate-symbol", json={"symbol_exchange": "UNSYM-N"})

    assert cs_response.status_code == 200, cs_response.text
    assert un_response.status_code == 200, un_response.text
    for response in (cs_response, un_response):
        body = response.json()
        assert body["regime_context"]["regime_score"] == 0.5
        assert body["regime_context"]["current_regime"] == "bullish"
        assert body["symbol_context"]["symbol_bias"] == "bullish"
        assert body["alignment"] == "aligned"
        assert body["decision_score"] > 0
    assert all(
        {params["symbol_type_0"], params["symbol_type_1"]} == {"CS", "UN"}
        for params in captured_params
    )


def test_decision_excludes_tf_requested_symbol_with_existing_not_found_contract(monkeypatch):
    client, _ = _client(monkeypatch, {"CSYM", "UNSYM"})

    response = client.post("/v1/decision/evaluate-symbol", json={"symbol_exchange": "ETF-N"})

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "symbol_not_found"


def test_decision_regime_score_ignores_neutral_and_unknown_rows(monkeypatch):
    client, _ = _client(monkeypatch, {"CSYM"})

    response = client.post("/v1/decision/evaluate-symbol", json={"symbol_exchange": "CSYM-N"})

    assert response.status_code == 200
    # (3 bullish - 1 bearish) / (3 bullish + 1 bearish), matching market forecast.
    assert response.json()["regime_context"]["regime_score"] == 0.5


def test_decision_regime_context_excludes_bats_from_the_shared_reporting_universe(monkeypatch):
    reporting_rows = [
        {"weekdate": _WEEKDATE, "exchange": "N", "trend": "^+", "cnt": 3},
        {"weekdate": _WEEKDATE, "exchange": "T", "trend": "^v", "cnt": 1},
    ]
    bats_row = {"weekdate": _WEEKDATE, "exchange": "B", "trend": "^v", "cnt": 100}
    filtered_rows = [
        {key: value for key, value in row.items() if key != "exchange"}
        for row in [*reporting_rows, bats_row]
        if row["exchange"] in CANONICAL_REPORTING_EXCHANGES
    ]
    client, _ = _client(monkeypatch, {"CSYM"})
    monkeypatch.setattr(
        decision.regime_queries,
        "fetch_regime_trend_aggregates",
        lambda _conn, **_kwargs: filtered_rows,
    )

    response = client.post("/v1/decision/evaluate-symbol", json={"symbol_exchange": "CSYM-N"})

    assert response.status_code == 200
    assert response.json()["regime_context"]["regime_score"] == 0.5
