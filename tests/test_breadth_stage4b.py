"""Focused Stage 4B API semantics tests; all are pure router/SQL tests."""

import sys
import json
from inspect import signature
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

_SQLALCHEMY_MOCK = MagicMock()
_SQLALCHEMY_MOCK.exc.DBAPIError = Exception
for _mod in ("sqlalchemy", "sqlalchemy.orm", "sqlalchemy.exc", "db", "mysql", "mysql.connector"):
    sys.modules.setdefault(_mod, _SQLALCHEMY_MOCK)

from routers.breadth import (  # noqa: E402
    _breadth_sql,
    _breadth_summary_sql,
    _postprocess_coverage,
    _postprocess,
    _resolve_population,
    _use_sector_summary,
)
from routers.leadership import _latest_weekdate, _rotation_raw_sql, _rotation_summary_sql, leadership_rotation_history, leadership_summary_latest  # noqa: E402
import routers.leadership as leadership_router  # noqa: E402
from discovery.endpoint_metadata import get_endpoint_metadata  # noqa: E402


@pytest.mark.parametrize(
    ("population", "cs_only", "expected"),
    [
        (None, None, "equities"),
        ("equities", None, "equities"),
        ("cs", None, "cs"),
        ("all", None, "all"),
        (None, True, "cs"),
        (None, False, "all"),
    ],
)
def test_population_contract(population, cs_only, expected):
    assert _resolve_population(population, cs_only) == expected


@pytest.mark.parametrize("population,cs_only", [("equities", True), ("equities", False), ("cs", False), ("all", True)])
def test_population_conflicts_are_rejected_before_execution(population, cs_only):
    with pytest.raises(HTTPException) as exc_info:
        _resolve_population(population, cs_only)
    assert exc_info.value.status_code == 400


def test_breadth_math_uses_classified_and_observed_denominators():
    row = _postprocess([{
        "observed_count": 10, "bullish_count": 3, "bearish_count": 1,
        "neutral_count": 2, "rsi_ge_110_count": 0, "rsi_ge_120_count": 0,
        "young_bullish_count": 0, "mature_bullish_count": 0,
    }])[0]
    assert row["total"] == row["classified_count"] == 4
    assert row["observed_count"] == 10
    assert row["bullish_pct"] == 3 / 4
    assert row["bearish_pct"] == 1 / 4
    assert row["neutral_pct"] == 2 / 10
    assert row["net_breadth"] == 2
    assert row["unclassified_count"] == 4


def test_zero_classified_denominator_is_explicit_zero_and_coverage_is_nullable():
    row = _postprocess([{
        "observed_count": 2, "bullish_count": 0, "bearish_count": 0,
        "neutral_count": 2, "classified_population_count": 0,
        "mapped_classified_count": 0,
    }])[0]
    assert row["total"] == 0
    assert row["bullish_pct"] == row["bearish_pct"] == 0.0
    assert row["neutral_pct"] == 1.0
    assert row["mapped_coverage_ratio"] is None


def test_all_unmapped_request_coverage_remains_visible_without_a_sector_row():
    coverage = _postprocess_coverage([{
        "weekdate": "2026-01-02", "classified_population_count": 7,
        "mapped_classified_count": 0, "unmapped_classified_count": 7,
    }])
    assert coverage == [{
        "weekdate": "2026-01-02", "classified_population_count": 7,
        "mapped_classified_count": 0, "unmapped_classified_count": 7,
        "mapped_coverage_ratio": 0.0,
    }]


def test_missing_coverage_row_does_not_invent_a_denominator():
    assert _postprocess_coverage([]) == []


def test_summary_fast_path_is_only_exact_canonical_sector_semantics():
    exact = dict(level="sector", population="equities", include_unknown=False,
                 min_price=None, min_volume=None, exchange=None)
    assert _use_sector_summary(**exact) is True
    assert _use_sector_summary(**{**exact, "population": "cs", "exchange": "N"}) is True
    assert _use_sector_summary(**{**exact, "population": "all"}) is False
    assert _use_sector_summary(**{**exact, "exchange": "B"}) is False
    assert _use_sector_summary(**{**exact, "level": "industry"}) is False
    assert _use_sector_summary(**{**exact, "min_price": 5}) is False
    assert _use_sector_summary(**{**exact, "include_unknown": True}) is False


def test_summary_uses_direct_star_and_coverage_rows():
    sql, params = _breadth_summary_sql(start=None, end=None, exchange=None, population="equities")
    assert "st_sector_summary_shadow" in sql
    assert "st_sector_summary_coverage_shadow" in sql
    assert params == {"type": "EQ", "exchange": "*"}
    assert "classified_population_count" in sql
    assert "unmapped_classified_count" in sql


def test_raw_all_population_does_not_claim_eq_semantics():
    sql, _ = _breadth_sql(level="sector", weekdate=None, start=None, end=None,
                           exchange=None, population="all", min_price=None,
                           min_volume=None, vol_scale=100, include_unknown=False)
    assert "d.type IN ('CS','UN')" not in sql
    assert "st_sector_summary_shadow" not in sql


def test_direct_raw_helper_default_is_equities_not_all():
    sql, _ = _breadth_sql(level="sector", weekdate=None, start=None, end=None,
                           exchange=None, min_price=None, min_volume=None,
                           vol_scale=100, include_unknown=False)
    assert "d.type IN ('CS','UN')" in sql


def test_canonical_raw_fallback_excludes_bats_and_i():
    sql, _ = _breadth_sql(level="industry", weekdate=None, start=None, end=None,
                           exchange=None, population="equities", min_price=None,
                           min_volume=None, vol_scale=100, include_unknown=False)
    assert "d.exchange IN ('A','N','Q','T')" in sql


def test_raw_fallback_keeps_10000_rsi_and_excludes_only_higher_values_from_rsi_metrics():
    sql, _ = _breadth_sql(level="industry", weekdate=None, start=None, end=None,
                           exchange=None, population="equities", min_price=None,
                           min_volume=None, vol_scale=100, include_unknown=False)
    assert "d.rsi <= 10000" in sql
    assert "d.rsi >= 110 THEN 1 ELSE 0" in sql


def test_rotation_defaults_to_eq_and_preserves_nullable_shadow_metrics():
    assert signature(leadership_rotation_history).parameters["type"].default.default == "EQ"
    sql, params = _rotation_summary_sql(type_="EQ", exchange=None, start=None, end=None,
                                        min_constituents=25, top_k=5)
    assert "st_sector_summary_shadow" in sql
    assert params["exchange"] == "*"
    assert "ss.bull_pct" in sql
    assert "ss.leadership_score" in sql


def test_rotation_explicit_cs_uses_shadow_and_legacy_type_uses_raw():
    cs_sql, cs_params = _rotation_summary_sql(type_="CS", exchange="N", start=None, end=None,
                                              min_constituents=25, top_k=5)
    raw_sql, raw_params = _rotation_raw_sql(type_="ETF", exchange=None, start=None, end=None,
                                            min_constituents=25, top_k=5)
    assert "st_sector_summary_shadow" in cs_sql and cs_params["type"] == "CS"
    assert "FROM st_data d" in raw_sql and raw_params["type"] == "ETF"


def test_rotation_raw_scope_translates_eq_case_and_star_without_literal_values():
    eq_sql, eq_params = _rotation_raw_sql(type_="eq", exchange="B", start=None, end=None,
                                          min_constituents=25, top_k=5)
    cs_sql, cs_params = _rotation_raw_sql(type_="cs", exchange="B", start=None, end=None,
                                          min_constituents=25, top_k=5)
    legacy_sql, legacy_params = _rotation_raw_sql(type_="etf", exchange="*", start=None, end=None,
                                                  min_constituents=25, top_k=5)
    assert "d.type IN ('CS','UN')" in eq_sql and "'type'" not in repr(eq_params)
    assert "d.exchange = :exchange" in eq_sql and eq_params["exchange"] == "B"
    assert "d.type = :type" in cs_sql and cs_params["type"] == "CS"
    assert "d.exchange IN ('A','N','Q','T')" in legacy_sql
    assert legacy_params["type"] == "ETF" and "exchange" not in legacy_params


def test_rotation_raw_anchor_uses_the_same_eq_and_star_translation(monkeypatch):
    class Result:
        def mappings(self):
            return self

        def first(self):
            return {"wd": "2026-08-21"}

    class Connection:
        def __init__(self):
            self.executed = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, statement, params):
            self.executed.append((str(statement), params))
            return Result()

    class Engine:
        def __init__(self):
            self.connection = Connection()

        def connect(self):
            return self.connection

    monkeypatch.setattr(leadership_router, "text", lambda sql: sql)
    engine = Engine()
    assert _latest_weekdate(engine, "*", "eq") == "2026-08-21"
    sql, params = engine.connection.executed[0]
    assert "d.type IN ('CS','UN')" in sql
    assert "d.exchange IN ('A','N','Q','T')" in sql
    assert "type" not in params and "exchange" not in params


def test_leadership_summary_latest_remains_cs_by_default():
    assert signature(leadership_summary_latest).parameters["type"].default.default == "CS"


def test_discovery_and_static_breadth_contracts_declare_population_and_coverage():
    for path in ("/v1/breadth/sector/latest", "/v1/breadth/sector/history"):
        metadata = get_endpoint_metadata(path, "GET")
        assert metadata["optional_inputs"]["population"]["safe_default"] == "equities"
        assert "Legacy alias" in metadata["optional_inputs"]["cs_only"]["description"]
        assert metadata["optional_inputs"]["cs_only"]["safe_default"] is None
        assert "mapped_coverage_ratio" in " ".join(metadata["response_shape"])

    manifest = json.loads((Path(__file__).parents[1] / "static" / "tools.json").read_text())
    tools = {tool["name"]: tool for tool in manifest["tools"]}
    for name in ("breadth_sector_latest", "breadth_sector_history"):
        params = {item["name"]: item for item in tools[name]["parameters"]}
        assert params["population"]["default"] == "equities"
        assert params["population"]["allowed_values"] == ["equities", "cs", "all"]
        assert "cs_only" in params

    rotation = get_endpoint_metadata("/v1/leadership/rotation/history", "GET")
    assert rotation["optional_inputs"]["type"]["safe_default"] == "EQ"
    assert rotation["safe_example_request"]["query"]["type"] == "EQ"
