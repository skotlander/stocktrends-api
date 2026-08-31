"""Canonical Stock Trends x402 resource-discovery representation.

This module describes payable resources without executing them. Endpoint
membership and access metadata come from the runtime payment policy; request
contracts and examples come from ``discovery.endpoint_metadata``. Prices are
intentionally referenced through the live STC catalog rather than duplicated.
"""

from __future__ import annotations

import copy
from typing import Any

from discovery.endpoint_metadata import (
    AI_CONTEXT_URL,
    PRICING_CATALOG_URL,
    SERVICE_NAME,
    TOOLS_MANIFEST_URL,
    WORKFLOWS_URL,
    build_input_schema,
    get_endpoint_metadata,
)
from payments.policy_provider import (
    get_effective_endpoint_payment_policy,
    get_runtime_payment_policy_config,
)
from payments.x402 import (
    X402_DEFAULT_ASSET_TRANSFER_METHOD,
    X402_DEFAULT_NETWORK,
    X402_DEFAULT_SCHEME,
    X402_DEFAULT_TOKEN,
    X402_DEFAULT_TOKEN_DECIMALS,
    X402_DEFAULT_TOKEN_NAME,
    X402_DEFAULT_TOKEN_VERSION,
    X402_SELLER_ADDRESS,
)
from services.intelligence_artifact_availability import (
    match_paid_intelligence_artifact_route,
)


DISCOVERY_SCHEMA = "stocktrends.x402-discovery.v1"
PUBLIC_API_BASE_URL = "https://api.stocktrends.com"
CANONICAL_DISCOVERY_PATH = "/.well-known/x402"
CANONICAL_DISCOVERY_URL = f"{PUBLIC_API_BASE_URL}{CANONICAL_DISCOVERY_PATH}"
CANONICAL_OPENAPI_URL = f"{PUBLIC_API_BASE_URL}/v1/openapi.json"

X402_DISCOVERY_ALIASES = (
    CANONICAL_DISCOVERY_PATH,
    "/.well-known/x402.json",
    "/.well-known/x402-discovery",
    "/.well-known/x402-services.json",
)

# A policy entry may only be omitted by adding a deliberate, reviewable entry
# here. There are currently no discovery exceptions.
DISCOVERY_EXCEPTIONS: dict[tuple[str, str], str] = {}


class X402DiscoveryCompletenessError(RuntimeError):
    """Raised when runtime payment policy cannot be represented safely."""


def _absolute_url(path: str) -> str:
    return f"{PUBLIC_API_BASE_URL}{path}"


def _x402_payment_metadata() -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "version": 2,
        "scheme": X402_DEFAULT_SCHEME,
        "network": X402_DEFAULT_NETWORK,
        "asset": {
            "address": X402_DEFAULT_TOKEN,
            "name": X402_DEFAULT_TOKEN_NAME,
            "version": X402_DEFAULT_TOKEN_VERSION,
            "decimals": X402_DEFAULT_TOKEN_DECIMALS,
            "transfer_method": X402_DEFAULT_ASSET_TRANSFER_METHOD,
        },
        "proof_headers": ["PAYMENT-SIGNATURE", "X-Payment"],
    }
    if X402_SELLER_ADDRESS:
        metadata["pay_to"] = X402_SELLER_ADDRESS
    return metadata


def _resource_from_policy(policy: Any) -> dict[str, Any]:
    method = policy.method.upper()
    path = policy.path_pattern
    metadata = get_endpoint_metadata(path, method)
    if metadata is None:
        raise X402DiscoveryCompletenessError(
            f"{method} {path} is payment-governed but has no endpoint metadata"
        )

    safe_example = metadata.get("safe_example_request")
    if not isinstance(safe_example, dict):
        raise X402DiscoveryCompletenessError(
            f"{method} {path} is payment-governed but has no safe example"
        )
    if safe_example.get("method") != method:
        raise X402DiscoveryCompletenessError(
            f"{method} {path} safe example declares a different method"
        )
    example_path = safe_example.get("path")
    if not isinstance(example_path, str) or not example_path.startswith("/v1/"):
        raise X402DiscoveryCompletenessError(
            f"{method} {path} safe example has no executable v1 path"
        )

    metadata_rule_id = metadata.get("pricing_rule_id")
    if metadata_rule_id and metadata_rule_id != policy.pricing_rule_id:
        raise X402DiscoveryCompletenessError(
            f"{method} {path} metadata pricing rule {metadata_rule_id!r} does not "
            f"match runtime policy {policy.pricing_rule_id!r}"
        )

    effective_policy = get_effective_endpoint_payment_policy(path, method)
    if effective_policy is None:
        raise X402DiscoveryCompletenessError(
            f"{method} {path} is configured but has no effective runtime policy"
        )
    supported_rails = list(effective_policy.allowed_rails)
    anonymous_x402 = "x402" in supported_rails
    availability_gated = match_paid_intelligence_artifact_route(method, path) is not None

    resource: dict[str, Any] = {
        "method": method,
        "path": path,
        "url": _absolute_url(path),
        "name": metadata["tool_name"],
        "title": metadata["title"],
        "description": metadata["resource_description"],
        "analytical_role": metadata.get("analytical_role", metadata["category"]),
        "input_schema": build_input_schema(path),
        "safe_example_request": copy.deepcopy(safe_example),
        "pricing_rule_id": effective_policy.pricing_rule_id,
        "supported_rails": supported_rails,
        "anonymous_x402_challenge_supported": anonymous_x402,
        "pricing_catalog_url": PRICING_CATALOG_URL,
        "pricing": {
            "source": "stc_pricing_catalog",
            "pricing_rule_id": effective_policy.pricing_rule_id,
            "catalog_url": PRICING_CATALOG_URL,
            "live_cost_included": False,
        },
        "availability": (
            {
                "classification": "pre_payment_availability_gated",
                "serviceable_example_outcome": "402_when_available_otherwise_404_or_503",
                "reason": (
                    "A matching validated and serveable published artifact must exist before "
                    "the payment gate is reached."
                ),
                "possible_unpaid_statuses": [402, 404, 503],
            }
            if availability_gated
            else {
                "classification": "immediately_discoverable",
                "serviceable_example_outcome": "402_without_payment_proof",
                "possible_unpaid_statuses": [402],
            }
        ),
    }
    for field in (
        "inference_contract",
        "inference_provider",
        "interpretation_dependency",
        "interpretation_guidance",
        "required_interpretation_steps",
        "cognition_architecture",
        "provenance_reference",
    ):
        if field in metadata:
            resource[field] = copy.deepcopy(metadata[field])
    if anonymous_x402:
        resource["x402"] = _x402_payment_metadata()
    return resource


def build_x402_discovery() -> dict[str, Any]:
    """Build the side-effect-free manifest from canonical runtime sources."""
    config = get_runtime_payment_policy_config()
    resources: list[dict[str, Any]] = []
    exceptions: list[dict[str, str]] = []

    for policy in sorted(
        config.endpoint_payment_policies,
        key=lambda item: (item.path_pattern, item.method, item.endpoint_id),
    ):
        key = (policy.method.upper(), policy.path_pattern)
        exception_reason = DISCOVERY_EXCEPTIONS.get(key)
        if exception_reason:
            exceptions.append(
                {"method": key[0], "path": key[1], "reason": exception_reason}
            )
            continue
        resources.append(_resource_from_policy(policy))

    return {
        "schema": DISCOVERY_SCHEMA,
        "canonical_url": CANONICAL_DISCOVERY_URL,
        "compatibility_aliases": [_absolute_url(path) for path in X402_DISCOVERY_ALIASES],
        "service": {
            "name": SERVICE_NAME,
            "api_base_url": f"{PUBLIC_API_BASE_URL}/v1",
            "openapi_url": CANONICAL_OPENAPI_URL,
            "x402_discovery_url": CANONICAL_DISCOVERY_URL,
            "ai_tools_url": TOOLS_MANIFEST_URL,
            "workflows_url": WORKFLOWS_URL,
            "pricing_catalog_url": PRICING_CATALOG_URL,
            "ai_context_url": AI_CONTEXT_URL,
        },
        "x402": {
            "versions_supported": [2],
            "payment": _x402_payment_metadata(),
        },
        "request_lifecycle": {
            "serviceable_request_required_before_challenge": True,
            "statement": (
                "Routing, parsing, schema validation, and request-only semantic validation "
                "run before an execution-time 402 challenge."
            ),
            "discovery_is_execution_free": True,
        },
        "payment_architecture": {
            "pricing_unit": "STC",
            "pricing_source": PRICING_CATALOG_URL,
            "rails_are_transport_not_pricing": True,
            "runtime_supported_rails": sorted(
                {rail for resource in resources for rail in resource["supported_rails"]}
            ),
            "mpp_is_session_based_not_x402_challenge_based": True,
        },
        "resources": resources,
        "discovery_exceptions": exceptions,
    }
