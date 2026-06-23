"""
Portfolio Ranker
================
Purpose:
    Rank a list of symbols by Stock Trends intelligence: regime alignment,
    trend state quality, relative strength, and ST-IM forward return expectations.

What it demonstrates:
    - How to evaluate individual symbols against the live market regime using
      /v1/decision/evaluate-symbol — a composite score that reflects both the
      symbol's trend context and its alignment with the current market regime
    - How to retrieve ST-IM (Stock Trends Inference Model) forward return
      distributions for each symbol across 4-week, 13-week, and 40-week horizons
    - How to rank a portfolio or candidate list by a structured intelligence signal
      rather than by raw price change

Why a developer would use this:
    - Building a regime-aware portfolio screener or watchlist prioritizer
    - Incorporating ST-IM probabilistic forward returns into portfolio construction
    - Creating an autonomous ranking agent that re-ranks holdings weekly after
      the latest Stock Trends data update

ST-IM field interpretation:
    x13wk  = expected 13-week forward return (mean of distribution)
    x13wksd = standard deviation of 13-week forward return distribution
    x13wk1 = lower confidence bound of 13-week expected return
    x13wk2 = upper confidence bound of 13-week expected return

    ST-IM outputs are conditional historical tendencies, not guarantees.
    They are useful for ranking and portfolio construction, not for precise
    price targets or individual-stock certainty.

Environment variables:
    ST_API_BASE_URL   API base URL (default: https://api.stocktrends.com)
    ST_API_KEY        API key for subscription access

Run:
    python examples/python/portfolio_ranker.py

    Or pass symbols explicitly:
    python examples/python/portfolio_ranker.py NVDA-Q MSFT-Q AAPL-Q TSLA-Q AMZN-Q
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = os.environ.get("ST_API_BASE_URL", "https://api.stocktrends.com").rstrip("/")
API_KEY = os.environ.get("ST_API_KEY", "")

# Default portfolio for demonstration; override via command-line arguments
DEFAULT_SYMBOLS = ["NVDA-Q", "MSFT-Q", "AAPL-Q", "TSLA-Q", "AMZN-Q", "META-Q", "GOOGL-Q"]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    h = {"Accept": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_headers(), params=params or {})
    _handle_402(resp, path)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, headers=_headers(), json=body)
    _handle_402(resp, path)
    resp.raise_for_status()
    return resp.json()


def _handle_402(resp: httpx.Response, path: str) -> None:
    if resp.status_code == 402:
        challenge = resp.json()
        pricing = challenge.get("pricing", {})
        raise SystemExit(
            f"\n[402 Payment Required]\n"
            f"  Endpoint: {path}\n"
            f"  Amount:   {pricing.get('amount_usd', '?')} USD\n"
            f"\nSet ST_API_KEY for subscription access, or implement x402 payment flow "
            f"(see examples/typescript/x402_client.ts)."
        )


def _parse_symbol_exchange(sym: str) -> tuple[str, str]:
    if "-" not in sym:
        raise ValueError(
            f"Expected format SYMBOL-EXCHANGE (e.g. NVDA-Q), got: {sym}"
        )
    parts = sym.rsplit("-", 1)
    return parts[0].strip().upper(), parts[1].strip().upper()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SymbolRanking:
    symbol_exchange: str
    symbol: str
    exchange: str
    rank: int

    # Decision evaluation
    bias: str
    confidence: str
    decision_score: float
    alignment: str

    # Symbol context
    trend: str
    trend_cnt: int
    mt_cnt: int
    rsi: int

    # ST-IM forward return distributions
    stim_x13wk: float | None        # expected 13-week return (mean)
    stim_x13wksd: float | None      # standard deviation
    stim_x13wk1: float | None       # lower confidence bound
    stim_x13wk2: float | None       # upper confidence bound
    stim_x4wk: float | None         # expected 4-week return
    stim_x40wk: float | None        # expected 40-week return
    stim_is_stale: bool

    # Regime context (same for all symbols in one run)
    regime: str
    regime_score: float
    weekdate: str

    # Signal notes from decision service
    signal_notes: list[str]


# ---------------------------------------------------------------------------
# Intelligence retrieval
# ---------------------------------------------------------------------------

def evaluate_symbol(symbol: str, exchange: str) -> dict[str, Any]:
    """
    Symbol-level decision evaluation: combines the symbol's trend context
    with the live market regime to produce bias, confidence, and decision_score.
    decision_score ranges from 0 (weak/divergent) to 1 (strong/aligned).

    The endpoint accepts symbol_exchange (combined) or symbol + exchange (separate).
    Both forms are equivalent; symbol_exchange is preferred for agent use.
    """
    return _post("/v1/decision/evaluate-symbol", {"symbol_exchange": f"{symbol}-{exchange}"})


def fetch_stim(symbol: str, exchange: str) -> dict[str, Any] | None:
    """
    ST-IM (Stock Trends Inference Model) forward return distributions.
    Returns expected returns and standard deviations for 4wk, 13wk, and 40wk horizons.
    Returns None if no ST-IM data is available for this instrument.

    The endpoint accepts symbol_exchange (combined) or symbol + exchange (separate).
    Both forms are equivalent; symbol_exchange is preferred for agent use.
    """
    try:
        return _get("/v1/stim/latest", {"symbol_exchange": f"{symbol}-{exchange}"})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def rank_portfolio(symbols: list[str]) -> list[SymbolRanking]:
    """
    Evaluate and rank a list of symbol-exchange strings.
    Primary ranking: decision_score (descending).
    Secondary: ST-IM 13-week expected return when scores are close.
    """
    results: list[SymbolRanking] = []

    for sym in symbols:
        symbol, exchange = _parse_symbol_exchange(sym)
        symbol_exchange = f"{symbol}-{exchange}"

        print(f"  Evaluating {symbol_exchange}...")

        try:
            decision = evaluate_symbol(symbol, exchange)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                print(f"    [skip] {symbol_exchange} — not found in latest week")
                continue
            raise

        stim = fetch_stim(symbol, exchange)

        ctx = decision.get("symbol_context", {})
        regime_ctx = decision.get("regime_context", {})

        stim_x13wk = float(stim["x13wk"]) if stim and stim.get("x13wk") is not None else None
        stim_x13wksd = float(stim["x13wksd"]) if stim and stim.get("x13wksd") is not None else None
        stim_x13wk1 = float(stim["x13wk1"]) if stim and stim.get("x13wk1") is not None else None
        stim_x13wk2 = float(stim["x13wk2"]) if stim and stim.get("x13wk2") is not None else None
        stim_x4wk = float(stim["x4wk"]) if stim and stim.get("x4wk") is not None else None
        stim_x40wk = float(stim["x40wk"]) if stim and stim.get("x40wk") is not None else None

        results.append(SymbolRanking(
            symbol_exchange=symbol_exchange,
            symbol=symbol,
            exchange=exchange,
            rank=0,  # assigned after sort
            bias=decision.get("bias", "unknown"),
            confidence=decision.get("confidence", "unknown"),
            decision_score=float(decision.get("decision_score", 0.0)),
            alignment=decision.get("alignment", "unknown"),
            trend=ctx.get("trend", ""),
            trend_cnt=int(ctx.get("trend_cnt", 0)),
            mt_cnt=int(ctx.get("mt_cnt", 0)),
            rsi=int(ctx.get("rsi", 0)),
            stim_x13wk=stim_x13wk,
            stim_x13wksd=stim_x13wksd,
            stim_x13wk1=stim_x13wk1,
            stim_x13wk2=stim_x13wk2,
            stim_x4wk=stim_x4wk,
            stim_x40wk=stim_x40wk,
            stim_is_stale=bool(stim and stim.get("is_stale")),
            regime=regime_ctx.get("current_regime", ""),
            regime_score=float(regime_ctx.get("regime_score", 0.0)),
            weekdate=decision.get("weekdate", ""),
            signal_notes=decision.get("signal_notes", []),
        ))

    # Sort: decision_score descending; break ties by ST-IM 13wk expected return
    results.sort(
        key=lambda r: (r.decision_score, r.stim_x13wk or 0.0),
        reverse=True,
    )
    for i, r in enumerate(results):
        r.rank = i + 1

    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _stim_line(r: SymbolRanking) -> str:
    if r.stim_x13wk is None:
        return "  ST-IM: n/a"
    stale = " [stale]" if r.stim_is_stale else ""
    bounds = ""
    if r.stim_x13wk1 is not None and r.stim_x13wk2 is not None:
        bounds = f"  CI [{r.stim_x13wk1:+.1f}% → {r.stim_x13wk2:+.1f}%]"
    return (
        f"  ST-IM 13wk: {r.stim_x13wk:+.1f}% ± {r.stim_x13wksd:.1f}%{bounds}"
        f"  |  4wk: {r.stim_x4wk:+.1f}%  40wk: {r.stim_x40wk:+.1f}%{stale}"
    )


def print_rankings(rankings: list[SymbolRanking]) -> None:
    if not rankings:
        print("No symbols could be evaluated.")
        return

    regime = rankings[0].regime.upper() if rankings else ""
    regime_score = rankings[0].regime_score if rankings else 0.0
    weekdate = rankings[0].weekdate if rankings else ""

    print()
    print("Stock Trends Portfolio Ranking")
    print("=" * 60)
    print(f"  Market Regime:  {regime} (score {regime_score:+.3f})")
    print(f"  Week:           {weekdate}")
    print(f"  Ranked by:      decision_score (composite trend/regime context score)")
    print()

    if regime in {"MIXED", "NEUTRAL"} or rankings[0].confidence == "low":
        print(
            "  Note: In a mixed or low-confidence regime, directional regime alignment\n"
            "  is weak or neutralized. decision_score reflects composite signal strength\n"
            "  — trend maturity, RSI level, and internal trend consistency — not a\n"
            "  bullish/bearish preference. Bullish and bearish symbols may both rank\n"
            "  highly if their trend context is mature and internally consistent.\n"
            "  Rankings here represent signal context, not buy/sell recommendations."
        )
        print()

    for r in rankings:
        trend_info = f"{r.trend}  cnt={r.trend_cnt}  mt={r.mt_cnt}  RSI={r.rsi}"
        print(f"#{r.rank}  {r.symbol_exchange:<12}  score={r.decision_score:.3f}  {r.bias.upper()} / {r.alignment}")
        print(f"      Trend:  {trend_info}")
        print(_stim_line(r))
        if r.signal_notes:
            for note in r.signal_notes:
                print(f"      Note:   {note}")
        print()

    print("─" * 60)
    print("ST-IM probabilities are conditional historical tendencies,")
    print("not guarantees, price targets, or buy/sell commands.")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(symbols: list[str]) -> None:
    print(f"Ranking {len(symbols)} symbols against Stock Trends intelligence...\n")
    rankings = rank_portfolio(symbols)
    print_rankings(rankings)


if __name__ == "__main__":
    symbols = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_SYMBOLS
    try:
        run(symbols)
    except ValueError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPStatusError as exc:
        print(f"HTTP error {exc.response.status_code}: {exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except httpx.ConnectError:
        print(
            f"Could not connect to {BASE_URL}\n"
            "Check ST_API_BASE_URL and network connectivity.",
            file=sys.stderr,
        )
        sys.exit(1)
