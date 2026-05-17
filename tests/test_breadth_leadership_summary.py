"""
Tests for the st_sector_summary fast-path in breadth and leadership routers.

These tests verify SQL-string correctness only — no live DB required.
They confirm:
  - which table is queried on the optimised vs fallback paths
  - that response-field aliases and filter clauses are correctly emitted
  - that top_k and min_constituents pass-through correctly
"""
import sys
from unittest.mock import MagicMock

# Satisfy module-level imports that need sqlalchemy / db / mysql before import.
# fastapi is expected to be installed in the test environment.
_SQLALCHEMY_MOCK = MagicMock()
_SQLALCHEMY_MOCK.exc.DBAPIError = Exception
for _mod in ("sqlalchemy", "sqlalchemy.orm", "sqlalchemy.exc", "db",
             "mysql", "mysql.connector"):
    sys.modules.setdefault(_mod, _SQLALCHEMY_MOCK)

from routers.breadth import _use_sector_summary, _breadth_summary_sql, _breadth_sql  # noqa: E402
from routers.leadership import _rotation_summary_sql  # noqa: E402


# ---------------------------------------------------------------------------
# _use_sector_summary predicate
# ---------------------------------------------------------------------------

class TestUseSectorSummary:
    def _call(self, **kw):
        defaults = dict(level="sector", cs_only=True, include_unknown=False,
                        min_price=None, min_volume=None)
        defaults.update(kw)
        return _use_sector_summary(**defaults)

    def test_defaults_true(self):
        assert self._call() is True

    def test_industry_group_false(self):
        assert self._call(level="industry_group") is False

    def test_industry_false(self):
        assert self._call(level="industry") is False

    def test_cs_only_false_fallback(self):
        assert self._call(cs_only=False) is False

    def test_include_unknown_true_fallback(self):
        assert self._call(include_unknown=True) is False

    def test_min_price_fallback(self):
        assert self._call(min_price=5.0) is False

    def test_min_volume_fallback(self):
        assert self._call(min_volume=1000) is False


# ---------------------------------------------------------------------------
# _breadth_summary_sql — optimised path uses st_sector_summary
# ---------------------------------------------------------------------------

class TestBreadthSummarySql:
    def test_references_summary_table(self):
        sql, _ = _breadth_summary_sql(start=None, end=None, exchange=None)
        assert "st_sector_summary" in sql

    def test_does_not_reference_st_data(self):
        sql, _ = _breadth_summary_sql(start=None, end=None, exchange=None)
        assert "st_data" not in sql

    def test_cs_filter_hardcoded(self):
        sql, _ = _breadth_summary_sql(start=None, end=None, exchange=None)
        assert "ss.type = 'CS'" in sql

    def test_exchange_param_emitted(self):
        sql, params = _breadth_summary_sql(start=None, end=None, exchange="N")
        assert "ss.exchange = :exchange" in sql
        assert params["exchange"] == "N"

    def test_start_param_emitted(self):
        sql, params = _breadth_summary_sql(start="2024-01-01", end=None, exchange=None)
        assert "ss.weekdate >= :start" in sql
        assert params["start"] == "2024-01-01"

    def test_end_param_emitted(self):
        sql, params = _breadth_summary_sql(start=None, end="2024-12-31", exchange=None)
        assert "ss.weekdate <= :end" in sql
        assert params["end"] == "2024-12-31"

    def test_all_breadth_columns_present(self):
        sql, _ = _breadth_summary_sql(start=None, end=None, exchange=None)
        for col in (
            "ss.total", "ss.bullish_count", "ss.bearish_count", "ss.neutral_count",
            "ss.avg_trend_cnt", "ss.avg_mt_cnt", "ss.avg_rsi",
            "ss.rsi_ge_110_count", "ss.rsi_ge_120_count",
            "ss.young_bullish_count", "ss.mature_bullish_count",
        ):
            assert col in sql, f"Missing column: {col}"

    def test_sector_name_from_summary_table(self):
        sql, _ = _breadth_summary_sql(start=None, end=None, exchange=None)
        assert "ss.sector_name" in sql

    def test_no_taxonomy_join(self):
        sql, _ = _breadth_summary_sql(start=None, end=None, exchange=None)
        assert "st_listsectorsandindustries" not in sql

    def test_no_exchange_param_when_omitted(self):
        _, params = _breadth_summary_sql(start=None, end=None, exchange=None)
        assert "exchange" not in params


# ---------------------------------------------------------------------------
# _breadth_sql — fallback path uses raw st_data
# ---------------------------------------------------------------------------

class TestBreadthFallbackSql:
    def _call(self, level="industry_group"):
        return _breadth_sql(
            level=level,
            weekdate=None,
            start=None,
            end=None,
            exchange=None,
            cs_only=True,
            min_price=None,
            min_volume=None,
            vol_scale=100,
            include_unknown=False,
        )

    def test_references_st_data(self):
        sql, _ = self._call()
        assert "st_data" in sql

    def test_does_not_reference_summary_table(self):
        sql, _ = self._call()
        assert "st_sector_summary" not in sql

    def test_industry_group_level(self):
        sql, _ = self._call(level="industry_group")
        assert "industry_group_code" in sql

    def test_industry_level(self):
        sql, _ = self._call(level="industry")
        assert "industry_code" in sql


# ---------------------------------------------------------------------------
# _rotation_summary_sql — leadership fast-path uses st_sector_summary
# ---------------------------------------------------------------------------

class TestRotationSummarySql:
    def _call(self, **kw):
        defaults = dict(type_="CS", exchange=None, start=None, end=None,
                        min_constituents=25, top_k=5)
        defaults.update(kw)
        return _rotation_summary_sql(**defaults)

    def test_references_summary_table(self):
        sql, _ = self._call()
        assert "st_sector_summary" in sql

    def test_does_not_reference_st_data(self):
        sql, _ = self._call()
        assert "st_data" not in sql

    def test_type_param_emitted(self):
        sql, params = self._call(type_="ETF")
        assert "ss.type = :type" in sql
        assert params["type"] == "ETF"

    def test_exchange_filter(self):
        sql, params = self._call(exchange="Q")
        assert "ss.exchange = :exchange" in sql
        assert params["exchange"] == "Q"

    def test_start_filter(self):
        sql, params = self._call(start="2023-01-01")
        assert "ss.weekdate >= :start" in sql
        assert params["start"] == "2023-01-01"

    def test_end_filter(self):
        sql, params = self._call(end="2023-12-31")
        assert "ss.weekdate <= :end" in sql
        assert params["end"] == "2023-12-31"

    def test_min_constituents_filter(self):
        sql, params = self._call(min_constituents=50)
        assert "ss.total >= :min_constituents" in sql
        assert params["min_constituents"] == 50

    def test_top_k_filter_included(self):
        sql, params = self._call(top_k=5)
        assert "rank_in_week <= :top_k" in sql
        assert params["top_k"] == 5

    def test_top_k_none_omits_filter(self):
        sql, params = self._call(top_k=None)
        assert "top_k" not in params

    def test_response_aliases_preserved(self):
        sql, _ = self._call()
        assert "ss.total AS n" in sql
        assert "ss.bullish_count AS bull_n" in sql
        assert "ss.bull_pct" in sql
        assert "ss.leadership_score" in sql
        assert "ss.bull_avg_rsi" in sql

    def test_rank_in_week_computed(self):
        sql, _ = self._call()
        assert "rank_in_week" in sql

    def test_user_variable_pattern(self):
        sql, _ = self._call()
        # MySQL 5.7 user-variable ranking pattern must be present
        assert "@r :=" in sql
        assert "@wk :=" in sql
        assert "CROSS JOIN" in sql

    def test_sector_name_from_summary_table(self):
        sql, _ = self._call()
        assert "ss.sector_name" in sql

    def test_no_taxonomy_join(self):
        sql, _ = self._call()
        assert "st_listsectorsandindustries" not in sql

    def test_final_order_by_present(self):
        sql, _ = self._call()
        assert "ORDER BY ranked.weekdate ASC" in sql
        assert "ranked.rank_in_week ASC" in sql
