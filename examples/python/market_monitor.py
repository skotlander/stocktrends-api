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
    strongest: list[tuple[str, float]]   # (sector_name, avg_rsi)
    weakest: list[tuple[str, float]]
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
    Sector leadership snapshot — which sectors contain the most instruments
    with RSI > 100 (outperforming vs. benchmark) and established bullish trends.

    RSI interpretation: Stock Trends RSI is a relative-strength measure vs. benchmark.
    RSI > 100 = outperformance. RSI < 100 = underperformance.
    This is NOT the oscillator-style RSI from Wilder.
    """
    data = _get("/v1/leadership/summary/latest")
    weekdate: str = data.get("weekdate", "")
    overall: list[dict[str, Any]] = data.get("overall_leaders", [])

    # Aggregate average RSI per sector from leadership constituents
    sector_rsi: dict[str, list[float]] = {}
    for leader in overall:
        sector = leader.get("sector_name", "Unknown")
        rsi = float(leader.get("rsi", 0))
        sector_rsi.setdefault(sector, []).append(rsi)

    ranked = sorted(
        ((sector, sum(rsis) / len(rsis)) for sector, rsis in sector_rsi.items()),
        key=lambda x: x[1],
        reverse=True,
    )

    return SectorLeadershipSummary(
        strongest=ranked[:4],
        weakest=ranked[-4:] if len(ranked) > 4 else [],
        weekdate=weekdate,
    )


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
    print("  (RSI > 100 = outperforming benchmark; RSI < 100 = underperforming)")
    if leadership.strongest:
        print("  Strongest sectors by avg RSI:")
        for sector, avg_rsi in leadership.strongest:
            print(f"    {sector:<32}  RSI {avg_rsi:>6.1f}")
    if leadership.weakest:
        print("  Weakest sectors by avg RSI:")
        for sector, avg_rsi in leadership.weakest:
            print(f"    {sector:<32}  RSI {avg_rsi:>6.1f}")

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
