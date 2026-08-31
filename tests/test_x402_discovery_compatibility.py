from __future__ import annotations

import ast
import copy
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

import main
import middleware.api_key as api_key_module
import middleware.metering as metering_module
import metering.logger as metering_logger_module
import payments.enforcement as enforcement_module
import payments.mpp_client as mpp_client_module
import payments.policy_provider as policy_provider_module
import payments.x402_contract as x402_contract_module
import routers.ai as ai_router_module
import routers.instruments as instruments_router_module
import routers.leadership as leadership_router_module
import routers.pricing as pricing_router_module
import routers.selections as selections_router_module
import routers.stim as stim_router_module
import routers.stocktrends_portfolios as stocktrends_portfolios_router_module
import routers.stocktrends_strategies as stocktrends_strategies_router_module
import routers.stwr as stwr_router_module
import routers.workflows as workflows_router_module
import services.intelligence_artifact_availability as availability_module
import discovery.x402_discovery as x402_discovery_module
from discovery.endpoint_metadata import get_endpoint_metadata
from discovery.x402_discovery import (
    CANONICAL_DISCOVERY_URL,
    DISCOVERY_SCHEMA,
    X402DiscoveryCompletenessError,
    X402DiscoverySystemicFailure,
    X402_DISCOVERY_ALIASES,
    build_x402_discovery,
    validate_discovery_resource_pricing_contract,
)
from middleware.api_key import (
    is_internal_admin_api_path,
    is_truly_public_api_path,
)
from payments.mpp import MPP_PAYMENT_CHANNEL_ID_HEADERS, MPP_REQUIRED_HEADERS
from payments.policy_provider import get_effective_endpoint_payment_policy
from payments.x402 import (
    extract_payment_signature,
    has_payment_signature,
    is_x402_payment_method,
)
from services.intelligence_artifact_availability import (
    match_paid_intelligence_artifact_route,
)
from support.payment_harness import (
    payment_governed_routes,
    rows_engine,
    v1_path,
    x402_headers,
)


_DISCOVERY_SOURCE = Path(x402_discovery_module.__file__)
_ALLOWED_SERVICE_IMPORT = "services.intelligence_artifact_availability"
_FORBIDDEN_DISCOVERY_CALLS = {
    "authorize_mpp_payment",
    "capture_mpp_payment",
    "configured_intelligence_artifact_store",
    "get_engine",
    "get_metering_engine",
    "log_api_request_economics",
    "log_api_request_event",
    "settle_with_facilitator",
    "verify_with_facilitator",
    "void_mpp_authorization",
}


def _discovery_purity_violations(source: str) -> list[str]:
    """Inspect imports and calls, including qualified forbidden mutations."""
    tree = ast.parse(source)
    violations: list[str] = []

    def inspect_module(module: str) -> None:
        forbidden = (
            module == "db"
            or module.startswith("db.")
            or module == "payments.x402"
            or module.startswith("payments.enforcement")
            or module.startswith("payments.mpp_client")
            or module.startswith("routers")
            or module.startswith("middleware.metering")
            or module == "metering"
            or module.startswith("metering.")
            or (
                module.startswith("services")
                and module != _ALLOWED_SERVICE_IMPORT
            )
        )
        if forbidden:
            violations.append(f"forbidden import: {module}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                inspect_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            inspect_module(node.module or "")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            else:
                call_name = ""
            if call_name in _FORBIDDEN_DISCOVERY_CALLS:
                violations.append(f"forbidden call: {call_name}")

    return violations


def _stub_request_logging(monkeypatch) -> None:
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(metering_module, "log_api_request_economics", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda *_a, **_kw: (Decimal("0"), Decimal("0")),
    )
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda *_a, **_kw: None)


def _resource_key(resource: dict) -> tuple[str, str]:
    return resource["method"], resource["path"]


def _manifest_keys(manifest: dict) -> set[tuple[str, str]]:
    represented = {_resource_key(resource) for resource in manifest["resources"]}
    represented.update(
        (exception["method"], exception["path"])
        for exception in manifest["discovery_exceptions"]
    )
    return represented


def _assert_mounted_enforcement_surface_is_discoverable(manifest: dict) -> None:
    enforced = {
        (method, v1_path(route))
        for route, method in payment_governed_routes()
    }
    missing = enforced.difference(_manifest_keys(manifest))
    assert not missing, (
        "mounted routes selected by runtime payment enforcement lack exact "
        f"discovery contracts: {sorted(missing)}"
    )


def test_well_known_aliases_are_anonymous_equivalent_and_canonical(monkeypatch):
    _stub_request_logging(monkeypatch)
    payloads = []
    with TestClient(main.app) as client:
        for alias in X402_DISCOVERY_ALIASES:
            response = client.get(alias)
            assert response.status_code == 200
            payloads.append(response.json())

    assert all(payload == payloads[0] for payload in payloads[1:])
    manifest = payloads[0]
    assert manifest["schema"] == DISCOVERY_SCHEMA
    assert manifest["canonical_url"] == CANONICAL_DISCOVERY_URL
    assert manifest["service"]["openapi_url"] == "https://api.stocktrends.com/v1/openapi.json"
    assert manifest["request_lifecycle"]["serviceable_request_required_before_challenge"] is True
    assert manifest["payment_architecture"]["pricing_unit"] == "STC"


def test_manifest_read_has_no_payment_data_or_artifact_side_effects(monkeypatch):
    _stub_request_logging(monkeypatch)
    calls = Counter()

    def poison(name):
        def _poison(*_args, **_kwargs):
            calls[name] += 1
            raise AssertionError(f"discovery invoked forbidden side effect: {name}")

        return _poison

    monkeypatch.setattr(enforcement_module, "verify_with_facilitator", poison("x402_verify"))
    monkeypatch.setattr(enforcement_module, "settle_with_facilitator", poison("x402_settle"))
    monkeypatch.setattr(mpp_client_module, "authorize_mpp_payment", poison("mpp_authorize"))
    monkeypatch.setattr(mpp_client_module, "capture_mpp_payment", poison("mpp_capture"))
    monkeypatch.setattr(mpp_client_module, "void_mpp_authorization", poison("mpp_void"))
    monkeypatch.setattr(ai_router_module, "get_engine", poison("market_data"))
    monkeypatch.setattr(ai_router_module, "get_metering_engine", poison("pricing_db"))
    monkeypatch.setattr(
        metering_logger_module,
        "log_api_request_event",
        poison("direct_request_event"),
    )
    monkeypatch.setattr(
        metering_logger_module,
        "log_api_request_economics",
        poison("direct_request_economics"),
    )
    monkeypatch.setattr(
        metering_logger_module,
        "get_metering_engine",
        poison("direct_metering_engine"),
    )
    monkeypatch.setattr(
        availability_module,
        "configured_intelligence_artifact_store",
        poison("artifact_store"),
    )

    with TestClient(main.app) as client:
        response = client.get(X402_DISCOVERY_ALIASES[0])

    assert response.status_code == 200
    assert calls == Counter()


def test_discovery_builder_dependency_firewall_is_structurally_enforced():
    source = _DISCOVERY_SOURCE.read_text(encoding="utf-8")
    assert _discovery_purity_violations(source) == []

    db_mutation = source + "\nimport db\ndef _mutation():\n    db.get_engine()\n"
    facilitator_mutation = (
        source
        + "\nimport payments.x402\ndef _mutation():\n"
        + "    payments.x402.verify_with_facilitator(None, None)\n"
    )
    metering_mutation = (
        source
        + "\nfrom metering.logger import log_api_request_economics\n"
        + "def _mutation():\n"
        + "    log_api_request_economics({})\n"
    )
    assert "forbidden import: db" in _discovery_purity_violations(db_mutation)
    assert "forbidden call: get_engine" in _discovery_purity_violations(db_mutation)
    assert "forbidden import: payments.x402" in _discovery_purity_violations(
        facilitator_mutation
    )
    assert "forbidden call: verify_with_facilitator" in (
        _discovery_purity_violations(facilitator_mutation)
    )
    assert "forbidden import: metering.logger" in _discovery_purity_violations(
        metering_mutation
    )
    assert "forbidden call: log_api_request_economics" in (
        _discovery_purity_violations(metering_mutation)
    )


def test_manifest_spies_are_proven_on_the_real_paid_request_path(
    payment_harness,
    monkeypatch,
):
    """Positive control: the absence spies observe production enforcement."""
    monkeypatch.setattr(stim_router_module, "get_engine", lambda: rows_engine([]))
    response = payment_harness.client.get(
        "/v1/stim/latest?symbol_exchange=IBM-N",
        headers=x402_headers(reference="discovery-purity-positive-control"),
    )

    assert response.status_code in {200, 404}
    assert payment_harness.facilitator.verify_count == 1
    assert payment_harness.facilitator.settle_count == 1
    assert payment_harness.mpp.authorize_count == 0


def test_manifest_behavioral_spies_observe_no_payment_or_economics_activity(
    payment_harness,
):
    response = payment_harness.client.get(X402_DISCOVERY_ALIASES[0])

    assert response.status_code == 200
    assert payment_harness.facilitator.verify_count == 0
    assert payment_harness.facilitator.settle_count == 0
    assert payment_harness.mpp.authorize_count == 0
    assert payment_harness.mpp.capture_count == 0
    assert payment_harness.mpp.void_count == 0
    assert payment_harness.logs.economics == []


def test_manifest_reconciles_with_runtime_payment_governed_surface():
    config = policy_provider_module.get_runtime_payment_policy_config()
    runtime_governed = {
        (policy.method.upper(), policy.path_pattern)
        for policy in config.endpoint_payment_policies
    }
    manifest = build_x402_discovery()

    assert _manifest_keys(manifest) == runtime_governed
    assert len(manifest["resources"]) == len(runtime_governed)

    for resource in manifest["resources"]:
        policy = get_effective_endpoint_payment_policy(
            resource["path"], resource["method"]
        )
        assert policy is not None
        assert resource["pricing_rule_id"] == policy.pricing_rule_id
        assert resource["supported_rails"] == list(policy.allowed_rails)
        assert resource["safe_example_request"]


def test_mounted_payment_enforcement_surface_is_also_discoverable():
    _assert_mounted_enforcement_surface_is_discoverable(build_x402_discovery())


def test_prefix_only_enforcement_route_fails_discovery_completeness(
    monkeypatch,
):
    baseline = policy_provider_module.get_runtime_payment_policy_config()
    mutated = replace(
        baseline,
        enforcement_path_prefixes=(
            *baseline.enforcement_path_prefixes,
            "/v1/agents",
        ),
    )
    monkeypatch.setattr(
        policy_provider_module,
        "get_runtime_payment_policy_config",
        lambda *_args, **_kwargs: mutated,
    )

    manifest = build_x402_discovery()
    with pytest.raises(AssertionError, match="lack exact discovery contracts"):
        _assert_mounted_enforcement_surface_is_discoverable(manifest)


def test_availability_classification_is_derived_from_existing_boundary():
    for resource in build_x402_discovery()["resources"]:
        example = resource["safe_example_request"]
        availability_gated = match_paid_intelligence_artifact_route(
            resource["method"], example["path"]
        ) is not None
        expected = (
            "pre_payment_availability_gated"
            if availability_gated
            else "immediately_discoverable"
        )
        assert resource["availability"]["classification"] == expected


def test_immediately_discoverable_safe_examples_reach_402_without_payment_side_effects(
    payment_harness,
):
    immediate = [
        resource
        for resource in build_x402_discovery()["resources"]
        if resource["availability"]["classification"] == "immediately_discoverable"
        and resource["anonymous_x402_challenge_supported"]
    ]
    assert immediate
    expected_eligible = {_resource_key(resource) for resource in immediate}
    exercised: set[tuple[str, str]] = set()

    for resource in immediate:
        example = resource["safe_example_request"]
        response = payment_harness.client.request(
            example["method"],
            example["path"],
            params=example.get("query"),
            json=example.get("json"),
        )
        assert response.status_code == 402, (
            resource["method"],
            resource["path"],
            response.status_code,
            response.text,
        )
        assert payment_harness.facilitator.verify_count == 0
        assert payment_harness.facilitator.settle_count == 0
        assert payment_harness.mpp.authorize_count == 0
        assert payment_harness.mpp.capture_count == 0
        assert payment_harness.mpp.void_count == 0
        exercised.add(_resource_key(resource))

    assert exercised == expected_eligible
    assert len(exercised) == len(immediate)


def test_availability_gated_examples_preserve_documented_pre_payment_outcomes(
    payment_harness,
):
    gated = [
        resource
        for resource in build_x402_discovery()["resources"]
        if resource["availability"]["classification"]
        == "pre_payment_availability_gated"
    ]
    assert gated

    for resource in gated:
        example = resource["safe_example_request"]
        response = payment_harness.client.request(
            example["method"],
            example["path"],
            params=example.get("query"),
        )
        assert response.status_code in resource["availability"]["possible_unpaid_statuses"]
        assert payment_harness.facilitator.verify_count == 0
        assert payment_harness.facilitator.settle_count == 0
        assert payment_harness.mpp.authorize_count == 0
        assert payment_harness.mpp.capture_count == 0
        assert payment_harness.mpp.void_count == 0


def test_openapi_security_payment_extensions_and_safe_examples_agree_with_runtime(monkeypatch):
    _stub_request_logging(monkeypatch)
    main.v1.openapi_schema = None
    with TestClient(main.app) as client:
        schema = client.get("/v1/openapi.json").json()

    schemes = schema["components"]["securitySchemes"]
    advertised_proof_schemes = {
        name: scheme
        for name, scheme in schemes.items()
        if scheme.get("name") in x402_contract_module.X402_PROOF_HEADERS
    }
    assert {
        scheme["name"] for scheme in advertised_proof_schemes.values()
    } == set(x402_contract_module.X402_PROOF_HEADERS)

    for scheme_name, scheme in advertised_proof_schemes.items():
        proof_header = scheme["name"]
        runtime_headers = Headers({proof_header: "test-payment-proof"})
        assert is_x402_payment_method(runtime_headers) is True
        assert has_payment_signature(runtime_headers) is True
        assert extract_payment_signature(runtime_headers) == "test-payment-proof"

    for path, path_item in schema["paths"].items():
        external_path = f"/v1{path}"
        for method, operation in path_item.items():
            if method not in main.HTTP_METHODS:
                continue
            policy = get_effective_endpoint_payment_policy(external_path, method.upper())
            if policy is None and is_internal_admin_api_path(external_path):
                assert operation["security"] == [{"InternalSecretAuth": []}]
            elif policy is None and is_truly_public_api_path(external_path):
                assert operation["security"] == []
            elif policy is None:
                assert operation["security"] == [
                    {"ApiKeyAuth": []},
                    {"BearerAuth": []},
                ]
            else:
                payment = operation["x-stocktrends-payment"]
                assert payment["supported_rails"] == list(policy.allowed_rails)
                assert payment["pricing_rule_id"] == policy.pricing_rule_id
                assert payment["serviceable_request_required_before_challenge"] is True
                if "x402" in policy.machine_payment_rails:
                    for scheme_name in advertised_proof_schemes:
                        assert {scheme_name: []} in operation["security"]
                    assert payment["anonymous_challenge_supported"] is True
                    assert payment["x402_version"] == x402_contract_module.X402_VERSION
                    assert payment["x402_proof_headers"] == list(
                        x402_contract_module.X402_PROOF_HEADERS
                    )

            metadata = get_endpoint_metadata(external_path, method.upper())
            if metadata and isinstance(metadata.get("safe_example_request"), dict):
                assert operation["x-stocktrends-safe-example-request"] == metadata[
                    "safe_example_request"
                ]


def test_observability_openapi_and_runtime_require_internal_secret_independently(
    monkeypatch,
):
    """A customer-key bypass is not a public-access classification."""
    _stub_request_logging(monkeypatch)
    monkeypatch.setenv("INTERNAL_OBSERVABILITY_SECRET", "internal-test-secret")

    def valid_customer_api_key(_self, _path: str, _raw_key: str):
        return True, {
            "api_key_id": "customer-key-id",
            "customer_id": "customer-id",
            "subscription_id": "subscription-id",
            "plan_code": "pro",
            "actor_type": "external_customer",
            "monthly_quota": 1000,
        }

    monkeypatch.setattr(
        api_key_module.ApiKeyMiddleware,
        "_authenticate_api_key",
        valid_customer_api_key,
    )
    main.v1.openapi_schema = None

    with TestClient(main.app) as client:
        schema = client.get("/v1/openapi.json").json()
        missing_secret = client.get(
            "/v1/observability/mpp/sessions/contract-test-channel"
        )
        customer_key_only = client.get(
            "/v1/observability/mpp/sessions/contract-test-channel",
            headers={"X-API-Key": "customer-key-does-not-grant-internal-access"},
        )

    assert schema["paths"]["/ai/tools"]["get"]["security"] == []
    observability = schema["paths"][
        "/observability/mpp/sessions/{payment_channel_id}"
    ]["get"]
    assert observability["security"] == [{"InternalSecretAuth": []}]
    assert observability["security"] != []
    assert schema["components"]["securitySchemes"]["InternalSecretAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Internal-Secret",
        "description": (
            "Internal/admin authentication for observability operations. "
            "A customer API key does not satisfy this requirement."
        ),
    }
    assert missing_secret.status_code == 403
    assert missing_secret.json()["detail"] == "Internal access only"
    assert customer_key_only.status_code == 403
    assert customer_key_only.json()["detail"] == "Internal access only"


def test_mpp_openapi_contract_is_derived_from_runtime_header_constants(monkeypatch):
    _stub_request_logging(monkeypatch)
    main.v1.openapi_schema = None
    with TestClient(main.app) as client:
        schema = client.get("/v1/openapi.json").json()

    operation = schema["paths"]["/stim/latest"]["get"]
    mpp = operation["x-stocktrends-payment"]["mpp"]
    expected_required = {header.lower() for header in MPP_REQUIRED_HEADERS}
    expected_channels = {
        header.lower() for header in MPP_PAYMENT_CHANNEL_ID_HEADERS
    }

    assert {header.lower() for header in mpp["required_headers"]} == expected_required
    assert {
        header.lower()
        for header in mpp["required_one_of"]["payment_channel_id_headers"]
    } == expected_channels
    assert (
        mpp["canonical_payment_channel_id_header"].lower()
        == MPP_PAYMENT_CHANNEL_ID_HEADERS[0]
    )
    assert {
        header.lower() for header in mpp["legacy_payment_channel_id_headers"]
    } == {header.lower() for header in MPP_PAYMENT_CHANNEL_ID_HEADERS[1:]}
    assert mpp["uses_x402_challenge_flow"] is False

    documented_headers = {
        header.lower() for header in mpp["security_schemes_by_header"]
    }
    assert documented_headers == expected_required | expected_channels
    assert "x-stocktrends-agent-id" not in documented_headers

    schemes = schema["components"]["securitySchemes"]
    for header, scheme_name in mpp["security_schemes_by_header"].items():
        assert schemes[scheme_name]["name"] == header

    required_scheme_names = {
        mpp["security_schemes_by_header"][header]
        for header in mpp["required_headers"]
    }
    channel_scheme_names = {
        mpp["security_schemes_by_header"][header]
        for header in mpp["required_one_of"]["payment_channel_id_headers"]
    }
    actual_mpp_alternatives = {
        frozenset(requirement)
        for requirement in operation["security"]
        if set(requirement) & channel_scheme_names
    }
    expected_mpp_alternatives = {
        frozenset(required_scheme_names | {channel_scheme})
        for channel_scheme in channel_scheme_names
    }
    assert actual_mpp_alternatives == expected_mpp_alternatives


def test_proof_header_contract_moves_runtime_openapi_and_manifest_together(
    monkeypatch,
):
    _stub_request_logging(monkeypatch)
    monkeypatch.setattr(
        x402_contract_module,
        "X402_PROOF_HEADERS",
        ("PAYMENT-SIGNATURE",),
    )
    main.v1.openapi_schema = None

    with TestClient(main.app) as client:
        schema = client.get("/v1/openapi.json").json()
        manifest = client.get(CANONICAL_DISCOVERY_URL.removeprefix(
            "https://api.stocktrends.com"
        )).json()

    documented_names = {
        scheme.get("name")
        for scheme in schema["components"]["securitySchemes"].values()
        if scheme.get("name") in {"PAYMENT-SIGNATURE", "X-Payment"}
    }
    assert documented_names == {"PAYMENT-SIGNATURE"}
    assert manifest["x402"]["payment"]["proof_headers"] == ["PAYMENT-SIGNATURE"]
    assert all(
        resource.get("x402", {}).get("proof_headers") == ["PAYMENT-SIGNATURE"]
        for resource in manifest["resources"]
        if resource["anonymous_x402_challenge_supported"]
    )

    removed_header = Headers({"X-Payment": "removed-proof"})
    assert is_x402_payment_method(removed_header) is False
    assert has_payment_signature(removed_header) is False
    assert extract_payment_signature(removed_header) is None

    retained_header = Headers({"PAYMENT-SIGNATURE": "retained-proof"})
    assert is_x402_payment_method(retained_header) is True
    assert extract_payment_signature(retained_header) == "retained-proof"


def test_every_canonical_proof_header_reaches_all_machine_contract_consumers(
    monkeypatch,
    payment_harness,
):
    """Addition-direction guard: a new canonical header needs no consumer edits."""
    monkeypatch.setattr(stim_router_module, "get_engine", lambda: rows_engine([]))
    main.v1.openapi_schema = None

    schema = payment_harness.client.get("/v1/openapi.json").json()
    manifest = payment_harness.client.get(X402_DISCOVERY_ALIASES[0]).json()
    plugin = payment_harness.client.get("/.well-known/ai-plugin.json").json()

    openapi_headers = {
        scheme.get("name")
        for scheme in schema["components"]["securitySchemes"].values()
        if scheme.get("name") in x402_contract_module.X402_PROOF_HEADERS
    }
    assert openapi_headers == set(x402_contract_module.X402_PROOF_HEADERS)
    assert manifest["x402"]["payment"]["proof_headers"] == list(
        x402_contract_module.X402_PROOF_HEADERS
    )
    assert plugin["x_stocktrends_access"]["x402_proof_headers"] == list(
        x402_contract_module.X402_PROOF_HEADERS
    )

    for index, proof_header in enumerate(x402_contract_module.X402_PROOF_HEADERS):
        probe = Headers({proof_header: "runtime-proof"})
        assert is_x402_payment_method(probe) is True
        assert has_payment_signature(probe) is True
        assert extract_payment_signature(probe) == "runtime-proof"

        cors = payment_harness.client.options(
            "/.well-known/x402",
            headers={
                "Origin": "https://developer.stocktrends.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": proof_header,
            },
        )
        assert cors.status_code == 200
        assert proof_header.lower() in cors.headers[
            "access-control-allow-headers"
        ].lower()

        reference = f"canonical-proof-header-{index}"
        paid_headers = x402_headers(reference=reference)
        payment_proof = paid_headers.pop("X-Payment")
        paid_headers[proof_header] = payment_proof
        payment_harness.facilitator.reset()

        paid = payment_harness.client.get(
            "/v1/stim/latest?symbol_exchange=IBM-N",
            headers=paid_headers,
        )
        assert paid.status_code != 402
        assert paid.status_code in {200, 404}
        assert payment_harness.facilitator.verify_count == 1
        assert payment_harness.facilitator.settle_count == 1
        assert payment_harness.facilitator.verify_calls[0][
            "payment_signature"
        ] == payment_proof
        assert payment_harness.facilitator.settle_calls[0][
            "payment_signature"
        ] == payment_proof


def test_strict_discovery_fails_but_runtime_manifest_degrades_per_resource(
    monkeypatch,
    payment_harness,
):
    baseline = policy_provider_module.get_runtime_payment_policy_config()
    broken_policy = policy_provider_module.EndpointPaymentPolicy(
        endpoint_id="reviewer-unrepresentable",
        path_pattern="/v1/reviewer/unrepresentable",
        method="GET",
        allowed_rails=("subscription", "x402", "mpp"),
        pricing_rule_id="reviewer_missing_rule",
    )
    degraded_config = replace(
        baseline,
        endpoint_payment_policies=(
            *baseline.endpoint_payment_policies,
            broken_policy,
        ),
    )
    monkeypatch.setattr(
        policy_provider_module,
        "get_runtime_payment_policy_config",
        lambda *_args, **_kwargs: degraded_config,
    )

    with pytest.raises(X402DiscoveryCompletenessError) as exc_info:
        build_x402_discovery(strict=True)
    assert exc_info.value.error_code == "missing_endpoint_metadata"

    response = payment_harness.client.get(X402_DISCOVERY_ALIASES[0])
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["complete"] is False
    assert manifest["resources"]
    assert any(resource["path"] == "/v1/stim/latest" for resource in manifest["resources"])
    assert not any(
        resource["path"] == broken_policy.path_pattern
        for resource in manifest["resources"]
    )
    assert {
        "method": "GET",
        "path": broken_policy.path_pattern,
        "error_code": "missing_endpoint_metadata",
        "reason": "resource_contract_unrepresentable",
    } in manifest["discovery_exceptions"]
    assert payment_harness.facilitator.verify_count == 0
    assert payment_harness.facilitator.settle_count == 0
    assert payment_harness.mpp.authorize_count == 0
    assert payment_harness.logs.economics == []


def test_all_runtime_resource_failures_surface_systemic_5xx(
    monkeypatch,
    payment_harness,
):
    def fail_every_resource(_policy, _config):
        raise X402DiscoveryCompletenessError(
            "forced systemic representation failure",
            error_code="forced_systemic_failure",
        )

    monkeypatch.setattr(
        x402_discovery_module,
        "_resource_from_policy",
        fail_every_resource,
    )

    with pytest.raises(X402DiscoverySystemicFailure):
        build_x402_discovery(strict=False)

    response = payment_harness.client.get(X402_DISCOVERY_ALIASES[0])
    assert response.status_code == 503
    assert response.json() == {
        "detail": "x402 discovery is temporarily unavailable"
    }
    assert payment_harness.facilitator.verify_count == 0
    assert payment_harness.facilitator.settle_count == 0
    assert payment_harness.mpp.authorize_count == 0
    assert payment_harness.logs.economics == []


def test_genuinely_empty_policy_configuration_is_complete_and_empty(
    monkeypatch,
    payment_harness,
):
    baseline = policy_provider_module.get_runtime_payment_policy_config()
    empty = replace(baseline, endpoint_payment_policies=())
    monkeypatch.setattr(
        policy_provider_module,
        "get_runtime_payment_policy_config",
        lambda *_args, **_kwargs: empty,
    )

    response = payment_harness.client.get(X402_DISCOVERY_ALIASES[0])
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["complete"] is True
    assert manifest["resources"] == []
    assert manifest["discovery_exceptions"] == []
    assert payment_harness.facilitator.verify_count == 0
    assert payment_harness.facilitator.settle_count == 0
    assert payment_harness.logs.economics == []


def test_manifest_pricing_contract_rejects_duplicated_endpoint_prices():
    manifest = build_x402_discovery()
    for resource in manifest["resources"]:
        validate_discovery_resource_pricing_contract(resource)

    mutation = copy.deepcopy(manifest["resources"][0])
    mutation["stc_cost"] = "0.05"
    mutation["price_usd"] = "0.05"
    with pytest.raises(X402DiscoveryCompletenessError) as exc_info:
        validate_discovery_resource_pricing_contract(mutation)
    assert exc_info.value.error_code == "duplicated_endpoint_price"


def test_machine_disabled_policy_remains_discoverable_with_effective_rails(
    monkeypatch,
):
    baseline = policy_provider_module.get_runtime_payment_policy_config()
    target_path = "/v1/stim/latest"
    changed_policies = tuple(
        replace(policy, machine_payments_enabled=False)
        if policy.path_pattern == target_path and policy.method == "GET"
        else policy
        for policy in baseline.endpoint_payment_policies
    )
    changed = replace(baseline, endpoint_payment_policies=changed_policies)
    monkeypatch.setattr(
        policy_provider_module,
        "get_runtime_payment_policy_config",
        lambda *_args, **_kwargs: changed,
    )

    manifest = build_x402_discovery()
    resource = next(
        resource for resource in manifest["resources"] if resource["path"] == target_path
    )
    assert resource["supported_rails"] == ["subscription"]
    assert resource["anonymous_x402_challenge_supported"] is False
    assert "x402" not in resource
    assert _manifest_keys(manifest) == {
        (policy.method.upper(), policy.path_pattern)
        for policy in changed.endpoint_payment_policies
    }


def test_openapi_cache_tracks_semantic_runtime_policy_and_manifest(
    monkeypatch,
):
    _stub_request_logging(monkeypatch)
    baseline = policy_provider_module.get_runtime_payment_policy_config()
    target_path = "/v1/stim/latest"
    subscription_only_policies = tuple(
        replace(
            policy,
            allowed_rails=("subscription",),
            machine_payments_enabled=False,
        )
        if policy.path_pattern == target_path and policy.method == "GET"
        else policy
        for policy in baseline.endpoint_payment_policies
    )
    subscription_only = replace(
        baseline,
        version="reviewer-subscription-only",
        fetched_at=(baseline.fetched_at or 0) + 1,
        endpoint_payment_policies=subscription_only_policies,
    )
    current = {"config": baseline}
    monkeypatch.setattr(
        policy_provider_module,
        "get_runtime_payment_policy_config",
        lambda *_args, **_kwargs: current["config"],
    )

    original_get_openapi = main.get_openapi
    generations = []

    def counting_get_openapi(*args, **kwargs):
        generations.append(1)
        return original_get_openapi(*args, **kwargs)

    monkeypatch.setattr(main, "get_openapi", counting_get_openapi)
    main.v1.openapi_schema = None

    with TestClient(main.app) as client:
        full_schema = client.get("/v1/openapi.json").json()
        assert len(generations) == 1
        full_operation = full_schema["paths"]["/stim/latest"]["get"]
        assert full_operation["x-stocktrends-payment"]["supported_rails"] == [
            "subscription",
            "x402",
            "mpp",
        ]

        current["config"] = subscription_only
        restricted_schema = client.get("/v1/openapi.json").json()
        assert len(generations) == 2
        restricted_operation = restricted_schema["paths"]["/stim/latest"]["get"]
        assert restricted_operation["security"] == [
            {"ApiKeyAuth": []},
            {"BearerAuth": []},
        ]
        restricted_payment = restricted_operation["x-stocktrends-payment"]
        assert restricted_payment["supported_rails"] == ["subscription"]
        assert restricted_payment["anonymous_challenge_supported"] is False
        assert "x402_version" not in restricted_payment
        assert "x402_proof_headers" not in restricted_payment

        manifest = client.get(X402_DISCOVERY_ALIASES[0]).json()
        manifest_resource = next(
            resource
            for resource in manifest["resources"]
            if resource["path"] == target_path
        )
        assert manifest_resource["supported_rails"] == ["subscription"]
        assert manifest_resource["anonymous_x402_challenge_supported"] is False
        assert "x402" not in manifest_resource

        repeated_schema = client.get("/v1/openapi.json").json()
        assert repeated_schema == restricted_schema
        assert len(generations) == 2

        current["config"] = baseline
        reversed_schema = client.get("/v1/openapi.json").json()
        assert len(generations) == 3
        assert reversed_schema["paths"]["/stim/latest"]["get"] == full_operation


def test_openapi_contract_cache_update_is_atomic_under_concurrency(monkeypatch):
    baseline = policy_provider_module.get_runtime_payment_policy_config()
    target_path = "/v1/stim/latest"
    subscription_only = replace(
        baseline,
        version="concurrency-subscription-only",
        endpoint_payment_policies=tuple(
            replace(
                policy,
                allowed_rails=("subscription",),
                machine_payments_enabled=False,
            )
            if policy.path_pattern == target_path and policy.method == "GET"
            else policy
            for policy in baseline.endpoint_payment_policies
        ),
    )
    current = {"config": baseline}
    monkeypatch.setattr(
        policy_provider_module,
        "get_runtime_payment_policy_config",
        lambda *_args, **_kwargs: current["config"],
    )

    original_get_openapi = main.get_openapi
    old_generation_started = threading.Event()
    release_old_generation = threading.Event()
    new_request_attempted = threading.Event()
    thread_context = threading.local()
    generation_labels = []

    def controlled_get_openapi(*args, **kwargs):
        label = thread_context.label
        generation_labels.append(label)
        if label == "old":
            old_generation_started.set()
            assert release_old_generation.wait(timeout=5)
        return original_get_openapi(*args, **kwargs)

    def request_schema(label: str):
        thread_context.label = label
        if label == "new":
            new_request_attempted.set()
        return main.v1.openapi()

    monkeypatch.setattr(main, "get_openapi", controlled_get_openapi)
    main.v1.openapi_schema = None

    with ThreadPoolExecutor(max_workers=2) as executor:
        old_future = executor.submit(request_schema, "old")
        assert old_generation_started.wait(timeout=5)

        current["config"] = subscription_only
        new_future = executor.submit(request_schema, "new")
        assert new_request_attempted.wait(timeout=5)
        assert generation_labels == ["old"]

        release_old_generation.set()
        old_schema = old_future.result(timeout=5)
        new_schema = new_future.result(timeout=5)

    assert old_schema["paths"]["/stim/latest"]["get"][
        "x-stocktrends-payment"
    ]["supported_rails"] == ["subscription", "x402", "mpp"]
    assert new_schema["paths"]["/stim/latest"]["get"][
        "x-stocktrends-payment"
    ]["supported_rails"] == ["subscription"]
    assert generation_labels == ["old", "new"]

    expected_fingerprint = policy_provider_module.payment_policy_contract_fingerprint(
        subscription_only
    )
    assert main.v1.state.openapi_payment_policy_fingerprint == expected_fingerprint
    assert main.v1.state.openapi_payment_contract_key[0] == expected_fingerprint
    assert main.v1.openapi_schema is new_schema

    thread_context.label = "current"
    assert main.v1.openapi() is new_schema
    assert generation_labels == ["old", "new"]


def test_policy_fingerprint_ignores_refresh_time_but_tracks_semantics():
    baseline = policy_provider_module.get_runtime_payment_policy_config()
    refreshed = replace(baseline, fetched_at=(baseline.fetched_at or 0) + 100)
    assert policy_provider_module.payment_policy_contract_fingerprint(
        baseline
    ) == policy_provider_module.payment_policy_contract_fingerprint(refreshed)

    first = baseline.endpoint_payment_policies[0]
    changed = replace(
        baseline,
        endpoint_payment_policies=(
            replace(first, allowed_rails=("subscription",)),
            *baseline.endpoint_payment_policies[1:],
        ),
    )
    assert policy_provider_module.payment_policy_contract_fingerprint(
        baseline
    ) != policy_provider_module.payment_policy_contract_fingerprint(changed)


def _resolve_openapi_parameter(schema: dict, parameter: dict) -> dict:
    reference = parameter.get("$ref")
    if not reference:
        return parameter
    name = reference.rsplit("/", 1)[-1]
    return schema["components"]["parameters"][name]


def _minimum_openapi_value(name: str, value_schema: dict):
    candidates = value_schema.get("anyOf") or [value_schema]
    candidate = next(
        (item for item in candidates if item.get("type") != "null"),
        value_schema,
    )
    if candidate.get("enum"):
        return candidate["enum"][0]
    if "default" in candidate:
        return candidate["default"]
    if name == "symbol":
        return "IBM"
    if name == "symbol_exchange":
        return "IBM-N"
    if candidate.get("format") == "date":
        return "2025-01-03"
    if candidate.get("type") == "integer":
        return candidate.get("minimum", 1)
    if candidate.get("type") == "number":
        return candidate.get("minimum", 1)
    if candidate.get("type") == "boolean":
        return False
    return "contract-test"


def _anonymous_request_from_openapi_claim(
    client: TestClient,
    schema: dict,
    path_template: str,
    method: str,
    operation: dict,
):
    example = operation.get("x-stocktrends-safe-example-request")
    if isinstance(example, dict):
        return client.request(
            example["method"],
            example["path"],
            params=example.get("query"),
            json=example.get("json"),
        )

    external_path = f"/v1{path_template}"
    query = {}
    for raw_parameter in operation.get("parameters", []):
        parameter = _resolve_openapi_parameter(schema, raw_parameter)
        if not parameter.get("required"):
            continue
        value = _minimum_openapi_value(parameter["name"], parameter.get("schema", {}))
        if parameter.get("in") == "path":
            external_path = external_path.replace(
                "{" + parameter["name"] + "}",
                str(value),
            )
        elif parameter.get("in") == "query":
            query[parameter["name"]] = value

    if external_path == "/v1/instruments/resolve":
        query["symbol_exchange"] = "IBM-N"
    return client.request(method, external_path, params=query)


def test_every_openapi_public_claim_is_anonymously_true_at_runtime(monkeypatch):
    """The schema itself supplies claims; runtime auth is the independent oracle."""
    _stub_request_logging(monkeypatch)
    empty_engine = rows_engine([])
    for module, accessor in (
        (instruments_router_module, "get_market_engine"),
        (selections_router_module, "get_engine"),
        (stwr_router_module, "get_engine"),
        (leadership_router_module, "get_engine"),
        (pricing_router_module, "get_metering_engine"),
        (stocktrends_portfolios_router_module, "get_engine"),
        (stocktrends_strategies_router_module, "get_engine"),
        (ai_router_module, "get_engine"),
        (ai_router_module, "get_metering_engine"),
    ):
        monkeypatch.setattr(module, accessor, lambda engine=empty_engine: engine)
    workflow_engine = rows_engine(
        [
            {"rule_name": rule_id, "cost_per_request": Decimal("0.10")}
            for rule_id in workflows_router_module._collect_registry_rule_ids()
        ]
    )
    monkeypatch.setattr(
        workflows_router_module,
        "get_metering_engine",
        lambda: workflow_engine,
    )
    main.v1.openapi_schema = None

    with TestClient(main.app) as client:
        schema = client.get("/v1/openapi.json").json()
        public_claims = [
            (path, method, operation)
            for path, path_item in schema["paths"].items()
            for method, operation in path_item.items()
            if method in main.HTTP_METHODS and operation.get("security") == []
        ]
        assert public_claims

        exercised = set()
        for path, method, operation in public_claims:
            response = _anonymous_request_from_openapi_claim(
                client,
                schema,
                path,
                method,
                operation,
            )
            assert response.status_code not in {401, 403}, (
                method,
                path,
                response.status_code,
                response.text,
            )
            assert response.status_code < 500 or response.status_code == 503, (
                method,
                path,
                response.status_code,
                response.text,
            )
            exercised.add((method.upper(), path))

        assert exercised == {
            (method.upper(), path) for path, method, _operation in public_claims
        }

        protected = client.get("/v1/agents")
        observability_anonymous = client.get(
            "/v1/observability/mpp/sessions/contract-test-channel"
        )
        observability_customer_key = client.get(
            "/v1/observability/mpp/sessions/contract-test-channel",
            headers={"X-API-Key": "customer-key-does-not-grant-internal-access"},
        )
        known_public = client.get("/v1/ai/tools")

    assert protected.status_code in {401, 403}
    assert observability_anonymous.status_code == 403
    assert observability_customer_key.status_code == 403
    assert known_public.status_code == 200


def test_root_and_v1_openapi_remain_structurally_identical(monkeypatch):
    _stub_request_logging(monkeypatch)
    main.v1.openapi_schema = None
    with TestClient(main.app) as client:
        root_response = client.get("/openapi.json")
        v1_response = client.get("/v1/openapi.json")

    assert root_response.status_code == 200
    assert v1_response.status_code == 200
    assert root_response.content == v1_response.content
    assert root_response.json() == v1_response.json()
