"""
Tests for POST /v1/portfolio/construct.

Validates:
1. Candidate ranking is by decision_score DESC — no alphabetical bias
2. Higher-scoring late-alphabet symbols beat lower-scoring early-alphabet ones
3. Exchange code "T" (TSX) is accepted as valid
4. Invalid exchange returns 400 with invalid_exchange error
5. Omitted exchange defaults to N/Q/A (reported in exchange_filter)
6. tools.json documents allowed values for universe, bias, count, exchange
7. Response discloses candidate selection method and count
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Stub heavy dependencies before importing project modules.
# conftest.py sets these via setdefault; this block is idempotent.
# ---------------------------------------------------------------------------
for _mod in ("sqlalchemy", "sqlalchemy.orm", "sqlalchemy.exc", "db"):
    sys.modules.setdefault(_mod, MagicMock())

from routers.portfolio import router  # noqa: E402

_app = FastAPI()
_app.include_router(router, prefix="/v1")
_client = TestClient(_app, raise_server_exceptions=True)

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_WD = datetime.date(2025, 1, 3)

# Regime aggregation: bullish=80, bearish=20 → regime_score = (80-20)/100 = 0.60
# → current_regime = "bullish" (>= 0.10)
_AGG_ROWS = [
    {"weekdate": _WD, "trend": "^+", "cnt": 80},
    {"weekdate": _WD, "trend": "v-", "cnt": 20},
]
_WEEKDATE_ROWS = [{"weekdate": _WD}]


def _candidate(symbol: str, trend_cnt: int, rsi: int, exchange: str = "Q") -> dict:
    return {
        "symbol": symbol,
        "exchange": exchange,
        "trend": "^+",        # bullish trend → sym_bias = "bullish"
        "trend_cnt": trend_cnt,
        "mt_cnt": trend_cnt,
        "rsi": rsi,
        "rsi_updn": "+",
        "vol_tag": "",
        "weekdate": _WD,
    }


def _mock_engine(
    candidates: list[dict],
    stim_rows: list[dict] | None = None,
) -> MagicMock:
    """Return a mock engine whose connection yields fixed query results in order.

    Query order (matching routers/portfolio.py construct_portfolio):
      1. weekdates  2. regime aggregation  3. candidates  4. ST-IM (st_returnmeans)
    """
    def _result(rows: list[dict]) -> MagicMock:
        r = MagicMock()
        r.mappings.return_value.all.return_value = rows
        return r

    conn = MagicMock()
    conn.execute.side_effect = [
        _result(_WEEKDATE_ROWS),
        _result(_AGG_ROWS),
        _result(candidates),
        _result(stim_rows if stim_rows is not None else []),
    ]

    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    engine.connect.return_value.__exit__.return_value = False
    return engine


def _stim_row(
    symbol: str,
    x13wk: float,
    x13wksd: float,
    exchange: str = "Q",
    weekdate: datetime.date | None = None,
) -> dict:
    """Build a minimal st_returnmeans row for use in stim_rows lists.

    weekdate defaults to _WD (matches _WEEKDATE_ROWS → stim_is_stale = False).
    Pass an earlier date to simulate a stale STIM dataset.
    """
    return {
        "symbol": symbol,
        "exchange": exchange,
        "x13wk": x13wk,
        "x13wksd": x13wksd,
        "weekdate": weekdate if weekdate is not None else _WD,
    }


# ---------------------------------------------------------------------------
# Decision score reference values (regime_score = 0.60, regime = "bullish")
#
# For trend='^+' (bullish) in bullish regime:
#   alignment = "aligned"  →  0.40
#   regime    = min(0.30, 0.60 * 0.5) = 0.30
#   trend_cnt >= 8          →  +0.15   (else 0.00 when trend_cnt == 1)
#   rsi >= 110 (bullish)    →  +0.15   (rsi==101 gives +0.08)
#
# LOW_SCORE  candidate: trend_cnt=1,  rsi=101  → 0.40+0.30+0.00+0.08 = 0.78
# HIGH_SCORE candidate: trend_cnt=8,  rsi=115  → 0.40+0.30+0.15+0.15 = 1.00
# ---------------------------------------------------------------------------

_LOW_TREND_CNT = 1
_LOW_RSI = 101    # 0.08 RSI bonus

_HIGH_TREND_CNT = 8
_HIGH_RSI = 115   # 0.15 RSI bonus


# ---------------------------------------------------------------------------
# 1 & 2. No alphabetical bias — high-scoring late-alphabet symbol wins
# ---------------------------------------------------------------------------

def test_ranking_is_by_decision_score_not_alphabetical():
    """
    21 candidates: 20 early-alphabet (AAMA–AAMT) with low scores,
    1 late-alphabet (ZZZZ) with the highest possible score.

    With the old ORDER BY symbol ASC LIMIT 20, ZZZZ would never appear in the
    candidate pool. With the fix (full universe fetch), ZZZZ ranks first.
    """
    early = [
        _candidate(f"AA{chr(ord('M') + i)}", _LOW_TREND_CNT, _LOW_RSI)
        for i in range(20)   # AAMA … AAMT
    ]
    late = [_candidate("ZZZZ", _HIGH_TREND_CNT, _HIGH_RSI)]
    candidates = early + late   # 21 total; ZZZZ would be excluded by old LIMIT 20

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 5})

    assert resp.status_code == 200, resp.text
    body = resp.json()

    symbols_in_portfolio = [p["symbol"] for p in body["portfolio"]]
    assert "ZZZZ" in symbols_in_portfolio, (
        f"ZZZZ (highest decision_score) not in portfolio: {symbols_in_portfolio}"
    )
    assert body["portfolio"][0]["symbol"] == "ZZZZ", (
        f"ZZZZ should be rank 1; got {body['portfolio'][0]['symbol']}"
    )


def test_ranking_descending_decision_score():
    """Portfolio positions are ordered decision_score DESC, then symbol ASC."""
    candidates = [
        _candidate("MMMM", _HIGH_TREND_CNT, _HIGH_RSI),   # score 1.00
        _candidate("AAAA", _LOW_TREND_CNT, _LOW_RSI),     # score 0.78
        _candidate("BBBB", _LOW_TREND_CNT, _LOW_RSI),     # score 0.78
        _candidate("CCCC", _HIGH_TREND_CNT, _HIGH_RSI),   # score 1.00
    ]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 4})

    assert resp.status_code == 200, resp.text
    portfolio = resp.json()["portfolio"]

    scores = [p["decision_score"] for p in portfolio]
    assert scores == sorted(scores, reverse=True), (
        f"Portfolio not sorted by decision_score DESC: {scores}"
    )
    # Tie-break: CCCC and MMMM both score 1.00; CCCC < MMMM alphabetically → rank 1
    assert portfolio[0]["symbol"] == "CCCC"
    assert portfolio[1]["symbol"] == "MMMM"


def test_candidates_evaluated_equals_full_universe():
    """candidates_evaluated must reflect all candidates passed, not a pre-limited subset."""
    candidates = [_candidate(f"SYM{i:03d}", _LOW_TREND_CNT, _LOW_RSI) for i in range(50)]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 5})

    assert resp.status_code == 200, resp.text
    assert resp.json()["candidates_evaluated"] == 50


def test_candidate_sql_has_no_alphabetical_limit():
    """
    Regression: the candidate query SQL must not contain ORDER BY symbol ASC or
    LIMIT :pool_size, and the params dict must not contain pool_size.

    Patches routers.portfolio.text to capture the actual SQL strings so this
    assertion catches the old alphabetical pre-limit bug even though the mock
    engine returns candidate rows unconditionally.

    With the old code this test fails because:
      - candidate_sql contains "ORDER BY symbol ASC"
      - candidate_sql contains "LIMIT :pool_size"
      - candidate_params contains "pool_size": 20
    """
    candidates = [_candidate("AAPL", _HIGH_TREND_CNT, _HIGH_RSI)]
    engine = _mock_engine(candidates)
    conn = engine.connect.return_value.__enter__.return_value

    with patch("routers.portfolio.text") as mock_text:
        with patch("routers.portfolio.get_engine", return_value=engine):
            resp = _client.post("/v1/portfolio/construct", json={"count": 5})

    assert resp.status_code == 200, resp.text

    # text() is called exactly 4 times: weekdates, regime aggregation, candidates, ST-IM.
    sql_calls = mock_text.call_args_list
    assert len(sql_calls) == 4, f"Expected 4 text() calls, got {len(sql_calls)}"
    candidate_sql: str = sql_calls[2].args[0]

    assert "ORDER BY symbol ASC" not in candidate_sql, (
        "Candidate SQL still contains 'ORDER BY symbol ASC' — alphabetical pre-limit bug "
        f"in query:\n{candidate_sql}"
    )
    assert "LIMIT" not in candidate_sql.upper(), (
        "Candidate SQL still contains 'LIMIT' — candidate pool size pre-filter bug "
        f"in query:\n{candidate_sql}"
    )

    # The params dict for the 3rd conn.execute() call must not contain pool_size.
    execute_calls = conn.execute.call_args_list
    assert len(execute_calls) == 4
    candidate_params: dict = execute_calls[2].args[1]
    assert "pool_size" not in candidate_params, (
        f"pool_size still in candidate params dict: {candidate_params}"
    )

    # Required WHERE filters must still be present.
    assert ":latest_wd" in candidate_sql, "Candidate SQL missing :latest_wd bind"
    assert "type" in candidate_sql, "Candidate SQL missing type filter"
    assert "trend" in candidate_sql.lower(), "Candidate SQL missing trend filter"
    assert "st_data" in candidate_sql, "Candidate SQL missing st_data table reference"


# ---------------------------------------------------------------------------
# 3. TSX exchange code "T" is accepted
# ---------------------------------------------------------------------------

def test_exchange_T_accepted():
    """exchange='T' (TSX) must pass validation and appear in exchange_filter."""
    candidates = [_candidate("SHOP", _HIGH_TREND_CNT, _HIGH_RSI, exchange="T")]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 1, "exchange": "T"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exchange_filter"] == ["T"]


def test_exchange_T_lowercase_normalised():
    """exchange='t' must be upper-cased and accepted."""
    candidates = [_candidate("SHOP", _HIGH_TREND_CNT, _HIGH_RSI, exchange="T")]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 1, "exchange": "t"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["exchange_filter"] == ["T"]


# ---------------------------------------------------------------------------
# 4. Invalid exchange returns 400
# ---------------------------------------------------------------------------

def test_invalid_exchange_returns_400():
    """Unsupported exchange code returns 400 with invalid_exchange error."""
    resp = _client.post("/v1/portfolio/construct", json={"exchange": "X"})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_exchange"
    assert detail["value"] == "X"
    assert isinstance(detail["valid"], list)
    assert "T" in detail["valid"]


# ---------------------------------------------------------------------------
# 5. Omitted exchange defaults to N/Q/A
# ---------------------------------------------------------------------------

def test_omitted_exchange_defaults_to_us_exchanges():
    """exchange omitted → exchange_filter = ['N', 'Q', 'A']."""
    candidates = [_candidate("AAPL", _HIGH_TREND_CNT, _HIGH_RSI, exchange="Q")]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 1})

    assert resp.status_code == 200, resp.text
    assert resp.json()["exchange_filter"] == ["N", "Q", "A"]


# ---------------------------------------------------------------------------
# 6. tools.json documents allowed values for construct_portfolio parameters
# ---------------------------------------------------------------------------

TOOLS_JSON = Path(__file__).resolve().parents[1] / "static" / "tools.json"


@pytest.fixture(scope="module")
def _manifest() -> dict:
    return json.loads(TOOLS_JSON.read_text(encoding="utf-8"))


def _construct_tool(_manifest: dict) -> dict:
    tool = next((t for t in _manifest["tools"] if t["name"] == "construct_portfolio"), None)
    assert tool is not None, "construct_portfolio not found in tools.json"
    return tool


def test_tools_json_construct_portfolio_present(_manifest):
    assert _construct_tool(_manifest) is not None


def test_tools_json_construct_portfolio_has_parameters(_manifest):
    tool = _construct_tool(_manifest)
    assert "parameters" in tool, "construct_portfolio must have a parameters list"
    assert isinstance(tool["parameters"], list)
    assert len(tool["parameters"]) >= 4


def test_tools_json_construct_portfolio_documents_exchange_T(_manifest):
    """exchange parameter must document 'T' as an allowed value."""
    tool = _construct_tool(_manifest)
    exchange_param = next(
        (p for p in tool.get("parameters", []) if p["name"] == "exchange"), None
    )
    assert exchange_param is not None, "exchange parameter missing from tools.json"
    allowed = exchange_param.get("allowed_values", [])
    assert "T" in allowed, f"'T' not in exchange allowed_values: {allowed}"


def test_tools_json_construct_portfolio_documents_bias_values(_manifest):
    """bias parameter must document auto, bullish, bearish."""
    tool = _construct_tool(_manifest)
    bias_param = next(
        (p for p in tool.get("parameters", []) if p["name"] == "bias"), None
    )
    assert bias_param is not None, "bias parameter missing from tools.json"
    allowed = set(bias_param.get("allowed_values", []))
    assert allowed == {"auto", "bullish", "bearish"}, (
        f"bias allowed_values must be {{auto, bullish, bearish}}, got {allowed}"
    )


def test_tools_json_construct_portfolio_documents_universe(_manifest):
    """universe parameter must be documented with allowed_values."""
    tool = _construct_tool(_manifest)
    universe_param = next(
        (p for p in tool.get("parameters", []) if p["name"] == "universe"), None
    )
    assert universe_param is not None, "universe parameter missing from tools.json"
    assert "top" in universe_param.get("allowed_values", [])


def test_tools_json_construct_portfolio_documents_count(_manifest):
    """count parameter must document range 1–10."""
    tool = _construct_tool(_manifest)
    count_param = next(
        (p for p in tool.get("parameters", []) if p["name"] == "count"), None
    )
    assert count_param is not None, "count parameter missing from tools.json"
    assert count_param.get("minimum") == 1
    assert count_param.get("maximum") == 10


def test_tools_json_construct_portfolio_description_mentions_exchanges(_manifest):
    """tools.json description must document the four exchange codes."""
    tool = _construct_tool(_manifest)
    desc = tool.get("description", "")
    for code in ("N", "Q", "A", "T"):
        assert code in desc, f"Exchange code '{code}' not mentioned in construct_portfolio description"


# ---------------------------------------------------------------------------
# 7. Response discloses candidate selection method and ordering
# ---------------------------------------------------------------------------

def test_response_includes_candidate_selection_metadata():
    """Response must include candidate_selection_method and candidate_ordering fields."""
    candidates = [_candidate("AAPL", _HIGH_TREND_CNT, _HIGH_RSI)]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 1})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "candidate_selection_method" in body, "candidate_selection_method missing from response"
    assert "candidate_ordering" in body, "candidate_ordering missing from response"
    assert body["candidate_selection_method"] == "full_eligible_universe"
    assert "decision_score" in body["candidate_ordering"]


def test_response_includes_universe_field():
    """Response must echo back the universe parameter."""
    candidates = [_candidate("AAPL", _HIGH_TREND_CNT, _HIGH_RSI)]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 1})

    assert resp.status_code == 200
    assert resp.json()["universe"] == "top"


def test_construction_notes_mention_candidates_evaluated():
    """construction_notes must contain a note about candidates evaluated."""
    candidates = [_candidate("AAPL", _HIGH_TREND_CNT, _HIGH_RSI)]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 1})

    assert resp.status_code == 200
    notes_combined = " ".join(resp.json()["construction_notes"])
    assert "candidate" in notes_combined.lower() or "evaluated" in notes_combined.lower(), (
        f"construction_notes should mention candidates evaluated: {notes_combined!r}"
    )


# ---------------------------------------------------------------------------
# 8. x402 preview shape includes new transparency fields
# ---------------------------------------------------------------------------

def test_portfolio_construct_preview_includes_transparency_fields():
    """
    discovery/preview.py entry for /v1/portfolio/construct must include the four
    new transparency fields added in this PR: universe, exchange_filter,
    candidate_selection_method, candidate_ordering.
    """
    from discovery.preview import get_endpoint_preview

    preview = get_endpoint_preview("/v1/portfolio/construct")
    assert preview is not None, "/v1/portfolio/construct not registered in preview registry"

    shape_str = " ".join(preview.get("response_shape", []))
    for field in ("universe", "exchange_filter", "candidate_selection_method", "candidate_ordering"):
        assert field in shape_str, (
            f"New transparency field '{field}' missing from /v1/portfolio/construct "
            f"x402 preview response_shape — update discovery/preview.py"
        )


# ---------------------------------------------------------------------------
# 9. ST-IM tiebreaker tests
# ---------------------------------------------------------------------------

def test_stim_breaks_decision_score_ties():
    """
    Two candidates with identical decision_score but different ST-IM
    risk-adjusted 13-week returns.  Higher x13wk/x13wksd ratio must rank first,
    overriding alphabetical order (AAAA < ZZZZ).
    """
    candidates = [
        _candidate("AAAA", _HIGH_TREND_CNT, _HIGH_RSI),   # decision_score 1.00
        _candidate("ZZZZ", _HIGH_TREND_CNT, _HIGH_RSI),   # decision_score 1.00
    ]
    # ZZZZ has a higher risk-adjusted return → should rank above AAAA
    stim = [
        _stim_row("AAAA", x13wk=5.0, x13wksd=2.0),   # ratio = 2.5  (lower)
        _stim_row("ZZZZ", x13wk=9.0, x13wksd=2.0),   # ratio = 4.5  (higher)
    ]
    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates, stim)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 2})

    assert resp.status_code == 200, resp.text
    portfolio = resp.json()["portfolio"]
    assert portfolio[0]["symbol"] == "ZZZZ", (
        f"ZZZZ has higher ST-IM ratio and must rank first despite alphabetical order; "
        f"got {portfolio[0]['symbol']}"
    )
    assert portfolio[1]["symbol"] == "AAAA"


def test_decision_score_dominates_stim():
    """
    A candidate with a higher decision_score must rank above one with a
    better ST-IM ratio.  ST-IM must only break ties within the same
    decision_score tier.
    """
    candidates = [
        _candidate("HIGH", _HIGH_TREND_CNT, _HIGH_RSI),   # decision_score 1.00
        _candidate("LOWW", _LOW_TREND_CNT,  _LOW_RSI),    # decision_score 0.78
    ]
    # LOWW has a far better ST-IM ratio — must still lose to HIGH's score
    stim = [
        _stim_row("HIGH", x13wk=1.0, x13wksd=2.0),   # ratio = 0.5 (low)
        _stim_row("LOWW", x13wk=9.0, x13wksd=1.0),   # ratio = 9.0 (high)
    ]
    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates, stim)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 2})

    assert resp.status_code == 200, resp.text
    portfolio = resp.json()["portfolio"]
    assert portfolio[0]["symbol"] == "HIGH", (
        "HIGH has a higher decision_score and must rank first regardless of ST-IM; "
        f"got {portfolio[0]['symbol']}"
    )
    assert portfolio[0]["decision_score"] > portfolio[1]["decision_score"]


def test_missing_stim_sets_covered_false():
    """
    A candidate with no matching row in st_returnmeans must have
    stim_covered=False and stim_percentile_13wk=None.
    """
    candidates = [_candidate("AAPL", _HIGH_TREND_CNT, _HIGH_RSI)]
    # No stim rows → AAPL is uncovered
    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates, [])):
        resp = _client.post("/v1/portfolio/construct", json={"count": 1})

    assert resp.status_code == 200, resp.text
    pos = resp.json()["portfolio"][0]
    assert pos["stim_covered"] is False, "stim_covered must be False when no ST-IM row exists"
    assert pos["stim_percentile_13wk"] is None, "stim_percentile_13wk must be None when uncovered"
    assert pos["stim_expected_return_13wk"] is None
    assert pos["stim_volatility_13wk"] is None
    assert pos["stim_risk_adjusted_13wk"] is None


def test_zero_stim_coverage_uses_decision_score_only():
    """
    When no candidate has ST-IM coverage, ranking_method must be
    'decision_score_only' and stim_covered_count must be 0.
    """
    candidates = [
        _candidate("BBBB", _HIGH_TREND_CNT, _HIGH_RSI),
        _candidate("AAAA", _LOW_TREND_CNT,  _LOW_RSI),
    ]
    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates, [])):
        resp = _client.post("/v1/portfolio/construct", json={"count": 2})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ranking_method"] == "decision_score_only", (
        f"Expected ranking_method='decision_score_only'; got {body['ranking_method']!r}"
    )
    assert body["stim_covered_count"] == 0
    assert body["stim_weekdate"] is None


def test_construct_issues_four_db_queries():
    """
    /portfolio/construct must issue exactly 4 DB queries:
      1. weekdates  2. regime aggregation  3. candidates  4. ST-IM (st_returnmeans).
    The 4th query must reference the st_returnmeans table.
    """
    candidates = [_candidate("AAPL", _HIGH_TREND_CNT, _HIGH_RSI)]
    engine = _mock_engine(candidates)

    with patch("routers.portfolio.text") as mock_text:
        with patch("routers.portfolio.get_engine", return_value=engine):
            resp = _client.post("/v1/portfolio/construct", json={"count": 1})

    assert resp.status_code == 200, resp.text
    sql_calls = mock_text.call_args_list
    assert len(sql_calls) == 4, (
        f"Expected 4 text() calls (weekdates, regime, candidates, stim); "
        f"got {len(sql_calls)}"
    )
    stim_sql: str = sql_calls[3].args[0]
    assert "st_returnmeans" in stim_sql, (
        f"4th SQL query must reference st_returnmeans; got:\n{stim_sql}"
    )


def test_portfolio_construct_preview_includes_stim_fields():
    """
    discovery/preview.py entry for /v1/portfolio/construct must include the
    four top-level ST-IM transparency fields added by this change.
    """
    from discovery.preview import get_endpoint_preview

    preview = get_endpoint_preview("/v1/portfolio/construct")
    assert preview is not None, "/v1/portfolio/construct not registered in preview registry"

    shape_str = " ".join(preview.get("response_shape", []))
    for field in (
        "stim_weekdate",
        "stim_covered_count",
        "stim_coverage_pct",
        "ranking_method",
    ):
        assert field in shape_str, (
            f"ST-IM field '{field}' missing from /v1/portfolio/construct "
            f"preview response_shape — update discovery/preview.py"
        )
    # Per-position ST-IM fields
    for field in (
        "portfolio[].stim_covered",
        "portfolio[].stim_percentile_13wk",
    ):
        assert field in shape_str, (
            f"Per-position ST-IM field '{field}' missing from preview response_shape"
        )


# ---------------------------------------------------------------------------
# 10. Follow-up tests (Codex review — stale, zero-SD, coverage ordering,
#     single-covered, identical values, SQL exchange scoping)
# ---------------------------------------------------------------------------

_STALE_WD = _WD - datetime.timedelta(days=7)   # one week before latest st_data


def test_stale_stim_weekdate_all_uncovered():
    """
    When stim_rows have weekdate < latest_wd, stim_is_stale=True and every
    candidate must be uncovered.  ranking_method must be 'decision_score_only'.
    """
    candidates = [
        _candidate("AAPL", _HIGH_TREND_CNT, _HIGH_RSI),
        _candidate("MSFT", _LOW_TREND_CNT,  _LOW_RSI),
    ]
    # Stim rows exist but their weekdate is one week behind st_data
    stim = [
        _stim_row("AAPL", x13wk=8.0, x13wksd=2.0, weekdate=_STALE_WD),
        _stim_row("MSFT", x13wk=6.0, x13wksd=2.0, weekdate=_STALE_WD),
    ]
    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates, stim)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 2})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ranking_method"] == "decision_score_only", (
        f"Stale STIM must fall back to decision_score_only; got {body['ranking_method']!r}"
    )
    assert body["stim_covered_count"] == 0
    for pos in body["portfolio"]:
        assert pos["stim_covered"] is False, (
            f"{pos['symbol']}: stim_covered must be False when stim_weekdate is stale"
        )
        assert pos["stim_percentile_13wk"] is None


def test_zero_x13wksd_treated_as_uncovered():
    """
    A st_returnmeans row with x13wksd == 0 must not trigger a ZeroDivisionError
    and must result in stim_covered=False for that candidate.
    """
    candidates = [_candidate("DIVZ", _HIGH_TREND_CNT, _HIGH_RSI)]
    stim = [_stim_row("DIVZ", x13wk=5.0, x13wksd=0.0)]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates, stim)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 1})

    assert resp.status_code == 200, resp.text
    pos = resp.json()["portfolio"][0]
    assert pos["stim_covered"] is False, "x13wksd=0 must produce stim_covered=False"
    assert pos["stim_percentile_13wk"] is None
    assert pos["stim_risk_adjusted_13wk"] is None


def test_covered_ranks_above_uncovered_same_decision_score():
    """
    Within the same decision_score tier, a candidate with ST-IM coverage
    must rank above one without coverage, even if the uncovered symbol
    sorts alphabetically first.
    """
    # Both share score 1.00; AAAA (uncovered) < ZZZZ (covered) alphabetically
    candidates = [
        _candidate("AAAA", _HIGH_TREND_CNT, _HIGH_RSI),   # no stim row → uncovered
        _candidate("ZZZZ", _HIGH_TREND_CNT, _HIGH_RSI),   # has stim row → covered
    ]
    stim = [_stim_row("ZZZZ", x13wk=6.0, x13wksd=2.0)]   # AAAA intentionally absent

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates, stim)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 2})

    assert resp.status_code == 200, resp.text
    portfolio = resp.json()["portfolio"]
    assert portfolio[0]["symbol"] == "ZZZZ", (
        "Covered ZZZZ must rank above uncovered AAAA within the same decision_score tier; "
        f"got {portfolio[0]['symbol']}"
    )
    assert portfolio[0]["stim_covered"] is True
    assert portfolio[1]["stim_covered"] is False


def test_single_covered_candidate_gets_midpoint_percentile():
    """
    When only one candidate is covered, w_min == w_max so the
    percentile is clamped to 0.5 (midpoint).
    """
    candidates = [_candidate("ONLY", _HIGH_TREND_CNT, _HIGH_RSI)]
    stim = [_stim_row("ONLY", x13wk=7.0, x13wksd=2.0)]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates, stim)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 1})

    assert resp.status_code == 200, resp.text
    pos = resp.json()["portfolio"][0]
    assert pos["stim_covered"] is True
    assert pos["stim_percentile_13wk"] == 0.5, (
        f"Single covered candidate must get percentile=0.5; got {pos['stim_percentile_13wk']}"
    )


def test_identical_stim_values_all_get_midpoint_percentile():
    """
    When all covered candidates share the same risk-adjusted ratio,
    w_min == w_max and every candidate receives percentile=0.5.
    Ordering within the tier then falls back to symbol ASC.
    """
    candidates = [
        _candidate("CCCC", _HIGH_TREND_CNT, _HIGH_RSI),
        _candidate("AAAA", _HIGH_TREND_CNT, _HIGH_RSI),
        _candidate("BBBB", _HIGH_TREND_CNT, _HIGH_RSI),
    ]
    stim = [
        _stim_row("AAAA", x13wk=4.0, x13wksd=2.0),   # ratio = 2.0
        _stim_row("BBBB", x13wk=4.0, x13wksd=2.0),   # ratio = 2.0
        _stim_row("CCCC", x13wk=4.0, x13wksd=2.0),   # ratio = 2.0
    ]

    with patch("routers.portfolio.get_engine", return_value=_mock_engine(candidates, stim)):
        resp = _client.post("/v1/portfolio/construct", json={"count": 3})

    assert resp.status_code == 200, resp.text
    portfolio = resp.json()["portfolio"]
    for pos in portfolio:
        assert pos["stim_percentile_13wk"] == 0.5, (
            f"{pos['symbol']}: expected percentile=0.5 when all ratios identical; "
            f"got {pos['stim_percentile_13wk']}"
        )
    # Symbol is the final deterministic tiebreaker → AAAA, BBBB, CCCC
    assert [p["symbol"] for p in portfolio] == ["AAAA", "BBBB", "CCCC"]


def test_stim_subquery_scoped_to_exchange_universe():
    """
    The MAX(weekdate) subquery inside the ST-IM query must be scoped to the
    same exchange universe as the outer WHERE clause.

    For the default (no exchange filter), the subquery must contain
    IN ('N', 'Q', 'A') so that a newer weekdate from a non-US exchange
    cannot cause the US-scoped outer query to return zero rows.
    """
    candidates = [_candidate("AAPL", _HIGH_TREND_CNT, _HIGH_RSI)]
    engine = _mock_engine(candidates)

    with patch("routers.portfolio.text") as mock_text:
        with patch("routers.portfolio.get_engine", return_value=engine):
            resp = _client.post("/v1/portfolio/construct", json={"count": 1})

    assert resp.status_code == 200, resp.text
    stim_sql: str = mock_text.call_args_list[3].args[0]

    # The subquery must restrict the MAX to the same exchange scope
    assert "SELECT MAX(weekdate) FROM st_returnmeans WHERE exchange IN" in stim_sql, (
        "MAX(weekdate) subquery must be scoped to exchange IN ('N','Q','A') "
        f"for default exchange filter; got:\n{stim_sql}"
    )
