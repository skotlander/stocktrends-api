"""
Research Workflow
=================
Purpose:
    Demonstrate autonomous consumption of published Stock Trends intelligence
    artifacts: discovery, market guidance, and market research reports.

What it demonstrates:
    - How to discover what intelligence artifacts are currently published
      using the free /v1/intelligence/discovery endpoint
    - How to retrieve the latest market guidance artifact (paid, 0.25 STC)
      and market research report (paid, 0.50 STC)
    - How to interpret the PublicArtifactEnvelope structure:
        artifact_type     type of intelligence artifact
        publication_status  published | product_grade | publish_ready
        weekdate          the market week this artifact covers
        provider          inference provider metadata
        payload           the actual intelligence content
        content_hash      deterministic content fingerprint (sha256:...)

    This workflow demonstrates the intelligence layer of Stock Trends —
    published research artifacts produced by the Stock Trends Intelligence
    Agent, not raw data retrieval.

Why a developer would use this:
    - Building an autonomous research pipeline that polls for new artifacts
      and processes them without human intervention
    - Grounding an LLM-powered research assistant in current Stock Trends
      intelligence artifacts rather than raw market data
    - Discovering what intelligence is available before deciding which paid
      artifacts to retrieve

Intelligence artifact types:
    discovery_metadata     Catalog of available artifacts (free)
    editorial_preview      Preview of editorial content (free)
    market_guidance        Published market guidance (0.25 STC)
    market_research_report Published research report (0.50 STC)

Environment variables:
    ST_API_BASE_URL   API base URL (default: https://api.stocktrends.com)
    ST_API_KEY        API key for subscription access (required for paid artifacts)

Run:
    python examples/python/research_workflow.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
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


def _get(path: str) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_headers())
    if resp.status_code == 402:
        challenge = resp.json()
        pricing = challenge.get("pricing", {})
        raise SystemExit(
            f"\n[402 Payment Required]\n"
            f"  Endpoint: {path}\n"
            f"  Amount:   {pricing.get('amount_usd', '?')} USD  ({pricing.get('unit', 'request')})\n"
            f"\nSet ST_API_KEY for subscription access, or implement x402 payment flow "
            f"(see examples/typescript/x402_client.ts)."
        )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ArtifactSummary:
    artifact_id: str
    artifact_type: str
    publication_status: str
    weekdate: str
    published_at: str
    provider_name: str
    provider_version: str | None
    payload_keys: list[str]
    content_hash: str
    revision: int
    warnings_count: int


def _summarize_artifact(envelope: dict[str, Any]) -> ArtifactSummary:
    provider = envelope.get("provider", {})
    payload = envelope.get("payload") or {}
    payload_keys = list(payload.keys()) if isinstance(payload, dict) else []
    warnings = envelope.get("warnings") or []
    return ArtifactSummary(
        artifact_id=envelope.get("artifact_id", ""),
        artifact_type=envelope.get("artifact_type", ""),
        publication_status=envelope.get("publication_status", ""),
        weekdate=envelope.get("weekdate", ""),
        published_at=envelope.get("published_at", ""),
        provider_name=provider.get("name", provider.get("id", "unknown")),
        provider_version=provider.get("version"),
        payload_keys=payload_keys,
        content_hash=envelope.get("content_hash", ""),
        revision=int(envelope.get("revision", 1)),
        warnings_count=len(warnings),
    )


# ---------------------------------------------------------------------------
# Workflow steps
# ---------------------------------------------------------------------------

def step_discover() -> dict[str, Any]:
    """
    Step 1 — Discover available intelligence artifacts (free endpoint).

    Returns the discovery_metadata envelope which catalogs what artifacts
    the Intelligence Agent has published and their availability status.
    Agents use this to decide whether to retrieve paid artifacts.
    """
    print("Step 1: Discovering available intelligence artifacts...")
    return _get("/v1/intelligence/discovery")


def step_fetch_guidance() -> dict[str, Any]:
    """
    Step 2 — Retrieve latest market guidance artifact (0.25 STC).

    Market guidance contains the Intelligence Agent's current market
    outlook, regime assessment, and actionable guidance structured for
    systematic consumption.
    """
    print("Step 2: Retrieving latest market guidance artifact (0.25 STC)...")
    return _get("/v1/intelligence/guidance/latest")


def step_fetch_research() -> dict[str, Any]:
    """
    Step 3 — Retrieve latest market research report artifact (0.50 STC).

    Market research reports contain in-depth published Stock Trends
    analysis: sector analysis, breadth interpretation, leadership review,
    and ST-IM signal interpretation across the full market.
    """
    print("Step 3: Retrieving latest market research report artifact (0.50 STC)...")
    return _get("/v1/intelligence/research/latest")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_artifact_summary(label: str, summary: ArtifactSummary) -> None:
    print(f"\n  {label}")
    print(f"  {'─' * 48}")
    print(f"  Artifact ID:     {summary.artifact_id}")
    print(f"  Type:            {summary.artifact_type}")
    print(f"  Status:          {summary.publication_status}")
    print(f"  Week:            {summary.weekdate}")
    print(f"  Published:       {summary.published_at}")
    print(f"  Revision:        {summary.revision}")
    print(f"  Provider:        {summary.provider_name}"
          + (f" v{summary.provider_version}" if summary.provider_version else ""))
    print(f"  Payload keys:    {', '.join(summary.payload_keys) if summary.payload_keys else '(none)'}")
    print(f"  Content hash:    {summary.content_hash[:32]}...")
    if summary.warnings_count:
        print(f"  Warnings:        {summary.warnings_count}")


def _extract_discovery_catalog(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract artifact catalog entries from a discovery_metadata envelope."""
    payload = envelope.get("payload") or {}
    return payload.get("artifacts", payload.get("catalog", []))


def print_research_workflow_summary(
    discovery: dict[str, Any],
    guidance: dict[str, Any] | None,
    research: dict[str, Any] | None,
) -> None:
    print()
    print("Stock Trends Research Workflow")
    print("=" * 56)

    # Discovery catalog
    catalog = _extract_discovery_catalog(discovery)
    disc_summary = _summarize_artifact(discovery)
    print(f"\n  Discovery Metadata  (week: {disc_summary.weekdate})")
    print(f"  Published artifacts in catalog: {len(catalog)}")
    if catalog:
        for entry in catalog:
            a_type = entry.get("artifact_type", entry.get("type", ""))
            a_id = entry.get("artifact_id", entry.get("id", ""))
            a_status = entry.get("publication_status", entry.get("status", ""))
            print(f"    • {a_type:<30}  {a_id:<36}  [{a_status}]")

    # Guidance artifact
    if guidance:
        g = _summarize_artifact(guidance)
        _print_artifact_summary("Market Guidance Artifact", g)
        # Surface top-level payload fields if the artifact exposes a title or summary
        payload = guidance.get("payload") or {}
        if isinstance(payload, dict):
            for key in ("title", "summary", "headline", "market_regime", "outlook"):
                if key in payload:
                    val = payload[key]
                    if isinstance(val, str) and len(val) < 200:
                        print(f"  {key.capitalize():<16} {val}")
    else:
        print("\n  Market Guidance: not retrieved (see 402 handling above)")

    # Research artifact
    if research:
        r = _summarize_artifact(research)
        _print_artifact_summary("Market Research Report Artifact", r)
        payload = research.get("payload") or {}
        if isinstance(payload, dict):
            for key in ("title", "summary", "headline", "report_type"):
                if key in payload:
                    val = payload[key]
                    if isinstance(val, str) and len(val) < 200:
                        print(f"  {key.capitalize():<16} {val}")
    else:
        print("\n  Market Research: not retrieved (see 402 handling above)")

    print("\n" + "=" * 56)
    print("[Research workflow complete]")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    discovery = step_discover()

    guidance: dict[str, Any] | None = None
    research: dict[str, Any] | None = None

    # Guidance and research are paid artifacts — they require subscription or x402 payment.
    # The workflow continues on 503 (store unavailable) or 404 (no artifact published yet)
    # so the discovery step alone can verify connectivity.
    try:
        guidance = step_fetch_guidance()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {404, 503}:
            print(f"  [skip] Guidance not available: {exc.response.status_code}")
        else:
            raise

    try:
        research = step_fetch_research()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {404, 503}:
            print(f"  [skip] Research not available: {exc.response.status_code}")
        else:
            raise

    print_research_workflow_summary(discovery, guidance, research)


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
