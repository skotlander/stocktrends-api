"""Canonical Stock Trends x402 resource-discovery representation.

This module describes payable resources without executing them. Endpoint
membership and access metadata come from the runtime payment policy; request
contracts and examples come from ``discovery.endpoint_metadata``. Prices are
intentionally referenced through the live STC catalog rather than duplicated.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from discovery.endpoint_metadata import (
    AI_CONTEXT_URL,
    PRICING_CATALOG_URL,
    PUBLIC_API_BASE_URL,
    SERVICE_NAME,
    TOOLS_MANIFEST_URL,
    WORKFLOWS_URL,
    X402_DISCOVERY_PATH,
    build_input_schema,
    get_endpoint_metadata,
)
from discovery.service_meta import (
    SERVICE_EVALUATION_GUIDANCE_SOURCE,
    SERVICE_EVALUATION_GUIDANCE_SUMMARY,
)
import payments.policy_provider as payment_policy
import payments.x402_contract as x402_contract
from services.intelligence_artifact_availability import (
    match_paid_intelligence_artifact_route,
)

logger = logging.getLogger("stocktrends_api.x402_discovery")

DISCOVERY_SCHEMA = "stocktrends.x402-discovery.v1"
# Same value as before; sourced from the shared constant so the path exists in
# one place rather than being spelled out in two modules.
CANONICAL_DISCOVERY_PATH = X402_DISCOVERY_PATH
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

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "resource_contract_unrepresentable",
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.method = method
        self.path = path


class X402DiscoverySystemicFailure(RuntimeError):
    """Raised when runtime discovery cannot represent any governed resource."""


_ALLOWED_PRICING_FIELDS = {
    "source",
    "pricing_rule_id",
    "catalog_url",
    "live_cost_included",
}
_FORBIDDEN_RESOURCE_PRICE_FIELDS = {
    "stc_cost",
    "price",
    "price_stc",
    "price_usd",
    "amount_usd",
    "effective_price_usd",
    "unit_price",
    "cost",
    "cost_usd",
    "cost_per_request",
    "request_price",
}


def _completeness_error(
    code: str,
    method: str,
    path: str,
    detail: str,
) -> X402DiscoveryCompletenessError:
    return X402DiscoveryCompletenessError(
        f"{method} {path} {detail}",
        error_code=code,
        method=method,
        path=path,
    )


def validate_discovery_resource_pricing_contract(resource: dict[str, Any]) -> None:
    """Reject duplicated endpoint prices while allowing canonical references."""
    method = str(resource.get("method") or "UNKNOWN")
    path = str(resource.get("path") or "<unknown>")
    allowed_reference_fields = {"pricing_rule_id", "pricing_catalog_url", "pricing"}
    forbidden = sorted(
        key
        for key in resource
        if key in _FORBIDDEN_RESOURCE_PRICE_FIELDS
        or (
            key not in allowed_reference_fields
            and (
                key.startswith("price_")
                or key.startswith("cost_")
                or key.endswith("_price")
                or key.endswith("_cost")
                or key.endswith("_price_usd")
                or key.endswith("_cost_usd")
                or key.endswith("_amount_usd")
            )
        )
    )
    if forbidden:
        raise _completeness_error(
            "duplicated_endpoint_price",
            method,
            path,
            f"publishes forbidden service-price fields: {', '.join(forbidden)}",
        )

    pricing = resource.get("pricing")
    if not isinstance(pricing, dict):
        raise _completeness_error(
            "missing_pricing_reference",
            method,
            path,
            "has no canonical pricing reference",
        )
    unexpected = sorted(set(pricing).difference(_ALLOWED_PRICING_FIELDS))
    if unexpected:
        raise _completeness_error(
            "duplicated_endpoint_price",
            method,
            path,
            f"publishes non-reference pricing fields: {', '.join(unexpected)}",
        )
    if pricing.get("live_cost_included") is not False:
        raise _completeness_error(
            "duplicated_endpoint_price",
            method,
            path,
            "must declare live_cost_included=false",
        )


def _absolute_url(path: str) -> str:
    return f"{PUBLIC_API_BASE_URL}{path}"


def _x402_payment_metadata() -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "version": x402_contract.X402_VERSION,
        "scheme": x402_contract.X402_DEFAULT_SCHEME,
        "network": x402_contract.X402_DEFAULT_NETWORK,
        "asset": {
            "address": x402_contract.X402_DEFAULT_TOKEN,
            "name": x402_contract.X402_DEFAULT_TOKEN_NAME,
            "version": x402_contract.X402_DEFAULT_TOKEN_VERSION,
            "decimals": x402_contract.X402_DEFAULT_TOKEN_DECIMALS,
            "transfer_method": x402_contract.X402_DEFAULT_ASSET_TRANSFER_METHOD,
        },
        "proof_headers": list(x402_contract.X402_PROOF_HEADERS),
    }
    if x402_contract.X402_SELLER_ADDRESS:
        metadata["pay_to"] = x402_contract.X402_SELLER_ADDRESS
    return metadata


def _resource_from_policy(policy: Any, config: Any) -> dict[str, Any]:
    method = policy.method.upper()
    path = policy.path_pattern
    metadata = get_endpoint_metadata(path, method)
    if metadata is None:
        raise _completeness_error(
            "missing_endpoint_metadata",
            method,
            path,
            "is payment-governed but has no endpoint metadata",
        )

    safe_example = metadata.get("safe_example_request")
    if not isinstance(safe_example, dict):
        raise _completeness_error(
            "missing_safe_example",
            method,
            path,
            "is payment-governed but has no safe example",
        )
    if safe_example.get("method") != method:
        raise _completeness_error(
            "safe_example_method_mismatch",
            method,
            path,
            "safe example declares a different method",
        )
    example_path = safe_example.get("path")
    if not isinstance(example_path, str) or not example_path.startswith("/v1/"):
        raise _completeness_error(
            "safe_example_path_invalid",
            method,
            path,
            "safe example has no executable v1 path",
        )

    metadata_rule_id = metadata.get("pricing_rule_id")
    if metadata_rule_id and metadata_rule_id != policy.pricing_rule_id:
        raise _completeness_error(
            "pricing_rule_mismatch",
            method,
            path,
            f"metadata pricing rule {metadata_rule_id!r} does not match runtime "
            f"policy {policy.pricing_rule_id!r}",
        )

    effective_policy = payment_policy.get_effective_endpoint_payment_policy_from_config(
        config,
        path,
        method,
    )
    if effective_policy is None:
        raise _completeness_error(
            "missing_effective_policy",
            method,
            path,
            "is configured but has no effective runtime policy",
        )
    supported_rails = list(effective_policy.allowed_rails)
    anonymous_x402 = "x402" in supported_rails
    availability_gated = (
        match_paid_intelligence_artifact_route(method, example_path) is not None
    )

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
    validate_discovery_resource_pricing_contract(resource)
    return resource


def build_x402_discovery(*, strict: bool = True) -> dict[str, Any]:
    """Build discovery from all endpoint policies in one runtime snapshot.

    ``strict=True`` is the architectural/CI contract. Runtime HTTP serving uses
    ``strict=False`` so one remotely introduced defect degrades one resource
    rather than collapsing the entire public discovery endpoint.
    """
    config = payment_policy.get_runtime_payment_policy_config()
    resources: list[dict[str, Any]] = []
    exceptions: list[dict[str, str]] = []
    runtime_failure_count = 0

    for policy in sorted(
        config.endpoint_payment_policies,
        key=lambda item: (item.path_pattern, item.method, item.endpoint_id),
    ):
        key = (policy.method.upper(), policy.path_pattern)
        exception_reason = DISCOVERY_EXCEPTIONS.get(key)
        if exception_reason:
            exceptions.append(
                {
                    "method": key[0],
                    "path": key[1],
                    "error_code": "documented_discovery_exception",
                    "reason": exception_reason,
                }
            )
            continue
        try:
            resources.append(_resource_from_policy(policy, config))
        except Exception as exc:
            if strict:
                raise
            runtime_failure_count += 1
            error_code = getattr(
                exc,
                "error_code",
                "resource_contract_unrepresentable",
            )
            exceptions.append(
                {
                    "method": key[0],
                    "path": key[1],
                    "error_code": str(error_code),
                    "reason": "resource_contract_unrepresentable",
                }
            )
            logger.error(
                "Omitting unrepresentable x402 discovery resource %s %s (%s)",
                key[0],
                key[1],
                error_code,
                exc_info=True,
            )

    if (
        config.endpoint_payment_policies
        and not resources
        and runtime_failure_count
    ):
        logger.critical(
            "x402 discovery failed to represent every governed resource; "
            "refusing to publish an empty successful manifest"
        )
        raise X402DiscoverySystemicFailure(
            "No governed x402 discovery resources could be represented."
        )

    return {
        "schema": DISCOVERY_SCHEMA,
        "complete": not exceptions,
        "membership": "all_endpoint_payment_policies_with_effective_remaining_rails",
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
        # Service-level, not per-resource. A client reading this manifest is by
        # definition deciding whether a payable resource is worth acquiring, so
        # the evaluation procedure belongs here — once, as a reference. Repeating
        # it on every resource entry would bloat the manifest and give the same
        # sentence as many places to drift apart.
        #
        # It is procedural and states no conclusion: it says how to evaluate, not
        # that anything is worth buying. It names only public, non-metered
        # resources, so following it cannot cause paid execution, and it adds no
        # price of its own — /v1/pricing/catalog remains the price source.
        "acquisition_evaluation": {
            "guidance": SERVICE_EVALUATION_GUIDANCE_SUMMARY,
            "canonical_source": SERVICE_EVALUATION_GUIDANCE_SOURCE,
            "inspect_before_deciding": {
                "what_is_offered": [
                    CANONICAL_DISCOVERY_URL,
                    TOOLS_MANIFEST_URL,
                    WORKFLOWS_URL,
                ],
                "how_to_interpret_it": [
                    _absolute_url("/v1/meta/inference"),
                    _absolute_url("/v1/meta/stim"),
                    _absolute_url("/v1/meta/indicators"),
                ],
                "what_evidence_exists": [
                    _absolute_url("/v1/selections/stim-select/outcomes/summary"),
                    _absolute_url("/v1/stocktrends/portfolios"),
                ],
                "what_it_costs": [
                    PRICING_CATALOG_URL,
                    _absolute_url("/v1/cost-estimate"),
                ],
            },
            "category_separation": (
                "A service description is not methodology, and methodology is not "
                "evidence. Read each from its own source."
            ),
            "availability_is_not_evidence": (
                "Listing a resource here says nothing about whether its output is "
                "useful for a given task."
            ),
            "scope_rule": "Inspect only the resources relevant to the task.",
            "no_payment_required_to_evaluate": (
                "Every resource named here is public and non-metered. Reading them "
                "does not execute paid work."
            ),
            "decision_is_the_callers": (
                "This describes how to evaluate. It states no conclusion and "
                "recommends acquiring nothing."
            ),
            # Kept out of the evidence category on purpose: it illustrates shape,
            # it does not measure anything.
            "illustrative_capability_example": {
                "endpoint": _absolute_url("/v1/ai/proof/market-edge"),
                "shows": "response structure and how context layers compose",
                "is_not": (
                    "not empirical evidence, not realized outcomes, not predictive "
                    "performance, and not investment performance"
                ),
            },
        },
        "x402": {
            "versions_supported": [x402_contract.X402_VERSION],
            "payment": _x402_payment_metadata(),
        },
        "request_lifecycle": {
            # Challenge issuance and payment execution are separate halves of
            # the protocol, and only the second requires a serviceable request.
            # A recognized fixed-price resource answers an unpaid probe of its
            # canonical URL with the challenge, so the payment and input
            # contract is readable without constructing a request first.
            # Availability-gated resources are the documented exception; each
            # resource entry states its own `possible_unpaid_statuses`.
            "serviceable_request_required_before_challenge": False,
            "serviceable_request_required_before_settlement": True,
            "statement": (
                "A 402 challenge quotes a price and describes a resource; it moves no "
                "money. For a recognized fixed-price payable resource presented with no "
                "payment proof, it is issued before application-input validation. "
                "Routing, parsing, schema validation, and request-only semantic "
                "validation all run before any payment verification or settlement."
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
