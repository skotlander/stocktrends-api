from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import market
from services import market_semantics, regime_queries


_LATEST = date(2026, 9, 25)
_PRIOR = date(2026, 9, 18)
_BATS_ONLY_NEWER = date(2026, 10, 2)


def _aggregate_row(
    weekdate: date,
    trend: str | None,
    cnt: int,
    *,
    valid_rsi_sum: float = 0,
    valid_rsi_count: int = 0,
    mt_cnt_sum: float = 0,
    mt_cnt_count: int = 0,
) -> dict:
    return {
        "weekdate": weekdate,
        "trend": trend,
        "cnt": cnt,
        "valid_rsi_sum": valid_rsi_sum,
        "valid_rsi_count": valid_rsi_count,
        "mt_cnt_sum": mt_cnt_sum,
        "mt_cnt_count": mt_cnt_count,
    }


_LATEST_ROWS = [
    # CS and UN directional observations are already combined by the canonical
    # query; TF observations are deliberately absent from this result.
    # Classified mt_cnt values are 10, 20, 30, and 40. One classified RSI is
    # null and another is 10001, while 600 remains valid under the conservative
    # aggregate data-quality ceiling. The valid classified RSI values are 600
    # and 300.
    _aggregate_row(_LATEST, "^+", 3, valid_rsi_sum=600, valid_rsi_count=1, mt_cnt_sum=60, mt_cnt_count=3),
    _aggregate_row(_LATEST, "^v", 1, valid_rsi_sum=300, valid_rsi_count=1, mt_cnt_sum=40, mt_cnt_count=1),
    # These rows intentionally carry valid, distinct RSI and large nonzero
    # maturity values. They must not affect classified market aggregates.
    _aggregate_row(_LATEST, "--", 10_000, valid_rsi_sum=4_000_000, valid_rsi_count=10_000, mt_cnt_sum=1_000_000, mt_cnt_count=10_000),
    _aggregate_row(_LATEST, "=", 2, valid_rsi_sum=820, valid_rsi_count=2, mt_cnt_sum=400, mt_cnt_count=2),
    _aggregate_row(_LATEST, None, 3, valid_rsi_sum=1_260, valid_rsi_count=3, mt_cnt_sum=900, mt_cnt_count=3),
    _aggregate_row(_LATEST, "unknown", 4, valid_rsi_sum=1_720, valid_rsi_count=4, mt_cnt_sum=1_200, mt_cnt_count=4),
]
_PRIOR_ROWS = [
    _aggregate_row(_PRIOR, "^+", 1, valid_rsi_sum=100, valid_rsi_count=1, mt_cnt_sum=2, mt_cnt_count=1),
    _aggregate_row(_PRIOR, "^v", 1, valid_rsi_sum=100, valid_rsi_count=1, mt_cnt_sum=2, mt_cnt_count=1),
]


def _client_with_query_results(monkeypatch, *, weekdates, rows):
    app = FastAPI()
    app.include_router(market.router, prefix="/v1")
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = MagicMock()
    engine.connect.return_value.__exit__.return_value = False
    monkeypatch.setattr(market, "get_engine", lambda: engine)
    monkeypatch.setattr(
        market.regime_queries,
        "fetch_eligible_regime_weekdates",
        lambda _conn, **kwargs: list(weekdates)[: kwargs["limit"]],
    )
    monkeypatch.setattr(
        market.regime_queries,
        "fetch_regime_trend_aggregates",
        lambda _conn, **kwargs: [
            row for row in rows if row["weekdate"] in set(kwargs["weekdates"])
        ],
    )
    return TestClient(app)


def test_regime_query_helpers_select_only_canonical_equities_and_classified_weeks(monkeypatch):
    captured: list[tuple[str, dict]] = []

    class _Result:
        def mappings(self):
            return self

        def all(self):
            return []

    class _Connection:
        def execute(self, sql, params):
            captured.append((sql, params))
            return _Result()

    monkeypatch.setattr(regime_queries, "text", lambda sql: sql)
    conn = _Connection()
    regime_queries.fetch_eligible_regime_weekdates(conn, limit=1)
    regime_queries.fetch_regime_trend_aggregates(conn, weekdates=[_LATEST])

    eligible_sql, eligible_params = captured[0]
    aggregate_sql, aggregate_params = captured[1]
    assert "type IN" in eligible_sql
    assert "exchange IN" in eligible_sql
    assert "trend IN" in eligible_sql
    assert set(value for key, value in eligible_params.items() if key.startswith("equity_type_")) == {"CS", "UN"}
    assert "TF" not in eligible_params.values()
    assert set(value for key, value in eligible_params.items() if key.startswith("reporting_exchange_")) == {"A", "N", "Q", "T"}
    assert "B" not in eligible_params.values()
    assert set(value for key, value in eligible_params.items() if key.startswith("classified_trend_")) == {
        "^+", "^-", "v^", "^v", "v+", "v-"
    }
    assert "type IN" in aggregate_sql
    assert "exchange IN" in aggregate_sql
    assert "rsi IS NOT NULL" in aggregate_sql
    assert "rsi <= :rsi_max_valid" in aggregate_sql
    assert aggregate_params["rsi_max_valid"] == 10_000
    assert set(value for key, value in aggregate_params.items() if key.startswith("equity_type_")) == {"CS", "UN"}
    assert set(value for key, value in aggregate_params.items() if key.startswith("reporting_exchange_")) == {"A", "N", "Q", "T"}
    assert "B" not in aggregate_params.values()
    assert "trend IN" not in aggregate_sql
    assert not any(key.startswith("classified_trend_") for key in aggregate_params)


def test_reporting_exchange_query_scope_excludes_bats_and_bats_only_newer_week(monkeypatch):
    raw_rows = [
        {"weekdate": _LATEST, "exchange": "N", "type": "CS", "trend": "^+", "cnt": 3},
        {"weekdate": _LATEST, "exchange": "T", "type": "UN", "trend": "^v", "cnt": 1},
        {"weekdate": _LATEST, "exchange": "N", "type": "CS", "trend": "--", "cnt": 1_000},
        {"weekdate": _LATEST, "exchange": "Q", "type": "UN", "trend": None, "cnt": 1_000},
        # These would turn the result strongly bearish if BATS were included.
        {"weekdate": _LATEST, "exchange": "B", "type": "CS", "trend": "^v", "cnt": 100},
        {"weekdate": _LATEST, "exchange": "N", "type": "TF", "trend": "^v", "cnt": 100},
        # A classified BATS-only newer week must not become the resolved week.
        {"weekdate": _BATS_ONLY_NEWER, "exchange": "B", "type": "CS", "trend": "^+", "cnt": 100},
    ]

    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self

        def all(self):
            return self.rows

    class _Connection:
        def execute(self, sql, params):
            types = {value for key, value in params.items() if key.startswith("equity_type_")}
            exchanges = {
                value for key, value in params.items() if key.startswith("reporting_exchange_")
            }
            selected = [
                row for row in raw_rows
                if row["type"] in types and row["exchange"] in exchanges
            ]
            if "SELECT DISTINCT weekdate" in sql:
                trends = {
                    value for key, value in params.items() if key.startswith("classified_trend_")
                }
                weekdates = sorted(
                    {row["weekdate"] for row in selected if row["trend"] in trends},
                    reverse=True,
                )[:params["limit"]]
                return _Result([{"weekdate": weekdate} for weekdate in weekdates])

            selected_weekdates = {
                value for key, value in params.items() if key.startswith("weekdate_")
            }
            grouped: dict[tuple[date, str | None], int] = {}
            for row in selected:
                if row["weekdate"] in selected_weekdates:
                    key = (row["weekdate"], row["trend"])
                    grouped[key] = grouped.get(key, 0) + row["cnt"]
            return _Result([
                {
                    "weekdate": weekdate,
                    "trend": trend,
                    "cnt": count,
                    "valid_rsi_sum": 0,
                    "valid_rsi_count": 0,
                    "mt_cnt_sum": 0,
                    "mt_cnt_count": 0,
                }
                for (weekdate, trend), count in grouped.items()
            ])

    monkeypatch.setattr(regime_queries, "text", lambda sql: sql)
    conn = _Connection()
    weekdates = regime_queries.fetch_eligible_regime_weekdates(conn, limit=1)
    aggregates = regime_queries.fetch_regime_trend_aggregates(conn, weekdates=weekdates)

    assert weekdates == [_LATEST]
    assert market_semantics.CANONICAL_REPORTING_EXCHANGES == ("A", "N", "Q", "T")
    assert sum(row["cnt"] for row in aggregates if row["trend"] == "^+") == 3
    assert sum(row["cnt"] for row in aggregates if row["trend"] == "^v") == 1


def test_latest_uses_classified_canonical_population_and_transparency(monkeypatch):
    client = _client_with_query_results(monkeypatch, weekdates=[_LATEST], rows=_LATEST_ROWS)

    response = client.get("/v1/market/regime/latest")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["population"] == "equities"
    assert body["bullish_pct"] == 0.75
    assert body["bearish_pct"] == 0.25
    assert body["regime_score"] == 0.5
    assert body["classified_count"] == 4
    assert body["signal_count"] == 4
    assert body["observed_count"] == 10_013
    assert body["neutral_count"] == 10_002
    assert body["unclassified_count"] == 7
    assert body["bullish_pct"] + body["bearish_pct"] == 1


def test_latest_and_history_agree_for_the_same_week(monkeypatch):
    client = _client_with_query_results(monkeypatch, weekdates=[_LATEST], rows=_LATEST_ROWS)

    latest = client.get("/v1/market/regime/latest").json()
    history = client.get("/v1/market/regime/history?limit=1").json()["history"][0]

    for field in (
        "regime_score", "signal_count", "classified_count", "observed_count",
        "neutral_count", "unclassified_count", "population",
    ):
        assert history[field] == latest[field]


def test_forecast_current_score_uses_the_same_canonical_population(monkeypatch):
    rows = _LATEST_ROWS + _PRIOR_ROWS
    client = _client_with_query_results(monkeypatch, weekdates=[_LATEST, _PRIOR], rows=rows)

    latest = client.get("/v1/market/regime/latest").json()
    history = client.get("/v1/market/regime/history?limit=2").json()["history"][0]
    forecast = client.get("/v1/market/regime/forecast?lookback=2").json()

    assert forecast["current_regime_score"] == latest["regime_score"]
    assert forecast["current_regime_score"] == history["regime_score"]


def test_neutral_unknown_and_invalid_rsi_are_isolated_from_market_aggregates(monkeypatch):
    client = _client_with_query_results(monkeypatch, weekdates=[_LATEST], rows=_LATEST_ROWS)

    response = client.get("/v1/market/regime/latest")

    assert response.status_code == 200
    body = response.json()
    # The valid classified RSI values are 600 and 300. Neutral/unknown
    # valid RSI values are deliberately distinct and would change this result
    # under the previous all-observed aggregation.
    assert body["avg_rsi"] == 450.0
    # All four classified mt_cnt values remain, including the rows with null
    # and >10000 RSI. Neutral/unknown maturity must not contribute.
    assert body["avg_mt_cnt"] == 25.0
    assert body["classified_count"] == 4
    assert body["observed_count"] == 10_013


def test_route_classification_continues_to_use_regime_service_thresholds(monkeypatch):
    rows = [
        _aggregate_row(_LATEST, "^+", 1),
        _aggregate_row(_LATEST, "^v", 9),
    ]
    client = _client_with_query_results(monkeypatch, weekdates=[_LATEST], rows=rows)

    response = client.get("/v1/market/regime/latest")

    assert response.status_code == 200
    assert response.json()["regime"] == "bearish"
