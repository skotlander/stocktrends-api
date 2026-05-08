from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from db import get_metering_engine

router = APIRouter(prefix="/pricing", tags=["pricing"])

# Catalog schema version — increment when the shape of /catalog changes.
_PRICING_CATALOG_VERSION = "2"


@router.get(
    "",
    summary="Pricing discovery metadata",
    description=(
        "Public planning infrastructure for agents and developer tooling. "
        "Explains payment identity, agent identity, accepted headers, payment rails, "
        "and how to combine /v1/pricing/catalog, /v1/workflows, and x402 402 previews "
        "before making paid data requests."
    ),
)
def get_pricing():
    return {
        "version": "2",
        "planning_role": {
            "purpose": "Explain how agents should identify themselves, choose a rail, and inspect costs before paid execution.",
            "recommended_sequence": [
                "GET /v1/ai/tools",
                "GET /v1/workflows",
                "GET /v1/pricing/catalog",
                "GET /v1/pricing",
                "Call paid endpoint; if x402 returns 402, inspect stocktrends_preview before paying.",
            ],
            "related_planning_endpoints": {
                "pricing_catalog": "/v1/pricing/catalog",
                "workflow_registry": "/v1/workflows",
                "cost_estimate": "/v1/cost-estimate",
                "ai_tools": "/v1/ai/tools",
            },
        },
        "agent_identity": {
            "supported": True,
            "purpose": (
                "Agent identity lets Stock Trends attribute usage to a persistent machine actor "
                "under a customer account. Identified agents may be auto-registered and can later "
                "be managed through dashboard and billing workflows."
            ),
            "identity_resolution": {
                "agent_identifier_source": "X-StockTrends-Agent-Id",
                "canonical_storage": {
                    "agent_identifier": "normalized external declared identity",
                    "agent_id": "internal registered api_agents.id when available",
                },
                "notes": [
                    "If X-StockTrends-Agent-Id is present, the request is treated as agent-attributed traffic.",
                    "Agent identifiers are normalized before lookup and registration.",
                    "When a valid customer context exists, first-seen agents may be auto-registered.",
                    "Disabled registered agents may be blocked.",
                ],
            },
            "recommended_headers": [
                {
                    "name": "X-StockTrends-Agent-Id",
                    "required_for_agent_identity": True,
                    "description": "Stable external identifier for the calling agent.",
                    "example": "editorial-agent-v1",
                },
                {
                    "name": "X-StockTrends-Agent-Type",
                    "required_for_agent_identity": False,
                    "description": "High-level agent category.",
                    "example": "editorial",
                },
                {
                    "name": "X-StockTrends-Agent-Vendor",
                    "required_for_agent_identity": False,
                    "description": "Vendor or platform operating the agent.",
                    "example": "stocktrends",
                },
                {
                    "name": "X-StockTrends-Agent-Version",
                    "required_for_agent_identity": False,
                    "description": "Agent software version.",
                    "example": "1.0.0",
                },
                {
                    "name": "X-StockTrends-Request-Purpose",
                    "required_for_agent_identity": False,
                    "description": "Optional statement of request purpose.",
                    "example": "weekly-editorial-generation",
                },
                {
                    "name": "X-StockTrends-Session-Id",
                    "required_for_agent_identity": False,
                    "description": "Optional session/workflow correlation id.",
                    "example": "run-2026-03-25-001",
                },
            ],
        },
        "payment_identity": {
            "supported_methods": ["subscription", "mpp", "x402"],
            "future_layers": ["stok"],
            "rail_guidance": {
                "subscription": "Account-based access with monthly STC allocation.",
                "x402": "Per-request HTTP 402 challenge/verify flow. Inspect stocktrends_preview before paying.",
                "mpp": "Session-based payments. STC is consumed from an active payment session and does not use the x402 challenge flow.",
                "stok": "Future incentive or discount layer; STOK does not replace STC pricing.",
            },
            "payment_headers": [
                {
                    "name": "X-StockTrends-Payment-Method",
                    "description": "Declared payment method for machine-pay capable flows.",
                    "example": "mpp",
                },
                {
                    "name": "X-StockTrends-Payment-Network",
                    "description": "Payment network identifier.",
                    "example": "base",
                },
                {
                    "name": "X-StockTrends-Payment-Token",
                    "description": "Optional payment token or asset identifier.",
                    "example": "USDC",
                },
                {
                    "name": "X-StockTrends-Payment-Reference",
                    "description": "Payment or settlement reference.",
                    "example": "invoice_12345",
                },
                {
                    "name": "X-StockTrends-Payment-Amount",
                    "description": "Native payment amount as declared by the caller.",
                    "example": "0.01",
                },
            ],
            "response_headers": [
                "X-StockTrends-Pricing-Rule",
                "X-StockTrends-Payment-Required",
                "X-StockTrends-Accepted-Payment-Methods",
            ],
        },
        "endpoint_families": {
            "stim": {
                "pricing_model": "subscription_or_agent_pay",
                "pricing_rule_default": "default_subscription",
                "agent_pay_supported": True,
                "payment_required": False,
                "accepted_payment_methods": [
                    "subscription",
                    "mpp",
                    "x402",
                ],
                "agent_headers": [
                    "X-StockTrends-Agent-Id",
                    "X-StockTrends-Agent-Type",
                    "X-StockTrends-Agent-Vendor",
                    "X-StockTrends-Agent-Version",
                    "X-StockTrends-Request-Purpose",
                    "X-StockTrends-Session-Id",
                ],
                "payment_headers": [
                    "X-StockTrends-Payment-Method",
                    "X-StockTrends-Payment-Network",
                    "X-StockTrends-Payment-Reference",
                    "X-StockTrends-Payment-Amount",
                    "X-StockTrends-Payment-Token",
                    "X-StockTrends-Pricing-Rule",
                ],
                "notes": [
                    "STIM endpoints are currently subscription-covered by default for entitled customers.",
                    "Identified agents can be attributed and logged even when subscription-backed.",
                    "Agent-native payment metadata is supported for discovery and logging.",
                    "Payment enforcement may be enabled in the future for selected routes.",
                ],
            },
            "ai": {
                "pricing_model": "free_metered",
                "pricing_rule_default": "default_free_metered",
                "payment_required": False,
                "notes": [
                    "Free-metered endpoints are logged but not billed.",
                ],
            },
            "breadth": {
                "pricing_model": "mixed",
                "notes": [
                    "/v1/breadth/sector/latest is free-metered.",
                    "Other breadth endpoints may require subscription.",
                ],
            },
            "general_protected_api": {
                "pricing_model": "subscription",
                "pricing_rule_default": "default_subscription",
                "payment_required": False,
            },
            "public": {
                "pricing_model": "free",
                "pricing_rule_default": "default_free",
                "payment_required": False,
            },
        },
        "examples": {
            "subscription_with_agent_identity": {
                "description": (
                    "Recommended pattern for subscribed customers that want request attribution "
                    "to a persistent agent."
                ),
                "headers": {
                    "Authorization": "Bearer <API_KEY>",
                    "X-StockTrends-Agent-Id": "editorial-agent-v1",
                    "X-StockTrends-Agent-Type": "editorial",
                    "X-StockTrends-Agent-Vendor": "stocktrends",
                    "X-StockTrends-Agent-Version": "1.0.0",
                    "X-StockTrends-Request-Purpose": "weekly-editorial-generation",
                    "X-StockTrends-Session-Id": "run-2026-03-25-001",
                },
            },
            "agent_pay_candidate": {
                "description": (
                    "Illustrative machine-pay pattern for endpoints that may support agent-native payment."
                ),
                "headers": {
                    "X-StockTrends-Agent-Id": "research-bot-17",
                    "X-StockTrends-Agent-Type": "research",
                    "X-StockTrends-Agent-Vendor": "external-lab",
                    "X-StockTrends-Agent-Version": "0.9.3",
                    "X-StockTrends-Request-Purpose": "factor-screening",
                    "X-StockTrends-Session-Id": "job-88421",
                    "X-StockTrends-Payment-Method": "mpp",
                    "X-StockTrends-Payment-Network": "base",
                    "X-StockTrends-Payment-Token": "USDC",
                    "X-StockTrends-Payment-Reference": "payment-ref-123",
                    "X-StockTrends-Payment-Amount": "0.01",
                },
            },
        },
    }


@router.get(
    "/catalog",
    summary="Live pricing rule catalog",
    description=(
        "Public planning infrastructure that returns all active pricing rules from the STC pricing engine. "
        "Each rule carries the declared endpoint price in STC units (cost_per_request), "
        "the access type, and the endpoint pattern it matches. "
        "Agents should call this endpoint once at startup to build a local cost map "
        "before making data requests, then use /v1/workflows to combine endpoint costs into a strategy. "
        "Response headers include x-st-pricing-version (catalog schema version) and "
        "x-st-pricing-updated-at (UTC timestamp when this catalog was served)."
    ),
)
def get_pricing_catalog(request: Request) -> JSONResponse:
    engine = get_metering_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    rule_name,
                    endpoint_pattern,
                    endpoint_family,
                    api_version,
                    access_type,
                    cost_per_request,
                    cost_unit,
                    requires_subscription,
                    requires_payment
                FROM api_pricing_rules
                WHERE is_active = 1
                ORDER BY endpoint_family, rule_name
                """
            )
        ).mappings().all()

    catalog = [
        {
            "pricing_rule_id": row["rule_name"],
            "endpoint_pattern": row["endpoint_pattern"],
            "endpoint_family": row["endpoint_family"],
            "api_version": row["api_version"],
            "access_type": row["access_type"],
            "cost_per_request": float(row["cost_per_request"]) if row["cost_per_request"] is not None else 0.0,
            "cost_unit": row["cost_unit"],
            "requires_subscription": bool(row["requires_subscription"]),
            "requires_payment": bool(row["requires_payment"]),
        }
        for row in rows
    ]

    served_at = datetime.now(timezone.utc).isoformat()

    return JSONResponse(
        content={
            "request_id": getattr(request.state, "request_id", None),
            "planning_role": {
                "purpose": "Concrete endpoint price map for agent budgeting before paid execution.",
                "unit": "STC",
                "unit_description": (
                    "Stock Trends Credits. Current operational policy treats 1 STC as approximately 1 USD, "
                    "but STC remains the internal accounting unit."
                ),
                "recommended_use": [
                    "Fetch at agent startup.",
                    "Join pricing_rule_id values with /v1/workflows steps.",
                    "Use HTTP 402 stocktrends_preview as the final per-request confirmation before x402 payment.",
                ],
                "public_behavior_note": (
                    "This endpoint is public under current API behavior. A follow-up should reconcile "
                    "all static auth metadata and metering policy comments so every discovery surface says the same thing."
                ),
            },
            "count": len(catalog),
            "rules": catalog,
        },
        headers={
            "x-st-pricing-version": _PRICING_CATALOG_VERSION,
            "x-st-pricing-updated-at": served_at,
        },
    )
