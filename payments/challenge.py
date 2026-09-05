"""
x402 challenge issuance — the half of the protocol that moves no money.

Challenge is not settlement
---------------------------
An x402 exchange has two distinct halves, and this module is deliberately only
the first:

    challenge issuance      quotes a price and describes the resource;
                            contacts nobody, verifies nothing, settles nothing.

    payment verification    inspects a presented artifact, contacts the
    and settlement          facilitator or the MPP control plane, and can move
                            money.  It lives in `payments.enforcement` and runs
                            behind the deferred gate, after FastAPI's structural
                            validation and after request-only semantic
                            validation.

The earlier payment-boundary work established that no deterministic client-input
failure knowable before paid execution may cause settlement, and it achieved
that by moving *the whole payment step* behind validation.  That was necessary
but not sufficient: it also moved challenge issuance behind validation, and a
challenge is a description of a resource, not a charge against it.  The
consequence was visible externally.  A machine-discovery probe of a canonical
resource URL — `GET /v1/prices/history` with no `symbol_exchange` — was answered
with `400 missing_required_param` before the payment contract was ever emitted,
so an indexer reading that resource saw no `402`, no payment requirements and no
Bazaar metadata, and concluded the resource was not payable.

PR3 separates the two halves.  For a recognized fixed-price payable route with
no payment authorization or proof presented, the challenge is issued from route
and policy knowledge alone, before application-input validation.  For a request
that *does* present payment material, nothing changes: it takes the full
structural -> semantic -> gate -> endpoint path, and the settlement seam stays
exactly where the earlier remediation put it.

The asymmetry is the point:

    unpaid + incomplete          -> 402 challenge   (nothing verified or settled)
    payment-bearing + incomplete -> 400/422         (nothing verified or settled)

What this module does and does not do
------------------------------------
Nothing here performs a payment-rail operation or any paid market-data
execution.  It calls the pure x402 challenge builder and the static endpoint
preview registry, and it is handed the accepted payment methods its caller
already resolved from the request's payment-policy snapshot.  There is no
facilitator call, no MPP control-plane call, no database access and no endpoint
execution.

It deliberately performs no payment-policy lookup of its own.  Reading live
policy here would let a challenge advertise rails from a newer configuration
snapshot than the one that chose the request's pricing rule and amount, and
would make a supposedly pure issuance layer perform configuration I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from api.route_recognition import recognize_route
from discovery.preview import get_endpoint_preview
from payments.x402 import (
    X402_CHALLENGE_MODE_HEADER,
    build_x402_challenge,
    has_x402_payment_proof,
)
from services.intelligence_artifact_availability import (
    match_paid_intelligence_artifact_route,
)

#: Stable error code and detail for a challenge, on both the response body and
#: the request-event row.  A challenge means "this resource is payable and here
#: is the contract"; it never means a payment was attempted, refused or taken.
CHALLENGE_ERROR_CODE = "payment_required"
CHALLENGE_ERROR_DETAIL = "x402 payment required"

#: Rail that early challenge issuance speaks for.  MPP is deliberately excluded:
#: it is session-based, authorizes against a session balance rather than
#: answering a per-request challenge, and its callers always present payment
#: material — so an MPP request is never an unpaid canonical probe.
EARLY_CHALLENGE_RAIL = "x402"


class EarlyChallengeClass(str, Enum):
    """
    Why a paid route may or may not be challenged before its inputs are known.

    Exactly one class is eligible.  Every other value names a reason the route
    was deliberately left on the ordinary validate-then-gate path, so an audit
    can read the classification rather than infer it.
    """

    #: Eligible.  Payment requirement and price are fully determined by
    #: (path, method) through an exact endpoint payment policy and its pricing
    #: rule, so the complete challenge can be built without a single request
    #: value.
    FIXED_PRICE = "fixed_price"

    #: No exact endpoint payment policy governs this path and method.  Prefix
    #: governance (`/v1/stim`) is deliberately not enough: it would turn the
    #: middleware into a "paid-looking URL -> 402" mechanism for paths that no
    #: policy actually prices.
    NO_ENDPOINT_POLICY = "no_endpoint_policy"

    #: The policy governs the route but does not enable the x402 rail here.
    NO_X402_RAIL = "no_x402_rail"

    #: The policy carries no pricing rule, so no amount can be quoted without
    #: guessing.  Fail closed.
    NO_PRICING_RULE = "no_pricing_rule"

    #: Availability-gated.  Paid Intelligence artifact routes confirm that the
    #: artifact store is reachable and the artifact exists *before* any payment
    #: challenge, and answer `503`/`404` when it is not.  The system does not
    #: quote a price for an intelligence product it cannot serve, so these
    #: routes must never be challenged ahead of that gate.
    AVAILABILITY_GATED = "availability_gated"

    #: The route template carries path parameters, so there is no bare canonical
    #: URL for a machine to probe and the "challenge before inputs are known"
    #: rationale does not apply.  Excluded rather than reasoned about per route.
    PARAMETERIZED_RESOURCE = "parameterized_resource"

    #: The path and method do not resolve to a real API route on this
    #: application — a route miss (`404`) or a method miss (`405`).  Recognition
    #: is authoritative and comes from the application's own routers.
    UNRECOGNIZED_ROUTE = "unrecognized_route"


@dataclass(frozen=True)
class EarlyChallengeDecision:
    """Whether an unpaid request may be answered with a challenge alone."""

    challenge_class: EarlyChallengeClass
    pricing_rule_id: str | None = None
    route_template: str | None = None

    @property
    def eligible(self) -> bool:
        return self.challenge_class is EarlyChallengeClass.FIXED_PRICE


@dataclass(frozen=True)
class X402Challenge:
    """
    A complete `402` payload, and nothing else.

    Carries no payment reference, no collected amount and no settlement
    response, because issuing this involved none of those.  Its presence must
    never be read as evidence that a rail was contacted.
    """

    body: dict
    payment_required_header: str
    accepted_payment_methods: str
    payment_network: str | None = None
    payment_token: str | None = None


def presents_x402_payment_proof(headers: Any) -> bool:
    """
    The discriminator the early challenge turns on: has this caller paid?

    Delegates to `payments.x402.has_x402_payment_proof`, which is the single
    definition of an x402 authorization carrier — the published
    `X402_PROOF_HEADERS` plus the supported `Authorization: x402 …` form.  This
    module deliberately keeps no proof-header list of its own; a second list
    would drift from the contract the facilitator path actually reads.

    An earlier revision of this guard also treated the descriptive Stock Trends
    payment headers (network, token, amount, reference, channel id) as proof.
    That was too broad: a caller can describe what it intends to pay with while
    holding no authorization at all, and such a caller is precisely the one that
    needs the challenge.  Naming a rail in `X-StockTrends-Payment-Method` is
    likewise intent, not payment.

    MPP stays safe without being named here.  The early-challenge guard requires
    the *resolved rail* to be x402, and an MPP request declares `mpp` and
    resolves to the MPP rail, so it is never intercepted — see
    `EARLY_CHALLENGE_RAIL`.
    """
    return has_x402_payment_proof(headers)


def challenge_mode_from_headers(headers: Any) -> str | None:
    """The challenge mode the caller asked for, or `None` for the default."""
    if headers is None:
        return None
    return (
        headers.get(X402_CHALLENGE_MODE_HEADER)
        or headers.get(X402_CHALLENGE_MODE_HEADER.lower())
    )


def classify_early_challenge_route(
    path: str,
    method: str,
    *,
    endpoint_policy: Any,
    route_template: str | None,
) -> EarlyChallengeDecision:
    """
    Classify a recognized route against the early-challenge criteria.

    `endpoint_policy` is the effective endpoint payment policy already resolved
    for this request — passed in rather than re-resolved so the classification
    and the request's own pricing cannot disagree about which policy applies.
    `route_template` is the recognized route's declared path template.

    Every rejection is named.  Ambiguity fails closed: a route that is not
    positively identified as fixed-price stays on the ordinary path, where it
    behaves exactly as it did before PR3.
    """
    if endpoint_policy is None:
        return EarlyChallengeDecision(EarlyChallengeClass.NO_ENDPOINT_POLICY)

    if EARLY_CHALLENGE_RAIL not in (endpoint_policy.machine_payment_rails or ()):
        return EarlyChallengeDecision(EarlyChallengeClass.NO_X402_RAIL)

    if not endpoint_policy.pricing_rule_id:
        return EarlyChallengeDecision(EarlyChallengeClass.NO_PRICING_RULE)

    if match_paid_intelligence_artifact_route(method, path) is not None:
        return EarlyChallengeDecision(
            EarlyChallengeClass.AVAILABILITY_GATED,
            pricing_rule_id=endpoint_policy.pricing_rule_id,
            route_template=route_template,
        )

    if route_template is None:
        # The route's declared shape is unknown, so neither the parameterized
        # check below nor anything else can be decided about it.  Unreachable
        # from `decide_early_challenge`, which classifies only recognized
        # routes; kept as a fail-closed answer for any other caller.
        return EarlyChallengeDecision(EarlyChallengeClass.UNRECOGNIZED_ROUTE)

    if "{" in route_template:
        return EarlyChallengeDecision(
            EarlyChallengeClass.PARAMETERIZED_RESOURCE,
            pricing_rule_id=endpoint_policy.pricing_rule_id,
            route_template=route_template,
        )

    return EarlyChallengeDecision(
        EarlyChallengeClass.FIXED_PRICE,
        pricing_rule_id=endpoint_policy.pricing_rule_id,
        route_template=route_template,
    )


def decide_early_challenge(
    *,
    app: Any,
    scope: Any,
    path: str,
    method: str,
    endpoint_policy: Any,
) -> EarlyChallengeDecision:
    """
    The complete early-challenge decision for one request.

    Route recognition runs first and is authoritative: a challenge may only be
    issued once the application's own routers confirm that this path and method
    resolve to a real API route.  A route miss stays `404` and a method miss
    stays `405`, because neither is recognized here and both fall through to the
    dispatcher untouched.
    """
    recognized = recognize_route(app, scope)
    if not recognized.is_api_route:
        return EarlyChallengeDecision(EarlyChallengeClass.UNRECOGNIZED_ROUTE)

    return classify_early_challenge_route(
        path,
        method,
        endpoint_policy=endpoint_policy,
        route_template=recognized.route_template,
    )


def challenge_precondition_metadata(decision: EarlyChallengeDecision) -> dict:
    """
    The publishable lifecycle contract for one resource, from its classification.

    Every surface that tells an agent when a `402` becomes reachable — the
    OpenAPI payment extension, the x402 discovery manifest, the tools manifest —
    renders this same object, so none of them can describe a precondition the
    runtime does not apply.  It is derived from `EarlyChallengeDecision`, which
    is the same classifier the request path consults; it is deliberately not a
    second hand-maintained list of paths.

    A single global boolean cannot state this truthfully, because the exceptions
    are real:

    * a recognized fixed-price resource is challengeable at its bare canonical
      URL, before application-input validation;
    * an availability-gated resource resolves availability first and answers
      `404`/`503` when the artifact is not serveable, so a challenge is not the
      first thing a probe can expect;
    * a parameterized resource has no bare canonical URL to probe at all.

    Settlement is the invariant across all three: no resource verifies, settles
    or captures against an unserviceable request.
    """
    challenge_class = decision.challenge_class
    eligible = decision.eligible

    return {
        "challenge_class": challenge_class.value,
        # False only where the runtime really will challenge a bare probe.
        "serviceable_request_required_before_challenge": not eligible,
        # True everywhere, on every rail, without exception.
        "serviceable_request_required_before_settlement": True,
        "bare_canonical_probe_returns_challenge": eligible,
        "availability_gate_precedes_challenge": (
            challenge_class is EarlyChallengeClass.AVAILABILITY_GATED
        ),
        # Read from the route template rather than from the winning class: a
        # by-id Intelligence artifact route is availability-gated *and*
        # parameterized, and reporting only the first would understate the
        # second.  The class says why the resource is excluded from early
        # challenge; the flags describe the resource.
        "parameterized_resource": bool(
            decision.route_template and "{" in decision.route_template
        ),
    }


def decorate_x402_challenge(
    *,
    path: str,
    method: str,
    challenge_body: dict,
    payment_required_header: str,
    pricing_rule_id: str | None,
    amount_usd: Decimal,
    accepted_payment_methods: str,
    payment_network: str | None = None,
    payment_token: str | None = None,
) -> X402Challenge:
    """
    Complete a raw x402 challenge with endpoint capability and preview metadata.

    Shared by both issuance points — the deferred gate's challenge branch and
    the pre-input challenge path — so the two cannot describe the same resource
    differently.  A caller comparing a challenge it obtained before validation
    with one it obtained after must see the same payable contract.

    `accepted_payment_methods` is supplied by the caller, resolved from the
    request's own payment-policy snapshot.  Resolving it here instead would
    read live policy during challenge composition, and a TTL refresh could then
    pair one snapshot's rule and amount with another snapshot's rails inside a
    single 402.

    The preview is schema-only metadata drawn from the static endpoint registry;
    it never contains live data and requires no data access to produce.
    """
    accepted_methods = accepted_payment_methods

    # Shallow-copy so a cached or reused enforcement result is never mutated.
    body = dict(challenge_body)
    body["accepted_payment_methods"] = accepted_methods.split(",")

    preview = get_endpoint_preview(
        path,
        pricing_rule_id=pricing_rule_id,
        stc_cost=f"{amount_usd:.6f}",
        effective_price_usd=f"{amount_usd:.6f}",
    )
    if preview is not None:
        body["stocktrends_preview"] = preview

    return X402Challenge(
        body=body,
        payment_required_header=payment_required_header,
        accepted_payment_methods=accepted_methods,
        payment_network=payment_network,
        payment_token=payment_token,
    )


def issue_x402_challenge(
    *,
    path: str,
    method: str,
    amount_usd: Decimal,
    pricing_rule_id: str | None,
    accepted_payment_methods: str,
    challenge_mode: str | None = None,
) -> X402Challenge:
    """
    Build the challenge for an unpaid recognized fixed-price resource.

    This is the whole of the early path's payment work.  `build_x402_challenge`
    is a pure function over the path, method, quoted amount and the static
    discovery registry: it emits the payment requirements, the canonical
    resource URL and the Bazaar extension without touching a facilitator, a
    session, a database or the endpoint.

    The challenge therefore describes the inputs the resource requires even
    though this request supplied none of them — which is exactly what a
    machine-discovery probe of a canonical URL needs, and what makes the
    resource indexable as payable.
    """
    challenge_body, payment_required_header = build_x402_challenge(
        path=path,
        amount_usd=amount_usd,
        method=method,
        challenge_mode=challenge_mode,
    )

    network, token = _requirement_context(challenge_body.get("payment_required"))

    return decorate_x402_challenge(
        path=path,
        method=method,
        challenge_body=challenge_body,
        payment_required_header=payment_required_header,
        pricing_rule_id=pricing_rule_id,
        amount_usd=amount_usd,
        accepted_payment_methods=accepted_payment_methods,
        payment_network=network,
        payment_token=token,
    )


def _requirement_context(payment_requirements: Any) -> tuple[str | None, str | None]:
    """The network and asset the challenge advertises, for the log rows."""
    if not isinstance(payment_requirements, dict):
        return None, None

    accepts = payment_requirements.get("accepts")
    if not isinstance(accepts, list) or not accepts or not isinstance(accepts[0], dict):
        return None, None

    requirement = accepts[0]
    return requirement.get("network"), requirement.get("asset")
