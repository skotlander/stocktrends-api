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


def _mock_engine(candidates: list[dict]) -> MagicMock:
    """Return a mock engine whose connection yields fixed query results in order."""
    def _result(rows: list[dict]) -> MagicMock:
        r = MagicMock()
        r.mappings.return_value.all.return_value = rows
        return r

    conn = MagicMock()
    conn.execute.side_effect = [
        _result(_WEEKDATE_ROWS),
        _result(_AGG_ROWS),
        _result(candidates),
    ]

    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    engine.connect.return_value.__exit__.return_value = False
    return engine


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

    # text() is called exactly 3 times: weekdates, regime aggregation, candidates.
    sql_calls = mock_text.call_args_list
    assert len(sql_calls) == 3, f"Expected 3 text() calls, got {len(sql_calls)}"
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
    assert len(execute_calls) == 3
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
