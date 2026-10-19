"""Synthetic regression tests for the published selection contract."""
import pytest
import sqlite3
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import selections, selections_published


@pytest.fixture
def client(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("CREATE TABLE st_select (weekdate TEXT, exchange TEXT, symbol TEXT, prob13wk REAL)")
        conn.execute("CREATE TABLE st_data (weekdate TEXT, exchange TEXT, symbol TEXT, type TEXT)")
        columns = ", ".join(f"{name} REAL" for name in (
            "x4wk1", "x4wk", "x4wk2", "x4wksd", "x13wk1", "x13wk", "x13wk2", "x13wksd",
            "x40wk1", "x40wk", "x40wk2", "x40wksd"))
        conn.execute(f"CREATE TABLE st_returnmeans (weekdate TEXT, exchange TEXT, symbol TEXT, {columns})")
        for symbol, prob, x4, x13, x40, typ in [
            ("LOW", 54.99, 1, 3, 7, "CS"), ("EDGE", 55, 1, 3, 7, "CS"),
            ("ETF", 55.01, 1, 3, 7, "TF"), ("X4", 70, 0, 3, 7, "CS"),
            ("X13", 70, 1, 2.19, 7, "CS"), ("X40", 70, 1, 3, 6.45, "CS"),
        ]:
            args = dict(symbol=symbol, prob=prob, x4=x4, x13=x13, x40=x40, typ=typ)
            conn.execute("INSERT INTO st_select VALUES ('2026-10-16', 'N', :symbol, :prob)", args)
            conn.execute("INSERT INTO st_data VALUES ('2026-10-16', 'N', :symbol, :typ)", args)
            conn.execute("INSERT INTO st_returnmeans (weekdate, exchange, symbol, x4wk1, x13wk1, x40wk1) VALUES ('2026-10-16', 'N', :symbol, :x4, :x13, :x40)", args)
    class Result:
        def __init__(self, cursor): self.cursor = cursor
        def mappings(self): return self
        def first(self): return self.cursor.fetchone()
        def all(self): return self.cursor.fetchall()
    class Engine:
        def connect(self): return self
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def execute(self, sql, params=None): return Result(conn.execute(str(sql), params or {}))
    monkeypatch.setattr(selections_published, "get_engine", lambda: Engine())
    monkeypatch.setattr(selections_published, "text", lambda sql: sql)
    monkeypatch.setattr(selections, "get_engine", lambda: Engine())
    monkeypatch.setattr(selections, "text", lambda sql: sql)
    app = FastAPI()
    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "test"
        return await call_next(request)
    app.include_router(selections_published.router, prefix="/v1")
    app.include_router(selections.router, prefix="/v1")
    with TestClient(app) as test_client:
        yield test_client
    conn.close()


@pytest.mark.parametrize("path", ["latest", "history"])
def test_published_boundaries_and_common_stock_membership(client, path):
    url = f"/v1/selections/published/{path}"
    response = client.get(url).json()
    assert [row["symbol"] for row in response["data"]] == ["ETF", "EDGE"]
    assert response["cs_only"] is False
    assert (response["min_prob13wk"], response["min_x4wk1"], response["min_x13wk1"], response["min_x40wk1"]) == (55, 0, 2.19, 6.45)
    assert [row["symbol"] for row in client.get(url, params={"cs_only": "true"}).json()["data"]] == ["EDGE"]


@pytest.mark.parametrize("path", ["latest", "history"])
def test_legacy_and_override_contract(client, path):
    url = f"/v1/selections/published/{path}"
    legacy = {"min_prob13wk": "0.55", "min_x4wk1": "0", "min_x13wk1": "2.19", "min_x40wk1": "6.45"}
    assert client.get(url, params=legacy).json()["count"] == 2
    for name in legacy:
        assert client.get(url, params={**legacy, name: "99"}).status_code == 422
    assert client.get(url + "?min_prob13wk=0.55&min_prob13wk=0").status_code == 422
    parameters = client.get("/openapi.json").json()["paths"][url]["get"]["parameters"]
    assert not set(legacy) & {p["name"] for p in parameters}


@pytest.mark.parametrize("path", ["latest", "history"])
def test_base_probability_is_optional_and_on_percentage_scale(client, path):
    url = f"/v1/selections/{path}"
    all_rows = client.get(url, params={"cs_only": "false"}).json()
    assert all_rows["count"] == 6
    filtered = client.get(url, params={"cs_only": "false", "min_prob13wk": "55"}).json()
    assert filtered["count"] == 5
    assert client.get(url, params={"cs_only": "false", "min_prob13wk": "0.55"}).json()["count"] == 6
    for invalid in ("-1", "101", "NaN", "inf"):
        assert client.get(url, params={"min_prob13wk": invalid}).status_code == 422
