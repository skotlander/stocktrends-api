"""
PR2 — x402 discovery compatibility contract.

Two properties are under test.

A. x402 ``ResourceInfo`` tags are endpoint-aware and the canonical endpoint
   registry is their authority.  The tag budget an indexer reads per resource is
   small, so spending all of it on one service-level set makes every Stock
   Trends capability look identical to a machine consumer.

B. The discovery metadata a standards-aware consumer is actually handed over
   HTTP is enough to construct a request that runtime accepts.

Where the metadata comes from, and why
--------------------------------------
Property B is exercised against the **actual HTTP 402 response** produced by the
running application, not against ``build_x402_requirements`` called directly.
Calling the builder and then separately issuing a request proves the builder
agrees with itself; it does not prove that runtime enforcement returns that same
metadata to a caller.  So for every governed resource this suite:

  1. makes one seeded unpaid request to obtain a challenge — the manifest served
     at ``/.well-known/x402`` supplies the seed, and nothing else;
  2. asserts HTTP 402 and decodes the real ``PAYMENT-REQUIRED`` header;
  3. checks the response body's canonical ``payment_required`` block against
     that decoded header;
  4. rebuilds method, materialized path and query/body **from the decoded HTTP
     metadata alone**;
  5. replays that rebuilt request and requires 402 with no facilitator call, no
     MPP control-plane call and no paid data access;
  6. repeats the whole cycle in both ``X-StockTrends-Challenge-Mode`` modes and
     requires the two to agree on callable request semantics.

Builder-level tests still exist in ``tests/test_x402_requirements.py``.  They are
the lower-level contract; this file is the end-to-end one.

What this suite does NOT assert
-------------------------------
It does not freeze the status an *unpaid* invalid request receives.  The durable
economic invariant is that a deterministically invalid **payment-bearing**
request must not reach verification, settlement, MPP authorization or capture,
or paid execution — so that is what the negative tests below present.  Whether
an unpaid invalid request is answered before or with a challenge is a separate
question, and this file must not pin it.

It also does not re-prove settlement ordering generally; ``tests/
test_settlement_ordering.py`` owns that, and its harness is reused here rather
than copied.
"""
from __future__ import annotations

import base64
import copy
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import discovery.endpoint_metadata as endpoint_metadata_module
import payments.x402 as x402_module
from discovery.endpoint_metadata import (
    SERVICE_NAME,
    SERVICE_TAGS,
    X402_DOMAIN_ANCHOR_TAGS,
    X402_RESOURCE_TAG_LIMIT,
    get_endpoint_metadata,
)
from discovery.x402_discovery import CANONICAL_DISCOVERY_PATH
from payments.policy_provider import get_runtime_payment_policy_config
from payments.x402 import (
    X402_CHALLENGE_MODE_COMPACT,
    X402_CHALLENGE_MODE_FULL,
    X402_CHALLENGE_MODE_HEADER,
)
from services.intelligence_artifact_availability import (
    match_paid_intelligence_artifact_route,
)
from services.intelligence_artifact_store import STORE_ENV_VAR
from support.payment_harness import (
    UNIT_PRICE_ATOMIC,
    mpp_headers,
    payment_governed_routes,
    unpaid_headers,
    x402_headers,
)

_CHALLENGE_MODES = (X402_CHALLENGE_MODE_COMPACT, X402_CHALLENGE_MODE_FULL)
_QUERY_METHODS = {"GET", "HEAD", "DELETE"}

# x402 ResourceInfo tag format limits.  The values in the registry already
# satisfy these; the contract exists so a later addition cannot quietly emit a
# tag an indexer would truncate, reject or fail to round-trip.
_TAG_MAX_LENGTH = 32

# Pre-payment availability outcomes a paid Intelligence Artifact route may
# legitimately produce instead of a challenge, and the error codes that prove
# the availability gate — rather than an unserviceable example — answered.
_AVAILABILITY_GATE_ERROR_CODES = {
    "intelligence_artifact_not_found",
    "intelligence_artifact_store_unavailable",
}

_AVAILABILITY_GATED_RESOURCES = {
    ("GET", "/v1/intelligence/guidance/latest"),
    ("GET", "/v1/intelligence/guidance/{artifact_id}"),
    ("GET", "/v1/intelligence/research/latest"),
    ("GET", "/v1/intelligence/research/{artifact_id}"),
}

_FIXTURE_ARTIFACT_STORE = (
    Path(__file__).resolve().parent / "fixtures" / "intelligence" / "public_artifacts" / "v1"
)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Seed:
    """The minimum needed to obtain one initial valid unpaid challenge."""

    method: str
    policy_path: str
    path: str
    query: dict[str, Any] | None
    body: dict[str, Any] | None
    classification: str

    @property
    def key(self) -> tuple[str, str]:
        return self.method, self.policy_path


@dataclass(frozen=True)
class AdvertisedRequest:
    """A request rebuilt from decoded HTTP challenge metadata alone.

    ``path`` is what the challenge tells a consumer to call; ``resource_path``
    is the ResourceInfo identity of the resource just challenged.  For every
    endpoint whose canonical example path is the endpoint path itself the two
    coincide.  They diverge only on the paid Intelligence artifact-by-id routes,
    where ResourceInfo names the concrete artifact that was requested while the
    full challenge's example advertises the documented placeholder id that shows
    the id format — both truthful, and asserted separately below rather than
    forced to be equal.
    """

    policy_path: str
    method: str
    path: str
    resource_path: str
    query: dict[str, Any] | None
    body: dict[str, Any] | None
    body_type: str | None

    @property
    def callable_semantics(self) -> tuple[Any, ...]:
        """The input semantics compact and full challenges must agree on."""
        return (self.method, self.query, self.body, self.body_type)

    @property
    def route_family(self) -> str:
        return self.path.rsplit("/", 1)[0]


@dataclass(frozen=True)
class RuntimeChallenge:
    """One decoded HTTP 402 and the request rebuilt from it."""

    mode: str
    status_code: int
    requirements: dict[str, Any]
    body: dict[str, Any]
    pricing_rule_header: str | None
    request: AdvertisedRequest

    @property
    def resource(self) -> dict[str, Any]:
        return self.requirements["resource"]

    @property
    def tags(self) -> list[str]:
        return self.resource["tags"]


@dataclass(frozen=True)
class ChallengePair:
    seed: Seed
    compact: RuntimeChallenge
    full: RuntimeChallenge

    @property
    def key(self) -> tuple[str, str]:
        return self.seed.key

    def by_mode(self, mode: str) -> RuntimeChallenge:
        return self.compact if mode == X402_CHALLENGE_MODE_COMPACT else self.full


# ---------------------------------------------------------------------------
# Obtaining and decoding a real HTTP challenge
# ---------------------------------------------------------------------------

def _strip_base_url(url: str) -> str:
    base = x402_module.X402_API_BASE_URL
    if base and url.startswith(base):
        return url[len(base):]
    return url


def _decode_payment_required(response) -> dict[str, Any]:
    header = response.headers.get("payment-required")
    assert header, "the 402 carried no PAYMENT-REQUIRED header to decode"
    decoded = json.loads(base64.b64decode(header).decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def _assert_mode_fidelity(requirements: dict[str, Any], mode: str, label: str) -> None:
    """The response must be in the challenge mode the request asked for.

    Compact and full differ structurally, so this reads the shape rather than
    trusting an echoed mode string: a runtime that ignored the header and served
    the other representation would otherwise go unnoticed.
    """
    info = requirements["extensions"]["bazaar"]["info"]
    input_info = info["input"]
    if mode == X402_CHALLENGE_MODE_COMPACT:
        assert "service_name" not in info, f"{label}: compact was asked for, full was served"
        assert "example" not in input_info, f"{label}: compact input carries a full example"
        assert "metadataUrl" in info, f"{label}: compact info is missing its own markers"
    else:
        assert info.get("service_name") == SERVICE_NAME, (
            f"{label}: full was asked for, compact was served"
        )
        assert "example" in input_info, f"{label}: full input carries no example"
        assert "parameters" in input_info, f"{label}: full input carries no parameter list"


def _reconstruct(requirements: dict[str, Any], mode: str, policy_path: str) -> AdvertisedRequest:
    """Rebuild an executable request from decoded challenge metadata only."""
    resource = requirements["resource"]
    resource_path = _strip_base_url(resource["url"])
    input_info = requirements["extensions"]["bazaar"]["info"]["input"]
    method = input_info["method"]

    if mode == X402_CHALLENGE_MODE_COMPACT:
        # Compact carries no example path; the resource identity is the only
        # place a materialized path is advertised.
        path = resource_path
        if method in _QUERY_METHODS:
            query = input_info["queryParams"]
            body, body_type = None, None
        else:
            body_type = input_info["bodyType"]
            body = input_info["body"]
            query = None
    else:
        example = input_info["example"]
        path = example["path"]
        assert example["method"] == method, (
            f"{policy_path}: full challenge example method disagrees with input.method"
        )
        if method in _QUERY_METHODS:
            query = example["query"]
            body, body_type = None, None
        else:
            body = example["json"]
            body_type = input_info["bodyType"]
            query = None

    for label, candidate in (("advertised", path), ("resource", resource_path)):
        assert isinstance(candidate, str) and candidate.startswith("/v1/"), (
            f"{policy_path}: {label} path {candidate!r} is not an executable v1 path"
        )
        assert "{" not in candidate and "}" not in candidate, (
            f"{policy_path}: {label} path {candidate!r} still carries an unresolved path "
            "placeholder, so no consumer can execute it"
        )
    if method in _QUERY_METHODS:
        assert isinstance(query, dict), f"{policy_path}: no advertised query input"
    else:
        assert isinstance(body, dict), f"{policy_path}: no advertised body input"
        assert body_type == "json", f"{policy_path}: unexpected bodyType {body_type!r}"

    return AdvertisedRequest(
        policy_path, method, path, resource_path, query, body, body_type
    )


def _acquire_runtime_challenge(client, seed: Seed, mode: str) -> RuntimeChallenge:
    """Drive the real enforcement path and decode what it returned."""
    headers = unpaid_headers()
    headers[X402_CHALLENGE_MODE_HEADER] = mode
    response = client.request(
        seed.method,
        seed.path,
        params=seed.query,
        json=seed.body,
        headers=headers,
    )
    assert response.status_code == 402, (
        f"{seed.policy_path} [{mode}]: seeded unpaid request did not receive a challenge "
        f"({response.status_code}): {response.text[:400]}"
    )

    requirements = _decode_payment_required(response)
    body = response.json()

    # Header/body parity: a consumer reading either representation must be
    # handed the same challenge.
    assert body["error"] == "payment_required"
    assert body["protocol"] == "x402"
    assert body["payment_required"] == requirements, (
        f"{seed.policy_path} [{mode}]: the response body's canonical payment_required "
        "block differs from the decoded PAYMENT-REQUIRED header"
    )
    assert body["resource"] == requirements["resource"]["url"], (
        f"{seed.policy_path} [{mode}]: body resource url differs from ResourceInfo url"
    )

    _assert_mode_fidelity(requirements, mode, f"{seed.policy_path} [{mode}]")

    return RuntimeChallenge(
        mode=mode,
        status_code=response.status_code,
        requirements=requirements,
        body=body,
        pricing_rule_header=response.headers.get("x-stocktrends-pricing-rule"),
        request=_reconstruct(requirements, mode, seed.policy_path),
    )


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

def _fixture_artifact_ids() -> dict[str, str]:
    manifest = json.loads(
        (_FIXTURE_ARTIFACT_STORE / "manifest.json").read_text(encoding="utf-8")
    )
    return {entry["artifact_type"]: entry["artifact_id"] for entry in manifest["artifacts"]}


def _assert_example_materializes_the_policy_path(policy_path: str, example_path: str) -> None:
    """An advertised example must be an instance of the route it describes.

    Stated generically rather than as a list of families: a templated policy
    path is materialized by filling its single placeholder segment, so anything
    that leaves the declared prefix — a different artifact family, an extra
    segment, a neighbouring route — is describing a different endpoint than the
    one whose price, rails and availability boundary the resource publishes.
    """
    if "{" not in policy_path:
        assert example_path == policy_path, (
            f"{policy_path}: advertised example path {example_path!r} is not this route"
        )
        return
    prefix, _, remainder = policy_path.partition("{")
    assert "/" not in remainder.partition("}")[2], (
        f"{policy_path}: this check assumes a single trailing path placeholder"
    )
    assert example_path.startswith(prefix), (
        f"{policy_path}: advertised example path {example_path!r} does not materialize "
        f"this route; expected it to begin with {prefix!r}"
    )
    assert "/" not in example_path[len(prefix):], (
        f"{policy_path}: advertised example path {example_path!r} adds path segments the "
        "route does not have"
    )


def _served_manifest(client) -> dict[str, Any]:
    response = client.get(CANONICAL_DISCOVERY_PATH)
    assert response.status_code == 200
    return response.json()


def _seeds(client, stored_artifact_ids: dict[str, str]) -> list[Seed]:
    """Seed material for the initial challenge, and nothing more.

    Read from the served manifest — the representation a crawler consumes first,
    and the only way to learn a concrete path for a templated route.  An
    availability-gated artifact-by-id route advertises a documented placeholder
    id that no store holds, so its seed substitutes a serveable id from the
    fixture store: a challenge cannot be obtained for an artifact that does not
    exist, and forcing one would break the availability boundary.
    """
    manifest = _served_manifest(client)
    assert manifest["complete"] is True
    assert not manifest["discovery_exceptions"]

    seeds: list[Seed] = []
    for resource in manifest["resources"]:
        example = resource["safe_example_request"]
        path = example["path"]
        _assert_example_materializes_the_policy_path(resource["path"], path)
        target = match_paid_intelligence_artifact_route(resource["method"], path)
        if target is not None and target.artifact_id is not None:
            stored = stored_artifact_ids[target.artifact_type]
            path = f"{path.rsplit('/', 1)[0]}/{stored}"
        seeds.append(
            Seed(
                method=resource["method"],
                policy_path=resource["path"],
                path=path,
                query=example.get("query"),
                body=example.get("json"),
                classification=resource["availability"]["classification"],
            )
        )
    assert seeds, "discovery published no resources to probe"
    return seeds


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def intelligence_fixture_store(monkeypatch) -> dict[str, str]:
    """Point the artifact store at the read-only test fixture corpus."""
    monkeypatch.setenv(STORE_ENV_VAR, str(_FIXTURE_ARTIFACT_STORE))
    stored = _fixture_artifact_ids()
    assert stored, "the intelligence fixture store published no artifacts"
    return stored


@pytest.fixture
def data_access_touches(monkeypatch) -> list[str]:
    """Record any data or artifact-store access made by a governed route.

    Scoped to this suite's question rather than re-proving purity generally: the
    settlement-ordering and semantic-boundary suites already own that for
    rejections.  What is new here is that neither obtaining a challenge nor
    replaying an advertised request may run the paid endpoint.
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


@pytest.fixture
def runtime_challenges(payment_harness, intelligence_fixture_store) -> list[ChallengePair]:
    """One real compact and one real full HTTP 402 per governed resource."""
    seeds = _seeds(payment_harness.client, intelligence_fixture_store)
    pairs = [
        ChallengePair(
            seed=seed,
            compact=_acquire_runtime_challenge(
                payment_harness.client, seed, X402_CHALLENGE_MODE_COMPACT
            ),
            full=_acquire_runtime_challenge(
                payment_harness.client, seed, X402_CHALLENGE_MODE_FULL
            ),
        )
        for seed in seeds
    ]
    # Non-vacuity: the walk must cover the whole governed surface.
    assert {pair.key for pair in pairs} == _governed_keys()
    return pairs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _governed_policies():
    config = get_runtime_payment_policy_config()
    return sorted(
        config.endpoint_payment_policies,
        key=lambda item: (item.path_pattern, item.method),
    )


def _governed_keys() -> set[tuple[str, str]]:
    return {(policy.method.upper(), policy.path_pattern) for policy in _governed_policies()}


def _send(harness, request: AdvertisedRequest, *, query=None, body=None, headers=None):
    return harness.client.request(
        request.method,
        request.path,
        params=query if query is not None else request.query,
        json=body if body is not None else request.body,
        headers=headers if headers is not None else unpaid_headers(),
    )


def _assert_no_payment_activity(harness, label: str = "") -> None:
    assert harness.facilitator.verify_count == 0, f"{label}: facilitator verify ran"
    assert harness.facilitator.settle_count == 0, f"{label}: facilitator settle ran"
    assert harness.mpp.authorize_count == 0, f"{label}: MPP authorize ran"
    assert harness.mpp.capture_count == 0, f"{label}: MPP capture ran"
    assert harness.mpp.void_count == 0, f"{label}: MPP void ran"


def _expected_registry_tags(method: str, path: str) -> list[str]:
    metadata = get_endpoint_metadata(path, method)
    assert metadata is not None, f"{path}: payment-governed but has no canonical metadata"
    expected: list[str] = []
    for tag in (*X402_DOMAIN_ANCHOR_TAGS, *metadata["tags"]):
        if tag not in expected:
            expected.append(tag)
    return expected


# ===========================================================================
# Part B — probeability, proven from the actual HTTP 402
# ===========================================================================

def _is_artifact_by_id(key: tuple[str, str]) -> bool:
    """Paid Intelligence artifact-by-id routes: the only templated governed paths."""
    return key in {
        ("GET", "/v1/intelligence/guidance/{artifact_id}"),
        ("GET", "/v1/intelligence/research/{artifact_id}"),
    }


def test_runtime_402_metadata_rebuilds_a_serviceable_request(
    payment_harness, runtime_challenges, data_access_touches
):
    """The end-to-end PR2 claim, in both challenge modes.

    Nothing here is read from the registry or from a builder: each replayed
    request is rebuilt from the decoded HTTP challenge that runtime returned.

    The artifact-by-id routes are treated exactly as their documented
    availability boundary requires.  Their ResourceInfo names the artifact the
    caller asked for, so replaying it must reach 402; their full-challenge
    example advertises the documented placeholder id, which no store holds, so
    replaying that must reach the artifact-not-found gate rather than 402.
    Forcing the placeholder to 402 would mean serving a challenge for an
    artifact that does not exist.
    """
    replayed_to_challenge = 0
    replayed_to_gate = 0
    for pair in runtime_challenges:
        for mode in _CHALLENGE_MODES:
            request = pair.by_mode(mode).request
            label = f"{pair.key} [{mode}]"

            # ResourceInfo identity is always a serviceable, payable resource.
            identity_response = payment_harness.client.request(
                request.method,
                request.resource_path,
                params=request.query,
                json=request.body,
                headers=unpaid_headers(),
            )
            assert identity_response.status_code == 402, (
                f"{label}: the ResourceInfo identity rebuilt from the HTTP challenge was "
                f"not payable ({identity_response.status_code}): "
                f"{identity_response.text[:400]}"
            )
            _assert_no_payment_activity(payment_harness, label)

            response = _send(payment_harness, request)
            if _is_artifact_by_id(pair.key) and request.path != request.resource_path:
                assert response.status_code == 404, (
                    f"{label}: the advertised placeholder example reached "
                    f"{response.status_code}, not the documented availability gate: "
                    f"{response.text[:300]}"
                )
                assert response.json()["detail"]["error"] == "intelligence_artifact_not_found"
                assert request.route_family == request.resource_path.rsplit("/", 1)[0], (
                    f"{label}: the advertised example is not even on the same route as the "
                    "resource it describes"
                )
                replayed_to_gate += 1
            else:
                assert response.status_code == 402, (
                    f"{label}: the request rebuilt from the advertised HTTP challenge was "
                    f"not serviceable ({response.status_code}): {response.text[:400]}"
                )
                replayed_to_challenge += 1
            _assert_no_payment_activity(payment_harness, label)

    assert not data_access_touches
    # 28 resources x 2 modes; only the two by-id routes' full-mode examples are
    # answered by the availability gate.
    assert replayed_to_gate == 2
    assert replayed_to_challenge == 2 * len(_governed_keys()) - 2


def test_compact_and_full_runtime_challenges_agree_on_callable_semantics(runtime_challenges):
    """Two consumers reading two challenge modes must call the same thing."""
    for pair in runtime_challenges:
        assert pair.compact.request.callable_semantics == pair.full.request.callable_semantics, (
            f"{pair.key}: compact and full HTTP challenges advertise different callable "
            f"semantics:\n  compact={pair.compact.request}\n  full={pair.full.request}"
        )
        # Resource identity travels on ResourceInfo, so it must match too.
        assert pair.compact.resource == pair.full.resource, (
            f"{pair.key}: compact and full HTTP challenges publish different ResourceInfo"
        )
        assert pair.compact.request.resource_path == pair.full.request.resource_path

        if _is_artifact_by_id(pair.key):
            # The documented placeholder example and the concrete resource must
            # at least name the same route, or one of them is not describing the
            # other at all.
            assert pair.full.request.route_family == pair.compact.request.route_family
        else:
            assert pair.compact.request.path == pair.full.request.path, (
                f"{pair.key}: compact and full HTTP challenges materialize different paths"
            )


def test_runtime_honours_the_requested_challenge_mode(runtime_challenges):
    """A mode swap must be visible, and compact must actually be smaller."""
    for pair in runtime_challenges:
        _assert_mode_fidelity(
            pair.compact.requirements, X402_CHALLENGE_MODE_COMPACT, f"{pair.key} recheck"
        )
        _assert_mode_fidelity(
            pair.full.requirements, X402_CHALLENGE_MODE_FULL, f"{pair.key} recheck"
        )
        compact_size = len(json.dumps(pair.compact.requirements, separators=(",", ":")))
        full_size = len(json.dumps(pair.full.requirements, separators=(",", ":")))
        assert compact_size < full_size, (
            f"{pair.key}: the compact challenge is not smaller than the full one "
            f"({compact_size} >= {full_size}); the modes may have been swapped"
        )


def test_runtime_challenge_header_and_body_carry_the_same_challenge(runtime_challenges):
    """Restated as a named contract; also enforced during acquisition."""
    for pair in runtime_challenges:
        for mode in _CHALLENGE_MODES:
            challenge = pair.by_mode(mode)
            assert challenge.body["payment_required"] == challenge.requirements
            assert challenge.body["resource"] == challenge.resource["url"]
            assert challenge.body["accepted_payment_methods"]


def test_an_unresolved_path_placeholder_fails_reconstruction():
    """A templated advertised path is not adequate proof of anything.

    Exercised against the reconstruction step directly, because runtime cannot
    be made to emit one without changing enforcement.
    """
    requirements = {
        "resource": {"url": "/v1/intelligence/guidance/{artifact_id}"},
        "extensions": {
            "bazaar": {
                "info": {"input": {"method": "GET", "queryParams": {}}},
            }
        },
    }
    with pytest.raises(AssertionError, match="unresolved path placeholder"):
        _reconstruct(requirements, X402_CHALLENGE_MODE_COMPACT, "probe")


# ===========================================================================
# Availability-gated Intelligence routes — narrow, derived, unchanged
# ===========================================================================

def test_availability_gated_classification_is_derived_from_the_runtime_boundary(
    payment_harness, intelligence_fixture_store
):
    gated = set()
    for seed in _seeds(payment_harness.client, intelligence_fixture_store):
        expected = (
            "pre_payment_availability_gated"
            if match_paid_intelligence_artifact_route(seed.method, seed.path) is not None
            else "immediately_discoverable"
        )
        assert seed.classification == expected, seed.policy_path
        if expected == "pre_payment_availability_gated":
            gated.add(seed.key)
    assert gated == _AVAILABILITY_GATED_RESOURCES


def test_availability_gated_routes_hold_their_gate_without_a_store(
    payment_harness, monkeypatch
):
    """With no store, the gate answers — and says so, rather than 402ing."""
    monkeypatch.delenv(STORE_ENV_VAR, raising=False)
    manifest = _served_manifest(payment_harness.client)
    gated = [
        resource
        for resource in manifest["resources"]
        if resource["availability"]["classification"] == "pre_payment_availability_gated"
    ]
    assert {(r["method"], r["path"]) for r in gated} == _AVAILABILITY_GATED_RESOURCES

    for resource in gated:
        example = resource["safe_example_request"]
        response = payment_harness.client.get(
            example["path"], params=example.get("query"), headers=unpaid_headers()
        )
        assert response.status_code in resource["availability"]["possible_unpaid_statuses"]
        assert response.status_code != 402
        assert response.json()["detail"]["error"] in _AVAILABILITY_GATE_ERROR_CODES
        _assert_no_payment_activity(payment_harness, resource["path"])


def test_availability_gate_cannot_hide_a_malformed_route_or_example(
    payment_harness, intelligence_fixture_store
):
    """The gate must answer availability questions, never validity ones.

    With the store open, an advertised placeholder artifact id reaches the
    artifact-not-found gate; the identical route with a serveable id reaches a
    real 402.  That difference is what proves the advertised route shape is
    sound and only the artifact is absent — an unserviceable example would fail
    both ways and would otherwise be indistinguishable behind a 503.
    """
    manifest = _served_manifest(payment_harness.client)
    checked_by_id = 0
    checked_latest = 0
    for resource in manifest["resources"]:
        if (resource["method"], resource["path"]) not in _AVAILABILITY_GATED_RESOURCES:
            continue
        example = resource["safe_example_request"]
        target = match_paid_intelligence_artifact_route(resource["method"], example["path"])
        assert target is not None
        response = payment_harness.client.get(
            example["path"], params=example.get("query"), headers=unpaid_headers()
        )

        if target.artifact_id is None:
            assert response.status_code == 402, (resource["path"], response.text[:300])
            checked_latest += 1
        else:
            assert response.status_code == 404, (resource["path"], response.text[:300])
            assert response.json()["detail"]["error"] == "intelligence_artifact_not_found"
            stored = intelligence_fixture_store[target.artifact_type]
            stored_path = f"{example['path'].rsplit('/', 1)[0]}/{stored}"
            stored_response = payment_harness.client.get(stored_path, headers=unpaid_headers())
            assert stored_response.status_code == 402, (
                f"{resource['path']}: the advertised route is not serviceable even with a "
                f"stored artifact ({stored_response.status_code}) — the example is "
                "malformed, not merely unavailable"
            )
            checked_by_id += 1
        _assert_no_payment_activity(payment_harness, resource["path"])

    assert checked_latest == 2
    assert checked_by_id == 2


# ===========================================================================
# Part A — ResourceInfo tags, read from the actual HTTP challenge
# ===========================================================================

def test_runtime_resource_tags_satisfy_the_x402_tag_format(runtime_challenges):
    """Format limits an x402 indexer can be expected to honour."""
    for pair in runtime_challenges:
        for mode in _CHALLENGE_MODES:
            tags = pair.by_mode(mode).tags
            label = f"{pair.key} [{mode}]"
            assert isinstance(tags, list) and tags, f"{label}: no tags"
            assert len(tags) <= X402_RESOURCE_TAG_LIMIT, (
                f"{label}: {len(tags)} tags exceeds the budget of "
                f"{X402_RESOURCE_TAG_LIMIT}: {tags}"
            )
            assert len(set(tags)) == len(tags), f"{label}: duplicate tags {tags}"
            for tag in tags:
                assert isinstance(tag, str), f"{label}: non-string tag {tag!r}"
                assert tag, f"{label}: empty tag"
                assert len(tag) <= _TAG_MAX_LENGTH, (
                    f"{label}: tag {tag!r} is {len(tag)} characters, over "
                    f"{_TAG_MAX_LENGTH}"
                )
                assert all(0x20 <= ord(char) <= 0x7E for char in tag), (
                    f"{label}: tag {tag!r} is not printable ASCII"
                )


def test_runtime_resource_tags_are_the_canonical_registry_tags(runtime_challenges):
    """Anchors plus that endpoint's declared capability tags, untruncated."""
    for pair in runtime_challenges:
        expected = _expected_registry_tags(*pair.key)
        assert len(expected) <= X402_RESOURCE_TAG_LIMIT, (
            f"{pair.key}: declares {len(expected)} tags, so the accessor would silently "
            f"truncate to {X402_RESOURCE_TAG_LIMIT}: {expected}"
        )
        for mode in _CHALLENGE_MODES:
            assert pair.by_mode(mode).tags == expected, f"{pair.key} [{mode}]"


def test_runtime_resource_tags_carry_anchors_plus_capability_semantics(runtime_challenges):
    for pair in runtime_challenges:
        tags = pair.compact.tags
        assert tags[: len(X402_DOMAIN_ANCHOR_TAGS)] == list(X402_DOMAIN_ANCHOR_TAGS), (
            f"{pair.key}: expected the stable domain anchors first, got {tags}"
        )
        assert len(tags) > len(X402_DOMAIN_ANCHOR_TAGS), (
            f"{pair.key}: carries only generic anchors and describes no capability"
        )


def test_runtime_resource_tags_never_regress_to_the_service_tag_set(runtime_challenges):
    """The defect PR2 exists to fix: one service-level set on every resource."""
    for pair in runtime_challenges:
        for mode in _CHALLENGE_MODES:
            tags = pair.by_mode(mode).tags
            assert tags != list(SERVICE_TAGS), f"{pair.key} [{mode}]: service-level fallback"
            assert set(tags) != set(SERVICE_TAGS), f"{pair.key} [{mode}]"


def _distinct_tag_sets(pairs: list[ChallengePair]) -> set[tuple[str, ...]]:
    return {tuple(pair.compact.tags) for pair in pairs}


def test_the_governed_surface_is_not_one_uniform_tag_set(runtime_challenges):
    """Identical tuples are allowed where the vocabulary is genuinely shared.

    What is not allowed is the whole payable surface collapsing to one set,
    which is exactly the pre-PR2 state.  No uniqueness ratio is asserted: x402
    does not require distinct tuples, and pinning one would constrain future
    taxonomy for no protocol reason.
    """
    distinct = _distinct_tag_sets(runtime_challenges)
    assert len(distinct) > 1, (
        f"the entire payable surface emits one tag set: {sorted(distinct)}"
    )


def test_uniform_tags_are_detectable(payment_harness, intelligence_fixture_store, monkeypatch):
    """Reverting to one service-wide set must break the anti-uniformity test.

    Without this the test above could pass for the wrong reason — for example if
    it enumerated nothing.
    """
    uniform = ["market-intelligence", "agentic"]
    for entry in endpoint_metadata_module._ENDPOINT_METADATA_BY_PATH.values():
        monkeypatch.setitem(entry, "tags", list(uniform))

    seeds = _seeds(payment_harness.client, intelligence_fixture_store)
    pairs = [
        ChallengePair(
            seed=seed,
            compact=_acquire_runtime_challenge(
                payment_harness.client, seed, X402_CHALLENGE_MODE_COMPACT
            ),
            full=_acquire_runtime_challenge(
                payment_harness.client, seed, X402_CHALLENGE_MODE_FULL
            ),
        )
        for seed in seeds
    ]
    assert len(_distinct_tag_sets(pairs)) == 1, (
        "a uniform tag set went undetected by the anti-uniformity check"
    )


def test_every_governed_endpoint_declares_explicit_semantic_tags():
    """No governed resource may inherit the generic category fallback.

    ``_metadata`` falls back to exactly ``[category]`` when an endpoint declares
    no tags, so that shape is what this rejects.  Endpoint semantics must be
    deliberate metadata.

    No count is asserted.  One explicit, truthful capability tag is a complete
    declaration — ``finance``, ``equities``, ``<capability>`` describes an
    endpoint perfectly well — and a minimum would be an arbitrary taxonomy rule
    that x402 does not impose.  What the tags must actually achieve is enforced
    by the emitted-ResourceInfo contracts above: anchors are retained, capability
    semantics are present, the old uniform ``SERVICE_TAGS`` set is detected as a
    regression, representative expectations are pinned, and the surface cannot
    collapse to one tag set.
    """
    for policy in _governed_policies():
        method = policy.method.upper()
        path = policy.path_pattern
        metadata = get_endpoint_metadata(path, method)
        assert metadata is not None, f"{path}: payment-governed but has no canonical metadata"
        tags = metadata["tags"]
        assert isinstance(tags, list) and tags, f"{path}: no endpoint tags"
        assert all(isinstance(tag, str) and tag for tag in tags), f"{path}: {tags}"
        assert tags != [metadata["category"]], (
            f"{path}: silently inherited the generic category fallback {tags}"
        )


def test_baseline_capability_semantics_are_advertised(runtime_challenges):
    """Named Stock Trends capabilities must be present where they belong.

    Asserted as substring presence across a resource's tags, so a later
    rewording within the same concept does not fail the test while dropping the
    concept does.
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
    by_key = {pair.key: pair for pair in runtime_challenges}
    for key, required in expectations.items():
        joined = " ".join(by_key[key].compact.tags)
        for concept in required:
            assert concept in joined, f"{key}: {concept!r} missing from {by_key[key].compact.tags}"


def test_probabilistic_return_semantics_favour_inference_over_breadth(runtime_challenges):
    """A probabilistic-returns need must not resolve to a breadth resource."""
    by_key = {pair.key: pair for pair in runtime_challenges}
    inference = set(by_key[("GET", "/v1/stim/latest")].compact.tags)
    breadth = set(by_key[("GET", "/v1/breadth/sector/history")].compact.tags)
    assert {"probabilistic-returns", "forward-returns"} <= inference
    assert not (inference & breadth) - set(X402_DOMAIN_ANCHOR_TAGS)


# ---------------------------------------------------------------------------
# Canonical registry is the authority — proven behaviourally
# ---------------------------------------------------------------------------

def _tags_over_http(client, seed: Seed) -> list[str]:
    return _acquire_runtime_challenge(client, seed, X402_CHALLENGE_MODE_COMPACT).tags


def test_canonical_registry_is_the_authority_for_emitted_tags(
    payment_harness, intelligence_fixture_store, monkeypatch
):
    """Change the registry, and the emitted ResourceInfo must change with it.

    This is the architectural claim stated as behaviour rather than as source
    shape: an independent payment-side endpoint tag table would keep serving its
    own values here and the mutation would not appear.  A second, untouched
    resource is checked in the same run so the test cannot pass by a global
    override that happens to move everything together.
    """
    seeds = {seed.key: seed for seed in _seeds(payment_harness.client, intelligence_fixture_store)}
    mutated_key = ("GET", "/v1/breadth/sector/latest")
    control_key = ("GET", "/v1/market/regime/latest")

    before_mutated = _tags_over_http(payment_harness.client, seeds[mutated_key])
    before_control = _tags_over_http(payment_harness.client, seeds[control_key])
    assert before_mutated != before_control

    sentinel = ["registry-authority-probe", "second-probe-tag"]
    monkeypatch.setitem(
        endpoint_metadata_module._ENDPOINT_METADATA_BY_PATH[mutated_key[1]],
        "tags",
        list(sentinel),
    )

    after_mutated = _tags_over_http(payment_harness.client, seeds[mutated_key])
    after_control = _tags_over_http(payment_harness.client, seeds[control_key])

    assert after_mutated == [*X402_DOMAIN_ANCHOR_TAGS, *sentinel], (
        "the emitted ResourceInfo did not follow the canonical registry; payment code "
        f"appears to hold its own endpoint tag table: {after_mutated}"
    )
    assert after_mutated != before_mutated
    assert after_control == before_control, (
        "mutating one endpoint changed another; tags are not resolved per endpoint"
    )


def test_over_budget_registry_tags_never_reach_a_resource(
    payment_harness, intelligence_fixture_store, monkeypatch
):
    """Six declared tags must not become six emitted tags."""
    seeds = {seed.key: seed for seed in _seeds(payment_harness.client, intelligence_fixture_store)}
    key = ("GET", "/v1/market/regime/latest")
    monkeypatch.setitem(
        endpoint_metadata_module._ENDPOINT_METADATA_BY_PATH[key[1]],
        "tags",
        [f"probe-{index}" for index in range(6)],
    )
    assert len(_tags_over_http(payment_harness.client, seeds[key])) == X402_RESOURCE_TAG_LIMIT


# ===========================================================================
# Pricing / payment-policy / rail non-drift
# ===========================================================================

def test_discovery_metadata_did_not_alter_the_payment_contract(
    payment_harness, runtime_challenges
):
    """Tags are metadata. They select no rail and change nothing charged."""
    policies = {(p.method.upper(), p.path_pattern): p for p in _governed_policies()}
    manifest = _served_manifest(payment_harness.client)
    resources = {(r["method"], r["path"]): r for r in manifest["resources"]}
    assert set(resources) == set(policies)

    for pair in runtime_challenges:
        policy = policies[pair.key]
        resource = resources[pair.key]
        assert resource["pricing_rule_id"] == policy.pricing_rule_id
        assert resource["supported_rails"] == list(policy.allowed_rails)
        assert resource["pricing"]["live_cost_included"] is False

        for mode in _CHALLENGE_MODES:
            challenge = pair.by_mode(mode)
            assert challenge.pricing_rule_header == policy.pricing_rule_id, (
                f"{pair.key} [{mode}]: the 402 quoted pricing rule "
                f"{challenge.pricing_rule_header!r}"
            )
            accepts = challenge.requirements["accepts"][0]
            assert accepts["scheme"] == x402_module.X402_DEFAULT_SCHEME
            assert accepts["network"] == x402_module.X402_DEFAULT_NETWORK
            assert accepts["asset"] == x402_module.X402_DEFAULT_TOKEN
            assert accepts["payTo"] == x402_module.X402_SELLER_ADDRESS
            assert accepts["amount"] == UNIT_PRICE_ATOMIC


# ===========================================================================
# Part C — a payment-bearing invalid request never reaches payment.
# This is the durable invariant.  These requests present a well-formed payment
# artifact, so a validation regression that let them through would settle
# money, not merely change a status code.  The
# unpaid case is deliberately NOT asserted here: which status an unpaid invalid
# request receives is a separate question that PR3 may revisit.
# ===========================================================================

_PAYING_RAILS = {"x402": x402_headers, "mpp": mpp_headers}


def _assert_rejected_before_payment(harness, response, label: str) -> None:
    assert response.status_code in {400, 422}, (
        f"{label}: expected a deterministic input rejection, got "
        f"{response.status_code}: {response.text[:300]}"
    )
    _assert_no_payment_activity(harness, label)


def _required_request_inputs(request: AdvertisedRequest) -> list[str]:
    """Required inputs the advertised request supplies in query or body.

    Path-located requirements are excluded on purpose: removing one changes the
    route rather than the request, which is a route-miss question already owned
    by the settlement-ordering suite.
    """
    metadata = get_endpoint_metadata(request.policy_path, request.method) or {}
    supplied = request.query if request.query is not None else (request.body or {})
    return [
        name
        for name, spec in (metadata.get("required_inputs") or {}).items()
        if (spec.get("parameter_source") or spec.get("input_location")) != "path"
        and name in supplied
    ]


@pytest.mark.parametrize("rail", sorted(_PAYING_RAILS))
def test_stripping_a_required_input_never_reaches_payment(
    payment_harness, runtime_challenges, data_access_touches, rail
):
    """Advertising valid examples must not have made invalid ones payable."""
    headers_for = _PAYING_RAILS[rail]
    exercised = 0
    for pair in runtime_challenges:
        request = pair.compact.request
        required = _required_request_inputs(request)
        if not required:
            continue
        supplied = request.query if request.query is not None else request.body
        stripped = {name: value for name, value in supplied.items() if name not in required}
        label = f"{pair.key} [{rail}] minus {required}"

        if request.query is not None:
            response = _send(payment_harness, request, query=stripped, headers=headers_for())
        else:
            response = _send(payment_harness, request, body=stripped, headers=headers_for())

        _assert_rejected_before_payment(payment_harness, response, label)
        exercised += 1

    assert not data_access_touches
    assert exercised >= 10, (
        f"only {exercised} resources were probed for required-input rejection; the "
        "registry no longer declares the required inputs this relies on"
    )


@pytest.mark.parametrize("rail", sorted(_PAYING_RAILS))
@pytest.mark.parametrize(
    ("path", "query"),
    [
        # Symbol identity present but semantically incomplete.
        ("/v1/indicators/latest", {"symbol_exchange": "IBM"}),
        ("/v1/stim/latest", {"symbol_exchange": "IBM"}),
        # Malformed value FastAPI itself rejects.
        ("/v1/prices/history", {"symbol_exchange": "IBM-N", "limit": "not-a-number"}),
        # Value outside the registered enum domain.
        ("/v1/stwr/reports/latest", {"rpt": "bullcross", "exchange": "ZZ"}),
    ],
)
def test_malformed_paying_requests_are_rejected_before_payment(
    payment_harness, data_access_touches, rail, path, query
):
    response = payment_harness.client.get(
        path, params=query, headers=_PAYING_RAILS[rail]()
    )
    _assert_rejected_before_payment(payment_harness, response, f"{path} [{rail}]")
    assert not data_access_touches


@pytest.mark.parametrize("rail", sorted(_PAYING_RAILS))
def test_a_malformed_paying_body_is_rejected_before_payment(
    payment_harness, data_access_touches, rail
):
    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol",
        json={"symbol_exchange": "IBM"},
        headers=_PAYING_RAILS[rail](),
    )
    _assert_rejected_before_payment(
        payment_harness, response, f"/v1/decision/evaluate-symbol [{rail}]"
    )
    assert not data_access_touches


def test_a_broken_advertised_example_is_provably_invalid(
    payment_harness, data_access_touches, monkeypatch
):
    """The falsification guard for the probeability contract.

    Three steps, none of which depend on how an *unpaid* invalid request is
    answered:

      1. mutate the canonical example to input runtime cannot serve;
      2. prove the mutation is genuinely observed in emitted discovery metadata,
         read back over HTTP from the served manifest;
      3. present that mutated input as a payment-bearing request and require it
         to be rejected with no verification, settlement, MPP control-plane
         traffic or paid execution.
    """
    path = "/v1/indicators/latest"
    broken_query = {"symbol_exchange": "IBM"}
    monkeypatch.setitem(
        endpoint_metadata_module._ENDPOINT_METADATA_BY_PATH[path],
        "safe_example_request",
        {"method": "GET", "path": path, "query": copy.deepcopy(broken_query)},
    )

    # 2 — the mutation is what discovery now advertises.
    manifest = _served_manifest(payment_harness.client)
    advertised = next(
        resource for resource in manifest["resources"] if resource["path"] == path
    )
    assert advertised["safe_example_request"]["query"] == broken_query, (
        "the mutation was not observed in emitted discovery metadata"
    )

    # 3 — and it is genuinely unserviceable, on every paying rail.
    for rail, headers_for in sorted(_PAYING_RAILS.items()):
        response = payment_harness.client.get(
            advertised["safe_example_request"]["path"],
            params=advertised["safe_example_request"]["query"],
            headers=headers_for(),
        )
        _assert_rejected_before_payment(payment_harness, response, f"{path} [{rail}]")
    assert not data_access_touches


def test_a_valid_advertised_example_reaches_the_payment_gate(
    payment_harness, intelligence_fixture_store
):
    """The counterpart to the test above, so neither passes vacuously.

    The unmutated advertised example, on the same paying rail, must get *past*
    validation and reach the payment gate — proven by the MPP control plane
    being asked to authorize, which the mutated request above never does.  The
    control plane is made to decline, so the endpoint still never executes and
    nothing is captured; the point is only that the request was serviceable
    enough to be charged for.
    """
    payment_harness.mpp.authorize_success = False
    seeds = {seed.key: seed for seed in _seeds(payment_harness.client, intelligence_fixture_store)}
    seed = seeds[("GET", "/v1/indicators/latest")]

    response = payment_harness.client.get(
        seed.path, params=seed.query, headers=mpp_headers()
    )
    assert response.status_code not in {400, 422}, (
        f"a valid advertised example was rejected as invalid input: {response.text[:300]}"
    )
    assert payment_harness.mpp.authorize_count == 1, (
        "the valid advertised example never reached the payment gate, so the rejection "
        "of the mutated example above cannot be attributed to the mutation"
    )
    assert payment_harness.mpp.capture_count == 0
    assert payment_harness.facilitator.settle_count == 0

    unpaid_response = payment_harness.client.get(
        seed.path, params=seed.query, headers=unpaid_headers()
    )
    assert unpaid_response.status_code == 402


# ===========================================================================
# Structural: the governed surface itself
# ===========================================================================

def test_runtime_policy_surface_is_fully_represented(payment_harness):
    manifest = _served_manifest(payment_harness.client)
    assert {(r["method"], r["path"]) for r in manifest["resources"]} == _governed_keys()
    assert manifest["complete"] is True
    assert not manifest["discovery_exceptions"]
    assert Decimal(UNIT_PRICE_ATOMIC) > 0
