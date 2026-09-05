"""
PR2 — x402 discovery compatibility contract.

Two properties are under test.

A. x402 ``ResourceInfo`` tags are endpoint-aware, and they come from the
   canonical endpoint registry rather than from a second table living inside
   payment code.  The tag budget an indexer reads per resource is small, so
   spending all of it on one service-level set makes every Stock Trends
   capability look identical to a machine consumer.

B. The discovery metadata a standards-aware consumer is actually handed is
   enough to construct a valid unpaid request, and that request reaches the
   documented pre-payment outcome.

Where the probe reads its request from, and why
-----------------------------------------------
Property B is exercised through the EMITTED Bazaar/x402 representation, not
through ``safe_example_request``.  Reading the registry field directly would
prove the registry agrees with itself; it would not prove that what a crawler
receives is serviceable.  Every field the probe sends — method, path, query or
body — is recovered from ``build_x402_requirements(...)`` output, in both the
full and the compact challenge shape, and the two must agree before either is
executed.

The only seed is the concrete path, taken from the public
``/.well-known/x402`` manifest.  That is the emitted representation a crawler
reads first, and it is the only way to learn a concrete path for a templated
route such as ``/v1/intelligence/{family}/{artifact_id}``.

What this suite does NOT do
---------------------------
It does not re-prove the settlement-ordering invariant.  ``tests/
test_settlement_ordering.py`` already owns that, and its harness is reused here
rather than copied.  The negative coverage below is scoped to one question this
PR introduces: an advertised example that runtime validation would reject must
be visible as a test failure, and mutating one must not quietly still pass.
"""
from __future__ import annotations

import ast
import copy
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import discovery.endpoint_metadata as endpoint_metadata_module
import payments.x402 as x402_module
import payments.x402_contract as x402_contract_module
from discovery.endpoint_metadata import (
    SERVICE_TAGS,
    X402_DOMAIN_ANCHOR_TAGS,
    X402_RESOURCE_TAG_LIMIT,
    get_endpoint_metadata,
    get_x402_resource_tags,
)
from discovery.x402_discovery import build_x402_discovery
from payments.policy_provider import get_runtime_payment_policy_config
from payments.x402 import (
    X402_CHALLENGE_MODE_COMPACT,
    X402_CHALLENGE_MODE_FULL,
    build_x402_requirements,
)
from services.intelligence_artifact_availability import (
    match_paid_intelligence_artifact_route,
)
from services.intelligence_artifact_store import STORE_ENV_VAR
from support.payment_harness import payment_governed_routes

# Any positive amount works: this suite asserts nothing about price, and the
# pricing engine is not consulted to build a requirements object.
_PROBE_AMOUNT = Decimal("0.01")

# The two pre-payment availability outcomes a paid Intelligence Artifact route
# may legitimately produce instead of a challenge, and the error codes that
# prove the availability gate — rather than an invalid advertised example — is
# what answered.
_AVAILABILITY_GATE_STATUSES = {404, 503}
_AVAILABILITY_GATE_ERROR_CODES = {
    "intelligence_artifact_not_found",
    "intelligence_artifact_store_unavailable",
}


# ---------------------------------------------------------------------------
# Recovering a request from emitted discovery metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdvertisedRequest:
    """A request rebuilt entirely from what discovery emitted."""

    policy_path: str
    method: str
    path: str
    query: dict[str, Any] | None
    body: dict[str, Any] | None

    @property
    def key(self) -> tuple[str, str]:
        return self.method, self.policy_path


def _strip_base_url(url: str) -> str:
    base = x402_module.X402_API_BASE_URL
    if base and url.startswith(base):
        return url[len(base):]
    return url


def _manifest_seeds() -> list[tuple[str, str, str, str]]:
    """(method, policy_path, seed_path, availability_classification) per resource.

    Read from the served discovery manifest, which is the representation a
    crawler consumes, and built strictly so an unrepresentable resource fails
    here rather than silently shrinking the probed surface.
    """
    seeds = []
    for resource in build_x402_discovery(strict=True)["resources"]:
        example = resource["safe_example_request"]
        seeds.append(
            (
                resource["method"],
                resource["path"],
                example["path"],
                resource["availability"]["classification"],
            )
        )
    assert seeds, "discovery published no resources to probe"
    return seeds


def _advertised_request(method: str, policy_path: str, seed_path: str) -> AdvertisedRequest:
    """Recover an executable request from the emitted x402 challenge metadata.

    Both challenge shapes are decoded, and they must advertise the same method,
    the same concrete path, and the same input payload.  A compact challenge
    that degrades its example while the full challenge keeps one would hand two
    consumers two different requests, and only one of them would work.
    """
    full = build_x402_requirements(
        path=seed_path,
        amount_usd=_PROBE_AMOUNT,
        method=method,
        challenge_mode=X402_CHALLENGE_MODE_FULL,
    )
    compact = build_x402_requirements(
        path=seed_path,
        amount_usd=_PROBE_AMOUNT,
        method=method,
        challenge_mode=X402_CHALLENGE_MODE_COMPACT,
    )

    full_input = full["extensions"]["bazaar"]["info"]["input"]
    compact_input = compact["extensions"]["bazaar"]["info"]["input"]

    advertised_method = full_input["method"]
    assert advertised_method == compact_input["method"], (
        f"{policy_path}: full and compact challenges advertise different methods"
    )
    assert advertised_method == method, (
        f"{policy_path}: emitted Bazaar input method {advertised_method!r} does not "
        f"match the runtime payment-policy method {method!r}"
    )

    example = full_input["example"]
    advertised_path = example["path"]
    assert "{" not in advertised_path and "}" not in advertised_path, (
        f"{policy_path}: advertised example path {advertised_path!r} still carries an "
        "unresolved path placeholder, so no consumer can execute it"
    )
    assert advertised_path == _strip_base_url(full["resource"]["url"]), (
        f"{policy_path}: emitted example path disagrees with the emitted ResourceInfo url"
    )
    assert _strip_base_url(full["resource"]["url"]) == _strip_base_url(
        compact["resource"]["url"]
    ), f"{policy_path}: full and compact challenges name different resource urls"

    if advertised_method in {"GET", "HEAD", "DELETE"}:
        query = example.get("query")
        assert isinstance(query, dict), f"{policy_path}: no advertised query example"
        assert query == compact_input.get("queryParams"), (
            f"{policy_path}: compact challenge advertises different query input than the "
            "full challenge"
        )
        return AdvertisedRequest(policy_path, advertised_method, advertised_path, query, None)

    body = example.get("json")
    assert isinstance(body, dict), f"{policy_path}: no advertised body example"
    assert compact_input.get("bodyType") == "json"
    assert body == compact_input.get("body"), (
        f"{policy_path}: compact challenge advertises different body input than the "
        "full challenge"
    )
    return AdvertisedRequest(policy_path, advertised_method, advertised_path, None, body)


def _advertised_requests() -> list[tuple[AdvertisedRequest, str]]:
    return [
        (_advertised_request(method, policy_path, seed_path), classification)
        for method, policy_path, seed_path, classification in _manifest_seeds()
    ]


def _send(harness, request: AdvertisedRequest, *, query=None, body=None):
    """Send an advertised request unpaid, with optional deliberate mutation."""
    return harness.client.request(
        request.method,
        request.path,
        params=query if query is not None else request.query,
        json=body if body is not None else request.body,
    )


def _assert_no_payment_activity(harness) -> None:
    assert harness.facilitator.verify_count == 0
    assert harness.facilitator.settle_count == 0
    assert harness.mpp.authorize_count == 0
    assert harness.mpp.capture_count == 0
    assert harness.mpp.void_count == 0


def _probe_reaches_challenge(harness, request: AdvertisedRequest) -> None:
    """The Part B contract for one request-probeable resource.

    Factored out so the mutation meta-test below can call the same code with a
    deliberately broken advertised example and observe it fail.
    """
    response = _send(harness, request)
    assert response.status_code == 402, (
        request.method,
        request.policy_path,
        response.status_code,
        response.text,
    )
    _assert_no_payment_activity(harness)


# ---------------------------------------------------------------------------
# Part A / D — endpoint-aware ResourceInfo tags
# ---------------------------------------------------------------------------

def _emitted_tags(method: str, path: str) -> list[str]:
    return build_x402_requirements(
        path=path,
        amount_usd=_PROBE_AMOUNT,
        method=method,
    )["resource"]["tags"]


def _governed_policies():
    config = get_runtime_payment_policy_config()
    return sorted(
        config.endpoint_payment_policies,
        key=lambda item: (item.path_pattern, item.method),
    )


def test_every_governed_resource_emits_endpoint_aware_tags():
    for policy in _governed_policies():
        method = policy.method.upper()
        path = policy.path_pattern
        tags = _emitted_tags(method, path)

        assert tags, f"{path}: emitted no ResourceInfo tags"
        assert all(isinstance(tag, str) and tag for tag in tags), f"{path}: {tags}"
        assert len(set(tags)) == len(tags), f"{path}: duplicate tags {tags}"
        assert len(tags) <= X402_RESOURCE_TAG_LIMIT, (
            f"{path}: {len(tags)} tags exceeds the x402 ResourceInfo budget of "
            f"{X402_RESOURCE_TAG_LIMIT}: {tags}"
        )
        assert tags[: len(X402_DOMAIN_ANCHOR_TAGS)] == list(X402_DOMAIN_ANCHOR_TAGS), (
            f"{path}: expected the stable domain anchors first, got {tags}"
        )
        assert len(tags) > len(X402_DOMAIN_ANCHOR_TAGS), (
            f"{path}: carries only generic anchors and discriminates nothing"
        )
        assert tags != list(SERVICE_TAGS), (
            f"{path}: fell back to the service-level tag set, which means the resource "
            "has no canonical endpoint metadata behind it"
        )


def test_resource_tags_come_from_canonical_endpoint_metadata():
    """The emitted tail must be the registry's own tags for that endpoint."""
    for policy in _governed_policies():
        method = policy.method.upper()
        path = policy.path_pattern
        metadata = get_endpoint_metadata(path, method)
        assert metadata is not None, f"{path}: payment-governed but has no metadata"

        expected: list[str] = []
        for tag in (*X402_DOMAIN_ANCHOR_TAGS, *metadata["tags"]):
            if tag not in expected:
                expected.append(tag)

        assert len(expected) <= X402_RESOURCE_TAG_LIMIT, (
            f"{path}: declares {len(expected)} tags, so the accessor would silently "
            f"truncate to {X402_RESOURCE_TAG_LIMIT}: {expected}"
        )
        assert _emitted_tags(method, path) == expected


def test_resource_tags_are_deterministic_and_mode_independent():
    for policy in _governed_policies():
        method = policy.method.upper()
        path = policy.path_pattern
        first = _emitted_tags(method, path)
        assert first == _emitted_tags(method, path)
        for mode in (X402_CHALLENGE_MODE_FULL, X402_CHALLENGE_MODE_COMPACT):
            assert first == build_x402_requirements(
                path=path,
                amount_usd=_PROBE_AMOUNT,
                method=method,
                challenge_mode=mode,
            )["resource"]["tags"]


def _tag_sets_by_resource() -> dict[tuple[str, str], tuple[str, ...]]:
    return {
        (policy.method.upper(), policy.path_pattern): tuple(
            _emitted_tags(policy.method.upper(), policy.path_pattern)
        )
        for policy in _governed_policies()
    }


def _resources_sharing_a_tag_set() -> dict[tuple[str, ...], list[tuple[str, str]]]:
    shared: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    for key, tags in _tag_sets_by_resource().items():
        shared.setdefault(tags, []).append(key)
    return {tags: keys for tags, keys in shared.items() if len(keys) > 1}


def test_generic_anchors_do_not_erase_endpoint_discrimination():
    """Every payable resource must be tag-distinguishable from every other one."""
    collisions = _resources_sharing_a_tag_set()
    assert not collisions, (
        "resources share an identical tag set, so a discovery indexer cannot tell "
        f"them apart: {sorted((list(tags), sorted(keys)) for tags, keys in collisions.items())}"
    )
    assert len(_tag_sets_by_resource()) == len(_governed_policies())


def test_uniform_tags_are_detectable(monkeypatch):
    """Reverting to one service-wide tag set must break the discrimination test.

    Without this, the test above could pass for the wrong reason — for example
    if it silently enumerated nothing.
    """
    uniform = ["market-intelligence", "agentic"]
    for entry in endpoint_metadata_module._ENDPOINT_METADATA_BY_PATH.values():
        monkeypatch.setitem(entry, "tags", list(uniform))

    collisions = _resources_sharing_a_tag_set()
    assert collisions, "a uniform tag set went undetected by the collision check"


def test_over_budget_tags_never_reach_a_resource(monkeypatch):
    """Six declared tags must not produce six emitted tags."""
    path = "/v1/market/regime/latest"
    entry = endpoint_metadata_module._ENDPOINT_METADATA_BY_PATH[path]
    monkeypatch.setitem(entry, "tags", [f"probe-{index}" for index in range(6)])
    assert len(get_x402_resource_tags(path, "GET")) == X402_RESOURCE_TAG_LIMIT
    assert len(_emitted_tags("GET", path)) == X402_RESOURCE_TAG_LIMIT


def test_payment_code_defines_no_endpoint_semantics_of_its_own():
    """payments/x402.py may consume the tag accessor; it may not be a registry.

    A second path->tags table inside payment code is the drift this asserts
    against, so the check is for endpoint paths appearing as literals at all.
    """
    source = Path(x402_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    path_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/v1/")
    ]
    assert not path_literals, (
        f"payments/x402.py names endpoint paths directly: {sorted(set(path_literals))}"
    )

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "get_x402_resource_tags" in called, (
        "payments/x402.py no longer resolves resource tags through the canonical "
        "endpoint-metadata accessor"
    )
    assert "SERVICE_TAGS" not in source, (
        "payments/x402.py still reaches for the service-level tag set for resource tags"
    )


def test_baseline_capability_semantics_are_advertised():
    """Named Stock Trends capabilities must be lexically present where they belong.

    These are the discovery needs the tag taxonomy exists to serve.  They are
    asserted as substring presence across a resource's tags so a later rewording
    within the same concept does not fail the test, while dropping the concept
    does.
    """
    expectations = {
        ("GET", "/v1/prices/history"): ["price-history", "stock-market-data"],
        ("GET", "/v1/prices/latest"): ["stock-market-data", "market-price"],
        ("GET", "/v1/indicators/latest"): ["technical-analysis", "trend"],
        ("GET", "/v1/indicators/history"): ["technical-analysis", "trend"],
        ("GET", "/v1/market/regime/latest"): ["market-regime"],
        ("GET", "/v1/breadth/sector/latest"): ["market-breadth", "sector-breadth"],
        ("GET", "/v1/breadth/sector/history"): ["market-breadth", "sector-breadth"],
        ("GET", "/v1/leadership/summary/latest"): ["market-leadership"],
        ("GET", "/v1/leadership/rotation/history"): ["sector-rotation"],
        ("GET", "/v1/stim/latest"): ["probabilistic-returns", "forward-returns"],
        ("GET", "/v1/stim/history"): ["probabilistic-returns", "forward-returns"],
        ("GET", "/v1/selections/latest"): ["stock-selection"],
        ("GET", "/v1/selections/published/latest"): ["stim-select"],
        ("GET", "/v1/agent/screener/top"): ["stock-screening", "technical-analysis"],
        ("POST", "/v1/decision/evaluate-symbol"): ["stock-evaluation", "stock-analysis"],
        ("POST", "/v1/portfolio/evaluate"): ["portfolio-evaluation"],
        ("POST", "/v1/portfolio/construct"): ["portfolio-construction"],
    }
    for (method, path), required in expectations.items():
        tags = _emitted_tags(method, path)
        joined = " ".join(tags)
        for concept in required:
            assert concept in joined, f"{method} {path}: {concept!r} missing from {tags}"


def test_probabilistic_return_semantics_favour_inference_over_breadth():
    """A probabilistic-returns need must not resolve to a breadth resource."""
    inference = set(_emitted_tags("GET", "/v1/stim/latest"))
    breadth = set(_emitted_tags("GET", "/v1/breadth/sector/history"))
    assert {"probabilistic-returns", "forward-returns"} <= inference
    assert not (inference & breadth) - set(X402_DOMAIN_ANCHOR_TAGS)


# ---------------------------------------------------------------------------
# Part D — pricing / policy / rail non-drift
# ---------------------------------------------------------------------------

def test_metadata_change_did_not_alter_the_payment_contract():
    """Discovery metadata carries no price and selects no rail."""
    policies = _governed_policies()
    resources = build_x402_discovery(strict=True)["resources"]
    assert len(resources) == len(policies)

    by_key = {(resource["method"], resource["path"]): resource for resource in resources}
    for policy in policies:
        resource = by_key[(policy.method.upper(), policy.path_pattern)]
        assert resource["pricing_rule_id"] == policy.pricing_rule_id
        assert resource["supported_rails"] == list(policy.allowed_rails)
        assert resource["pricing"]["live_cost_included"] is False

        requirements = build_x402_requirements(
            path=policy.path_pattern,
            amount_usd=_PROBE_AMOUNT,
            method=policy.method,
        )
        accepts = requirements["accepts"][0]
        assert accepts["scheme"] == x402_contract_module.X402_DEFAULT_SCHEME
        assert accepts["network"] == x402_contract_module.X402_DEFAULT_NETWORK
        assert accepts["asset"] == x402_contract_module.X402_DEFAULT_TOKEN
        assert accepts["payTo"] == x402_contract_module.X402_SELLER_ADDRESS
        # Tags are metadata. They must not leak into what is charged.
        assert accepts["amount"] == "10000"


# ---------------------------------------------------------------------------
# Part B — advertised-example probeability
# ---------------------------------------------------------------------------

def test_advertised_examples_are_classified_by_the_runtime_availability_boundary():
    """The exception list is derived, not hand-maintained."""
    gated = set()
    for request, classification in _advertised_requests():
        availability_gated = (
            match_paid_intelligence_artifact_route(request.method, request.path) is not None
        )
        expected = (
            "pre_payment_availability_gated"
            if availability_gated
            else "immediately_discoverable"
        )
        assert classification == expected, request.policy_path
        if availability_gated:
            gated.add(request.key)

    # The complete, exact exception set: paid Intelligence Artifact routes only.
    assert gated == {
        ("GET", "/v1/intelligence/guidance/latest"),
        ("GET", "/v1/intelligence/guidance/{artifact_id}"),
        ("GET", "/v1/intelligence/research/latest"),
        ("GET", "/v1/intelligence/research/{artifact_id}"),
    }


def test_request_probeable_resources_reach_402_from_emitted_metadata(payment_harness):
    probeable = [
        request
        for request, classification in _advertised_requests()
        if classification == "immediately_discoverable"
    ]
    assert probeable

    for request in probeable:
        _probe_reaches_challenge(payment_harness, request)

    # A GET-only probe would silently skip the body-input contract.
    assert any(request.method == "POST" for request in probeable), (
        "no POST resource was probed, so body-style advertised input is unproven"
    )
    assert {request.key for request in probeable} == {
        (policy.method.upper(), policy.path_pattern)
        for policy in _governed_policies()
    } - {
        ("GET", "/v1/intelligence/guidance/latest"),
        ("GET", "/v1/intelligence/guidance/{artifact_id}"),
        ("GET", "/v1/intelligence/research/latest"),
        ("GET", "/v1/intelligence/research/{artifact_id}"),
    }


def test_availability_gated_resources_reach_their_gate_not_an_input_error(payment_harness):
    """A gated example may be answered by availability — never by invalidity."""
    gated = [
        request
        for request, classification in _advertised_requests()
        if classification == "pre_payment_availability_gated"
    ]
    assert gated

    for request in gated:
        response = _send(payment_harness, request)
        assert response.status_code in {402} | _AVAILABILITY_GATE_STATUSES, (
            request.policy_path,
            response.status_code,
            response.text,
        )
        if response.status_code != 402:
            detail = response.json()["detail"]
            assert detail["error"] in _AVAILABILITY_GATE_ERROR_CODES, (
                f"{request.policy_path}: pre-payment answer {response.status_code} was not "
                f"the availability gate: {detail}"
            )
        _assert_no_payment_activity(payment_harness)


def test_a_broken_advertised_example_fails_the_probe(payment_harness, monkeypatch):
    """Mutating the advertised example to invalid input must be caught.

    This is the meta-test for the contract above: it proves the probe reads the
    example that was emitted, rather than passing regardless of its content.
    """
    path = "/v1/indicators/latest"
    entry = endpoint_metadata_module._ENDPOINT_METADATA_BY_PATH[path]
    monkeypatch.setitem(
        entry,
        "safe_example_request",
        {
            "method": "GET",
            "path": path,
            # A bare ticker with no exchange suffix is exactly what pre-payment
            # semantic validation rejects.
            "query": {"symbol_exchange": "IBM"},
        },
    )

    mutated = _advertised_request("GET", path, path)
    assert mutated.query == {"symbol_exchange": "IBM"}, (
        "the probe did not read the mutated example out of emitted metadata"
    )
    with pytest.raises(AssertionError):
        _probe_reaches_challenge(payment_harness, mutated)
    _assert_no_payment_activity(payment_harness)


def test_an_unresolved_path_placeholder_fails_the_probe(monkeypatch):
    """A templated advertised path is not adequate proof of anything."""
    path = "/v1/intelligence/guidance/{artifact_id}"
    entry = endpoint_metadata_module._ENDPOINT_METADATA_BY_PATH[path]
    monkeypatch.setitem(
        entry,
        "safe_example_request",
        {"method": "GET", "path": path, "query": {}},
    )
    with pytest.raises(AssertionError, match="unresolved path placeholder"):
        _advertised_request("GET", path, path)


_FIXTURE_ARTIFACT_STORE = (
    Path(__file__).resolve().parent / "fixtures" / "intelligence" / "public_artifacts" / "v1"
)


def _fixture_artifact_ids() -> dict[str, str]:
    manifest = json.loads((_FIXTURE_ARTIFACT_STORE / "manifest.json").read_text(encoding="utf-8"))
    return {
        entry["artifact_type"]: entry["artifact_id"]
        for entry in manifest["artifacts"]
    }


def test_availability_gated_examples_are_serviceable_requests(payment_harness, monkeypatch):
    """The gated exception must be availability, never an unserviceable example.

    The suite's default environment has no artifact store, so every gated
    example is answered 503 before FastAPI ever parses it — which would let a
    malformed example hide behind the gate.  Pointing at the fixture store opens
    the gate and makes the request itself answerable:

      * the two ``latest`` examples become fully serviceable and reach 402;
      * the two ``{artifact_id}`` examples advertise a documented placeholder id
        that no store holds, so they reach the artifact-not-found gate — and the
        same advertised route with a real stored id reaches 402, which is what
        proves the advertised path shape, not the request, is what differs.
    """
    monkeypatch.setenv(STORE_ENV_VAR, str(_FIXTURE_ARTIFACT_STORE))
    stored_ids = _fixture_artifact_ids()
    assert stored_ids, "the intelligence fixture store published no artifacts"

    gated = [
        request
        for request, classification in _advertised_requests()
        if classification == "pre_payment_availability_gated"
    ]
    assert gated

    reached_challenge = 0
    reached_not_found = 0
    for request in gated:
        target = match_paid_intelligence_artifact_route(request.method, request.path)
        assert target is not None
        response = _send(payment_harness, request)

        if target.artifact_id is None:
            assert response.status_code == 402, (
                request.policy_path,
                response.status_code,
                response.text,
            )
            reached_challenge += 1
        else:
            assert response.status_code == 404, (
                request.policy_path,
                response.status_code,
                response.text,
            )
            assert response.json()["detail"]["error"] == "intelligence_artifact_not_found"
            reached_not_found += 1

            stored_path = request.path.rsplit("/", 1)[0] + "/" + stored_ids[target.artifact_type]
            stored_response = payment_harness.client.get(stored_path)
            assert stored_response.status_code == 402, (
                f"{request.policy_path}: the advertised route is not serviceable even with a "
                f"stored artifact ({stored_response.status_code}) — the example is malformed, "
                "not merely unavailable"
            )

        _assert_no_payment_activity(payment_harness)

    assert reached_challenge == 2
    assert reached_not_found == 2


# ---------------------------------------------------------------------------
# Part C — the advertised surface never weakens pre-payment safety
# ---------------------------------------------------------------------------

def _required_request_inputs(request: AdvertisedRequest) -> list[str]:
    """Required inputs the advertised example supplies in query or body.

    Path-located requirements are excluded on purpose: removing one changes the
    route rather than the request, which is a route-miss question and is already
    owned by the settlement-ordering suite.
    """
    metadata = get_endpoint_metadata(request.policy_path, request.method) or {}
    supplied = request.query if request.query is not None else (request.body or {})
    return [
        name
        for name, spec in (metadata.get("required_inputs") or {}).items()
        if (spec.get("parameter_source") or spec.get("input_location")) != "path"
        and name in supplied
    ]


def test_stripping_a_required_input_never_reaches_the_payment_gate(payment_harness):
    """Advertising valid examples must not have made invalid ones payable."""
    exercised = 0
    for request, _classification in _advertised_requests():
        required = _required_request_inputs(request)
        if not required:
            continue
        supplied = request.query if request.query is not None else request.body
        stripped = {
            name: value for name, value in supplied.items() if name not in required
        }
        if request.query is not None:
            response = _send(payment_harness, request, query=stripped)
        else:
            response = _send(payment_harness, request, body=stripped)

        assert response.status_code in {400, 422}, (
            request.method,
            request.policy_path,
            response.status_code,
            response.text,
        )
        _assert_no_payment_activity(payment_harness)
        exercised += 1

    assert exercised >= 10, (
        f"only {exercised} resources were probed for required-input rejection; the "
        "registry no longer declares the required inputs this relies on"
    )


@pytest.mark.parametrize(
    ("method", "path", "mutation"),
    [
        # Symbol identity present but semantically incomplete.
        ("GET", "/v1/indicators/latest", {"symbol_exchange": "IBM"}),
        ("GET", "/v1/stim/latest", {"symbol_exchange": "IBM"}),
        # Malformed value FastAPI itself rejects.
        ("GET", "/v1/prices/history", {"symbol_exchange": "IBM-N", "limit": "not-a-number"}),
        # Value outside the registered enum domain.
        ("GET", "/v1/stwr/reports/latest", {"rpt": "bullcross", "exchange": "ZZ"}),
    ],
)
def test_malformed_advertised_values_still_fail_before_payment(
    payment_harness, method, path, mutation
):
    request = _advertised_request(method, path, path)
    response = _send(payment_harness, request, query=mutation)
    assert response.status_code in {400, 422}, (path, response.status_code, response.text)
    _assert_no_payment_activity(payment_harness)


def test_a_malformed_body_still_fails_before_payment(payment_harness):
    request = _advertised_request("POST", "/v1/decision/evaluate-symbol", "/v1/decision/evaluate-symbol")
    broken = copy.deepcopy(request.body or {})
    broken["symbol_exchange"] = "IBM"
    response = _send(payment_harness, request, body=broken)
    assert response.status_code in {400, 422}, (response.status_code, response.text)
    _assert_no_payment_activity(payment_harness)


@pytest.fixture
def data_access_touches(monkeypatch) -> list[str]:
    """Record any data or artifact-store access made by a governed route.

    Scoped to this suite's question rather than re-proving purity generally:
    the settlement-ordering and semantic-boundary suites already own that for
    rejections.  What is new here is the *valid advertised example*, which must
    reach the payment gate without the endpoint having run.
    """
    import importlib

    touches: list[str] = []
    modules = {
        importlib.import_module(route.endpoint.__module__)
        for route, _method in payment_governed_routes()
    }
    poisoned = 0
    for module in modules:
        for name in ("get_engine", "configured_intelligence_artifact_store"):
            if not hasattr(module, name):
                continue
            label = f"{module.__name__}.{name}"

            def sentinel(*_args, _label=label, **_kwargs):
                touches.append(_label)
                raise AssertionError(
                    f"{_label} was called while the request was still unpaid"
                )

            monkeypatch.setattr(module, name, sentinel)
            poisoned += 1

    assert poisoned > 10, f"only {poisoned} data entry points poisoned; guard is vacuous"
    return touches


def test_advertised_examples_never_execute_the_paid_endpoint(
    payment_harness, data_access_touches
):
    """A 402 must mean the gate answered, not that the endpoint ran for free."""
    probeable = [
        request
        for request, classification in _advertised_requests()
        if classification == "immediately_discoverable"
    ]
    assert probeable

    for request in probeable:
        _probe_reaches_challenge(payment_harness, request)

    assert not data_access_touches
