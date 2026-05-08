# discovery/preview.py
#
# Compatibility wrapper for x402 stocktrends_preview generation. The preview
# content now comes from discovery.endpoint_metadata so descriptions, Bazaar
# output examples, AI tools, and workflow guidance can share one source.

from __future__ import annotations

from discovery.endpoint_metadata import build_endpoint_preview, iter_endpoint_metadata

_PREVIEW_BY_PATH: dict[str, dict] = {
    entry["path"]: build_endpoint_preview(entry["path"]) or {}
    for entry in iter_endpoint_metadata()
}


def get_endpoint_preview(
    path: str,
    *,
    pricing_rule_id: str | None = None,
    stc_cost: str | None = None,
    effective_price_usd: str | None = None,
) -> dict | None:
    """Return a preview for *path*, or None if no metadata is registered."""
    return build_endpoint_preview(
        path,
        pricing_rule_id=pricing_rule_id,
        stc_cost=stc_cost,
        effective_price_usd=effective_price_usd,
    )
