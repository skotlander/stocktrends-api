"""
Market Monitor
==============
Purpose:
    A market-monitoring agent that retrieves market regime, sector breadth,
    and sector leadership signals from Stock Trends, then synthesizes them
    into a structured market intelligence summary.

What it demonstrates:
    - How to compose multiple Stock Trends intelligence layers into one workflow:
      regime → breadth → leadership → synthesis
    - How to interpret Stock Trends signals correctly:
        - regime_score ranges from -1 (fully bearish) to +1 (fully bullish)
        - RSI > 100 means outperformance vs. benchmark; it is NOT a standard oscillator
        - bullish trend states: ^+ ^- v^
        - bearish trend states: v- v+ ^v
    - How to produce structured agent-ready output rather than raw data dumps

Why a developer would use this:
    - Foundation for a scheduled market-monitoring agent (run weekly after data update)
    - Starting point for regime-aware portfolio or position-sizing systems
    - Template for any workflow that needs to understand current market structure
      before making per-symbol decisions

Environment variables:
    ST_API_BASE_URL   API base URL (default: https://api.stocktrends.com)
    ST_API_KEY        API key for subscription access (optional for free endpoints)

Run:
    python examples/python/market_monitor.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE_URL = os.environ.get("ST_API_BASE_URL", "https://api.stocktrends.com").rstrip("/")
API_KEY = os.environ.get("ST_API_KEY", "")


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
    if resp.status_code == 402:
        # x402 payment required — agent would fund payment and retry
        challenge = resp.json()
        pricing = challenge.get("pricing", {})
        raise SystemExit(
            f"\n[402 Payment Required]\n"
            f"  Endpoint: {path}\n"
            f"  Amount:   {pricing.get('amount_usd', '?')} USD\n"
            f"  Network:  {pricing.get('network', '?')}\n"
            f"\nFund a subscription (ST_API_KEY) or implement x402 payment flow "
            f"(see examples/typescript/x402_client.ts)."
        )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RegimeSummary:
    regime: str
    confidence: str
    regime_score: float
    bullish_pct: float
    bearish_pct: float
    signal_count: int
    weekdate: str


@dataclass
class BreadthClassification:
    improving: list[str] = field(default_factory=list)
    deteriorating: list[str] = field(default_factory=list)
    neutral: list[str] = field(default_factory=list)


@dataclass
class SectorLeadershipSummary:
    # Each entry: (sector_name, constituent_count, avg_constituent_rsi, top_symbols)
    # These are leadership constituents (instruments meeting RSI ≥ 110, mt_cnt ≥ 4),
    # NOT sector-wide RSI averages.
    by_sector: list[tuple[str, int, float, list[str]]]
    weekdate: str


# ---------------------------------------------------------------------------
# Intelligence retrieval
# ---------------------------------------------------------------------------

def fetch_market_regime() -> RegimeSummary:
    """Current market regime based on distribution of trend states across all instruments."""
    data = _get("/v1/market/regime/latest")
    return RegimeSummary(
        regime=data["regime"],
        confidence=data["confidence"],
        regime_score=data["regime_score"],
        bullish_pct=data["bullish_pct"],
        bearish_pct=data["bearish_pct"],
        signal_count=data["signal_count"],
        weekdate=data["weekdate"],
    )


def fetch_sector_breadth() -> tuple[str, BreadthClassification]:
    """Sector breadth snapshot — what fraction of each sector is in a bullish trend state."""
    data = _get("/v1/breadth/sector/latest")
    sectors: list[dict[str, Any]] = data.get("data", [])
    weekdate: str = data.get("weekdate", "")

    improving, deteriorating, neutral = [], [], []
    for s in sectors:
        name = s.get("sector_name") or s.get("sector_code", "Unknown")
        bullish_pct: float = s.get("bullish_pct", 0.0)
        bearish_pct: float = s.get("bearish_pct", 0.0)
        if bullish_pct >= 0.60:
            improving.append(name)
        elif bearish_pct >= 0.55:
            deteriorating.append(name)
        else:
            neutral.append(name)

    return weekdate, BreadthClassification(
        improving=improving,
        deteriorating=deteriorating,
        neutral=neutral,
    )


def fetch_sector_leadership() -> SectorLeadershipSummary:
    """
    Leadership constituent snapshot from /v1/leadership/summary/latest.

    overall_leaders is a filtered list of individual instruments that meet
    the leadership screen (default: RSI >= 110, mt_cnt >= 4).  It is NOT
    a sector-wide RSI aggregate — it is the set of leadership constituents
    currently on the screen.  We group them by sector to show where
    leadership concentration sits, not to compute sector-wide RSI.

    RSI interpretation: Stock Trends RSI is relative strength vs. benchmark.
    RSI > 100 = outperformance. RSI < 100 = underperformance.
    This is NOT the Wilder oscillator.
    """
    data = _get("/v1/leadership/summary/latest")
    weekdate: str = data.get("weekdate", "")
    overall: list[dict[str, Any]] = data.get("overall_leaders", [])

    # Group leadership constituents by sector.
    # The endpoint returns overall_leaders sorted by RSI desc, so the first
    # symbols per sector are the strongest in that sector's constituent list.
    sector_buckets: dict[str, dict[str, Any]] = {}
    for leader in overall:
        sector = leader.get("sector_name") or "Unknown"
        rsi = float(leader.get("rsi", 0))
        symbol = leader.get("symbol", "")
        exchange = leader.get("exchange", "")
        sym_ex = f"{symbol}-{exchange}" if exchange else symbol

        if sector not in sector_buckets:
            sector_buckets[sector] = {"rsis": [], "symbols": []}
        sector_buckets[sector]["rsis"].append(rsi)
        sector_buckets[sector]["symbols"].append(sym_ex)

    # Sort by constituent count desc, break ties by avg constituent RSI desc
    ranked: list[tuple[str, int, float, list[str]]] = []
    for sector, bucket in sector_buckets.items():
        count = len(bucket["rsis"])
        avg_rsi = sum(bucket["rsis"]) / count
        top_syms = bucket["symbols"][:3]
        ranked.append((sector, count, avg_rsi, top_syms))

    ranked.sort(key=lambda x: (x[1], x[2]), reverse=True)

    return SectorLeadershipSummary(by_sector=ranked, weekdate=weekdate)


# ---------------------------------------------------------------------------
# Agent output
# ---------------------------------------------------------------------------

def print_market_intelligence_summary(
    regime: RegimeSummary,
    breadth_weekdate: str,
    breadth: BreadthClassification,
    leadership: SectorLeadershipSummary,
) -> None:
    print()
    print("Stock Trends Market Intelligence Summary")
    print("=" * 56)

    # --- Regime ---
    regime_label = regime.regime.upper()
    print(f"\nMarket Regime:  {regime_label} ({regime.confidence} confidence)")
    print(f"  Regime score: {regime.regime_score:+.3f}  (range: −1 bearish → +1 bullish)")
    print(f"  Bullish:      {regime.bullish_pct:.1%} of {regime.signal_count:,} instruments")
    print(f"  Bearish:      {regime.bearish_pct:.1%}")
    print(f"  Week:         {regime.weekdate}")

    # --- Breadth ---
    print(f"\nSector Breadth  (week: {breadth_weekdate})")
    if breadth.improving:
        print(f"  Improving (≥60% bullish):     {', '.join(breadth.improving)}")
    else:
        print("  Improving:  none meeting threshold")
    if breadth.deteriorating:
        print(f"  Deteriorating (≥55% bearish): {', '.join(breadth.deteriorating)}")
    if breadth.neutral:
        print(f"  Neutral:                      {', '.join(breadth.neutral)}")

    # --- Leadership ---
    print(f"\nSector Leadership  (week: {leadership.weekdate})")
    print("  Source: /v1/leadership/summary/latest — leadership constituents only")
    print("  (Filtered: RSI >= 110 and mt_cnt >= 4; not sector-wide RSI averages)")
    print("  (RSI = relative strength vs benchmark; 100 = baseline, >100 = outperforming)")
    if leadership.by_sector:
        print("  Leadership constituents by sector:")
        for sector, count, avg_rsi, top_syms in leadership.by_sector[:6]:
            examples = ", ".join(top_syms) if top_syms else "—"
            print(
                f"    {sector:<32}  {count:>3} leader(s)"
                f"  avg constituent RSI {avg_rsi:>7.1f}"
                f"  e.g. {examples}"
            )

    print("\n" + "=" * 56)
    print("[Stock Trends market intelligence summary complete]")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    print("Fetching market regime...")
    regime = fetch_market_regime()

    print("Fetching sector breadth...")
    breadth_weekdate, breadth = fetch_sector_breadth()

    print("Fetching sector leadership...")
    leadership = fetch_sector_leadership()

    print_market_intelligence_summary(regime, breadth_weekdate, breadth, leadership)


if __name__ == "__main__":
    try:
        run()
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
