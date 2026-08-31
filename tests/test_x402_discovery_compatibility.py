from __future__ import annotations

from collections import Counter
from decimal import Decimal

from fastapi.testclient import TestClient
from starlette.datastructures import Headers

import main
import middleware.api_key as api_key_module
import middleware.metering as metering_module
import payments.enforcement as enforcement_module
import payments.mpp_client as mpp_client_module
import routers.ai as ai_router_module
import services.intelligence_artifact_availability as availability_module
from discovery.endpoint_metadata import get_endpoint_metadata
from discovery.x402_discovery import (
    CANONICAL_DISCOVERY_URL,
    DISCOVERY_SCHEMA,
    X402_DISCOVERY_ALIASES,
    build_x402_discovery,
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
from support.payment_harness import payment_governed_routes, v1_path


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
        availability_module,
        "configured_intelligence_artifact_store",
        poison("artifact_store"),
    )

    with TestClient(main.app) as client:
        response = client.get(X402_DISCOVERY_ALIASES[0])

    assert response.status_code == 200
    assert calls == Counter()


def test_manifest_reconciles_with_runtime_payment_governed_surface():
    runtime_governed = {
        (method, v1_path(route))
        for route, method in payment_governed_routes()
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


def test_availability_classification_is_derived_from_existing_boundary():
    for resource in build_x402_discovery()["resources"]:
        availability_gated = match_paid_intelligence_artifact_route(
            resource["method"], resource["path"]
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
    assert schemes["X402PaymentSignature"]["name"] == "PAYMENT-SIGNATURE"
    assert schemes["X402LegacyPayment"]["name"] == "X-Payment"

    for scheme_name in ("X402PaymentSignature", "X402LegacyPayment"):
        proof_header = schemes[scheme_name]["name"]
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
                    assert {"X402PaymentSignature": []} in operation["security"]
                    assert {"X402LegacyPayment": []} in operation["security"]
                    assert payment["anonymous_challenge_supported"] is True
                    assert payment["x402_version"] == 2

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
