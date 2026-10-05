"""Regression coverage for selection-universe CS filtering."""

from __future__ import annotations

import pytest
from starlette.requests import Request

import routers.selections as selections
import routers.selections_published as selections_published


_WEEKDATE = "2026-09-25"
_ROWS = [
    {"weekdate": _WEEKDATE, "exchange": "N", "symbol": "COMMON", "prob13wk": 0.80, "type": "CS"},
    {"weekdate": _WEEKDATE, "exchange": "N", "symbol": "UNIT", "prob13wk": 0.75, "type": "UN"},
    {"weekdate": _WEEKDATE, "exchange": "N", "symbol": "FUND", "prob13wk": 0.70, "type": "TF"},
]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _SelectionEngine:
    """Small in-memory stand-in that makes the SQL universe predicate observable."""

    def __init__(self):
        self.executed: list[tuple[str, dict]] = []

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        bound = dict(params or {})
        self.executed.append((sql, bound))
        if "MAX(weekdate)" in sql:
            return _Result([{"weekdate": _WEEKDATE}])

        has_cs_universe_predicate = (
            "EXISTS (" in sql
            and "FROM st_data cs_filter" in sql
            and "cs_filter.type = 'CS'" in sql
        )
        # Model the outer SQL condition, not just the parameter: omitting the
        # predicate while cs_only=1 returns the complete fixture universe and
        # therefore fails the route-level response assertion below.
        rows = (
            [row for row in _ROWS if row["type"] == "CS"]
            if bound.get("cs_only") and has_cs_universe_predicate
            else list(_ROWS)
        )
        return _Result(rows)


def _request() -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    request.state.request_id = "req-cs-only"
    return request


@pytest.fixture
def selection_engine(monkeypatch):
    engine = _SelectionEngine()
    for module in (selections, selections_published):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
        monkeypatch.setattr(module, "text", lambda sql: sql)
    return engine


@pytest.mark.parametrize(
    ("route", "kwargs"),
    [
        (selections.selections_latest, {"exchange": None, "min_prob13wk": None, "limit": 10}),
        (selections.selections_history, {"symbol_exchange": None, "symbol": None, "exchange": None, "start": None, "end": None, "min_prob13wk": None, "limit": 10}),
        (selections_published.selections_published_latest, {"exchange": None, "min_prob13wk": 0.55, "min_x4wk1": 0.0, "min_x13wk1": 2.19, "min_x40wk1": 6.45, "limit": 10}),
        (selections_published.selections_published_history, {"symbol_exchange": None, "symbol": None, "exchange": None, "start": None, "end": None, "min_prob13wk": 0.55, "min_x4wk1": 0.0, "min_x13wk1": 2.19, "min_x40wk1": 6.45, "limit": 10}),
    ],
    ids=["base-latest", "base-history", "published-latest", "published-history"],
)
@pytest.mark.parametrize("include_data", [False, True], ids=["no-context", "context"])
@pytest.mark.parametrize("include_mast", [False, True], ids=["no-mast", "mast"])
@pytest.mark.parametrize("cs_only", [True, False], ids=["cs-only", "all-types"])
def test_cs_only_filters_the_selection_universe_for_every_selection_route(
    selection_engine, route, kwargs, include_data, include_mast, cs_only
):
    response = route(
        _request(),
        include_data=include_data,
        include_mast=include_mast,
        cs_only=cs_only,
        **kwargs,
    )

    expected_symbols = ["COMMON"] if cs_only else ["COMMON", "UNIT", "FUND"]
    assert [row["symbol"] for row in response["data"]] == expected_symbols
    assert response["count"] == len(expected_symbols)
    assert response["cs_only"] is cs_only
    sql, params = selection_engine.executed[-1]
    assert params["cs_only"] == int(cs_only)
    assert "EXISTS (" in sql
    assert "FROM st_data cs_filter" in sql
    assert "cs_filter.type = 'CS'" in sql
