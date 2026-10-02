from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routing import install_payment_execution_boundary
from discovery.endpoint_metadata import get_endpoint_metadata
from payments.policy_provider import _default_policy_config
from routers import market
from services import epoch_queries
from utils.history_bounds import HISTORY_ENDPOINT_BOUNDS


_LATEST_ROW = {
    "weekdate": date(2026, 9, 25),
    "model_version": "epoch_v1_2026-09-25",
    "model_payload_sha256": "persisted-payload-sha",
    "classifier_code_sha": "persisted-code-sha",
    "raw_cluster_id": 0,
    "epoch_id": "BROAD_BULLISH",
    "epoch_name": "Broad Bullish",
    "assigned_distance": 0.123456,
    "second_nearest_distance": 1.234567,
    "separation_margin": 1.111111,
    "bullish_ratio": 0.765432,
    "avg_mt_cnt_bull": 12.345678,
    "avg_mt_cnt_bear": 4.567891,
    "avg_trend_cnt": 5.678912,
    "pct_trend_cnt_ge_4": 0.654321,
    "rsi_median": 102.345678,
    "classified_count": 3210,
    "rsi_valid_count": 3200,
    "previous_raw_cluster_id": 2,
    "previous_epoch_id": "BULLISH_MATURITY",
    "weeks_in_epoch": 16,
    "changed_this_week": 0,
    "classified_at_unix": Decimal("1790886903.176735"),
}


def _client(monkeypatch, *, latest=_LATEST_ROW, history=None) -> TestClient:
    app = FastAPI()
    app.include_router(market.router, prefix="/v1")
    install_payment_execution_boundary(app)
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = MagicMock()
    engine.connect.return_value.__exit__.return_value = False
    monkeypatch.setattr(market, "get_engine", lambda: engine)
    monkeypatch.setattr(epoch_queries, "fetch_latest_epoch_row", lambda _conn: latest)
    monkeypatch.setattr(
        epoch_queries,
        "fetch_epoch_history_rows",
        lambda _conn, **_kwargs: list(history if history is not None else [latest]),
    )
    return TestClient(app)


def test_epoch_queries_read_only_frozen_persisted_table_with_bound_parameters(monkeypatch):
    captured: list[tuple[str, dict]] = []

    class Result:
        def mappings(self):
            return self

        def first(self):
            return None

        def all(self):
            return []

    class Connection:
        def execute(self, statement, params):
            captured.append((str(statement), dict(params)))
            return Result()

    monkeypatch.setattr(epoch_queries, "text", lambda sql: sql)
    conn = Connection()
    epoch_queries.fetch_latest_epoch_row(conn)
    epoch_queries.fetch_epoch_history_rows(
        conn, limit=53, start_date=date(2020, 1, 3), end_date=date(2021, 1, 1)
    )

    latest_sql, latest_params = captured[0]
    history_sql, history_params = captured[1]
    for sql in (latest_sql, history_sql):
        assert "FROM st_market_epoch" in sql
        assert "model_version = :model_version" in sql
        assert "UNIX_TIMESTAMP(classified_at) AS classified_at_unix" in sql
        assert "CONVERT_TZ(" not in sql
        assert "st_data" not in sql
        assert "stdata." not in sql
        assert "INSERT" not in sql
        assert "UPDATE" not in sql
        assert "DELETE" not in sql
    assert "ORDER BY weekdate DESC" in latest_sql
    assert "LIMIT 1" in latest_sql
    assert latest_params == {"model_version": epoch_queries.EPOCH_V1_MODEL_VERSION}
    assert "weekdate >= :start_date" in history_sql
    assert "weekdate <= :end_date" in history_sql
    assert "LIMIT :limit" in history_sql
    assert history_params == {
        "model_version": epoch_queries.EPOCH_V1_MODEL_VERSION,
        "limit": 53,
        "start_date": date(2020, 1, 3),
        "end_date": date(2021, 1, 1),
    }


def test_latest_returns_persisted_snapshot_without_recomputation(monkeypatch):
    response = _client(monkeypatch).get("/v1/market/epoch/latest")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["epoch_id"] == _LATEST_ROW["epoch_id"]
    assert body["epoch_name"] == _LATEST_ROW["epoch_name"]
    assert body["weeks_in_epoch"] == _LATEST_ROW["weeks_in_epoch"]
    assert body["changed_this_week"] == 0
    assert isinstance(body["changed_this_week"], int)
    assert body["assigned_distance"] == _LATEST_ROW["assigned_distance"]
    assert body["model_payload_sha256"] == _LATEST_ROW["model_payload_sha256"]
    assert body["classifier_code_sha"] == _LATEST_ROW["classifier_code_sha"]
    assert body["weekdate"] == "2026-09-25"
    assert body["classified_at"] == "2026-10-01T20:35:03.176735+00:00"
    assert "classified_at_unix" not in body


def test_epoch_snapshot_converts_unix_timestamp_to_exact_utc_datetime():
    snapshot = market._epoch_snapshot({"classified_at_unix": Decimal("1790886903.176735")})
    assert snapshot["classified_at"] == datetime(
        2026, 10, 1, 20, 35, 3, 176735, tzinfo=timezone.utc
    )
    assert "classified_at_unix" not in snapshot


def test_epoch_snapshot_preserves_null_classified_at():
    assert market._epoch_snapshot({"classified_at_unix": None}) == {"classified_at": None}


def test_latest_and_history_first_snapshot_are_identical(monkeypatch):
    client = _client(monkeypatch)
    latest = client.get("/v1/market/epoch/latest").json()
    history = client.get("/v1/market/epoch/history?limit=1").json()["history"][0]
    assert history == latest


def test_history_bounds_ordering_and_no_data_behavior(monkeypatch):
    prior = {**_LATEST_ROW, "weekdate": date(2026, 9, 18), "weeks_in_epoch": 15}
    client = _client(monkeypatch, history=[_LATEST_ROW, prior])
    response = client.get("/v1/market/epoch/history?start_date=2026-09-01&end_date=2026-09-25")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["weekdate"] for row in body["history"]] == ["2026-09-25", "2026-09-18"]
    assert body["limit"] == 52
    assert body["start_date"] == "2026-09-01"
    assert body["end_date"] == "2026-09-25"
    assert body["applied_bounds"]["truncated_by_limit"] is False
    assert body["applied_bounds"]["max_limit"] == 2600
    invalid_range = client.get(
        "/v1/market/epoch/history?start_date=2026-09-25&end_date=2026-09-18"
    )
    assert invalid_range.status_code == 422
    assert invalid_range.json()["detail"]["error"] == "invalid_date_range"

    empty = _client(monkeypatch, latest=None, history=[])
    assert empty.get("/v1/market/epoch/latest").json()["detail"]["error"] == "no_signal_data"
    assert empty.get("/v1/market/epoch/history").json()["detail"]["error"] == "no_signal_data"


def test_epoch_openapi_discovery_and_payment_policy_contract(monkeypatch):
    client = _client(monkeypatch)
    schema = client.get("/openapi.json").json()
    assert "/v1/market/epoch/latest" in schema["paths"]
    history = schema["paths"]["/v1/market/epoch/history"]["get"]
    parameters = {item["name"]: item for item in history["parameters"]}
    assert parameters["limit"]["schema"]["default"] == 52
    assert parameters["limit"]["schema"]["maximum"] == 2600
    assert {"start_date", "end_date"} <= set(parameters)
    assert "forward-looking" not in history["description"].lower()
    assert "not a probability or forecast confidence" in history["description"].lower()

    for path, name in (("/v1/market/epoch/latest", "market_epoch_latest"), ("/v1/market/epoch/history", "market_epoch_history")):
        metadata = get_endpoint_metadata(path, "GET")
        assert metadata is not None
        assert metadata["tool_name"] == name
        assert metadata["analytical_role"] == "market_epoch_classifier"
        assert "forecast confidence" in " ".join(metadata["notes"]).lower()
        policy = next(p for p in _default_policy_config().endpoint_payment_policies if p.path_pattern == path)
        assert policy.endpoint_id == name
        assert policy.pricing_rule_id == name
        assert policy.allowed_rails == ("subscription", "x402", "mpp")

    history_shape = get_endpoint_metadata("/v1/market/epoch/history", "GET")["response_shape"]
    for field in (
        "bullish_ratio", "avg_mt_cnt_bull", "avg_mt_cnt_bear", "avg_trend_cnt",
        "pct_trend_cnt_ge_4", "rsi_median", "classified_count", "rsi_valid_count",
        "previous_epoch_id", "previous_raw_cluster_id",
    ):
        assert f"history[].{field}" in history_shape

    assert HISTORY_ENDPOINT_BOUNDS["/v1/market/epoch/history"] == {
        "default_limit": 52, "max_limit": 2600, "default_window_weeks": None,
    }


def test_epoch_semantic_contract_and_static_history_shape_are_aligned():
    contract = (Path(__file__).parents[1] / "docs" / "STOCK_TRENDS_SEMANTIC_CONTRACT.md").read_text(encoding="utf-8")
    for term in (
        "## Market Epoch v1", "epoch_id", "epoch_name", "raw_cluster_id", "weeks_in_epoch",
        "changed_this_week", "assigned_distance", "second_nearest_distance", "separation_margin",
        "classified_at", "UTC provenance timestamp",
        "BROAD_BULLISH", "BEARISH_MATURITY", "BULLISH_MATURITY", "persisted `0` or `1` flag",
        "not a trade signal", "not a forward-return forecast", "not causal AI",
    ):
        assert term in contract

    manifest = json.loads((Path(__file__).parents[1] / "static" / "tools.json").read_text(encoding="utf-8"))
    static_history = next(tool for tool in manifest["tools"] if tool["name"] == "market_epoch_history")
    assert set(get_endpoint_metadata("/v1/market/epoch/history", "GET")["response_shape"]) == set(static_history["response_shape"])
