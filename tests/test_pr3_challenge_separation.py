"""
PR3 acceptance suite — x402 challenge issuance separated from settlement.

What changed, and why it is not a relaxation
--------------------------------------------
The payment-boundary remediation established:

    No deterministic client-input failure or route-miss condition knowable
    before paid service execution may cause x402 settlement.

It achieved that by moving the whole payment step behind FastAPI's structural
validation and the endpoint-local semantic validators.  That invariant is
untouched here and is re-asserted throughout this file: every payment-bearing
request in it that carries invalid input reaches zero facilitator calls, zero
MPP control-plane calls and zero data access.

What the earlier work also did, unintentionally, was move *challenge issuance*
behind validation.  A challenge moves no money — it quotes a price and
describes a resource — so gating it on request validity had a purely
informational cost, and that cost was externally visible.  Coinbase CDP Bazaar
validates a resource by probing its canonical URL without application
parameters; `GET /v1/prices/history` answered `400 missing_required_param`
before the payment contract existed, so the resource was indexed as
`returns_402: false`, `valid: false`.  `GET /v1/market/regime/latest`, which
needs no input, was indexed correctly — which is the whole diagnosis in two
requests.

The corrected state machine, and the single distinction it turns on:

    recognized fixed-price paid route
        payment proof present?
            NO  -> challenge only -> 402            (nothing verified/settled)
            YES -> FastAPI structural validation
                -> request-only semantic validation
                -> deferred payment gate
                -> endpoint                          (settles exactly once)

So the same incomplete request diverges on payment-bearing state alone:

    unpaid + incomplete          -> 402 challenge
    payment-bearing + incomplete -> 400/422, zero settlement

Measurement
-----------
As in `test_settlement_ordering.py`, status codes are never the whole assertion.
Non-settlement is asserted against the facilitator and MPP spies, and
non-execution against a query counter or a poisoned data-access sentinel: a 402
says nothing on its own about whether the endpoint ran or a rail was contacted.
"""

from __future__ import annotations

import base64
import json

import pytest
from support.payment_harness import (
    SENTINEL_STC_COST,
    SENTINEL_UNIT_PRICE_USD,
    counting_engine,
    mpp_headers,
    payment_governed_routes,
    rows_engine,
    unpaid_headers,
    v1_path,
    x402_headers,
)

import middleware.metering as metering_module
import payments.x402 as x402_module
import routers.decision as decision_router
import routers.prices as prices_router
import routers.screener as screener_router
import routers.stim as stim_router

_CANONICAL_BASE_URL = "https://api.stocktrends.com"

_PRICE_ROW = {
    "weekdate": "2026-01-02", "exchange": "N", "symbol": "IBM", "type": "CS",
    "currency_code": "USD", "price": 100.0, "adj_close": 100.0,
    "pr_week_hi": 101.0, "pr_week_lo": 99.0, "volume": 1000, "trades": 10,
    "split_fact": 1.0, "pr_change": 0.5,
}

# The production failure, as a URL: the canonical resource with no application
# parameters at all.  This is what CDP probes and what used to return 400.
_BARE_PRICES_HISTORY = "/v1/prices/history"
_VALID_PRICES_HISTORY = "/v1/prices/history?symbol_exchange=IBM-N"
_SEMANTIC_INVALID_PRICES = "/v1/prices/history?symbol_exchange=IBM"
_STRUCTURAL_INVALID_PRICES = "/v1/prices/history?symbol_exchange=IBM-N&limit=0"


@pytest.fixture
def priced_engines(monkeypatch):
    """Stub the router engines this suite touches with a benign result set."""
    for module in (prices_router, stim_router, screener_router):
        monkeypatch.setattr(module, "get_engine", lambda: rows_engine([_PRICE_ROW]))


@pytest.fixture
def canonical_base_url(monkeypatch):
    """
    Serve challenges with the production resource base URL.

    `X402_API_BASE_URL` is unset in the test environment, which makes
    `resource.url` a bare path.  CDP indexes the absolute canonical URL, so the
    regression case has to be asserted against the configuration production
    actually runs.
    """
    monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _CANONICAL_BASE_URL)


def _assert_nothing_moved(harness) -> None:
    """No rail was contacted, in either direction, by any means."""
    assert harness.verify_count == 0, "facilitator verify must not run"
    assert harness.settle_count == 0, "facilitator settle must not run"
    assert harness.mpp.authorize_count == 0, "MPP must not authorize"
    assert harness.mpp.capture_count == 0, "MPP must not capture"
    assert harness.mpp.void_count == 0, "MPP must not void"


def _decoded_payment_required_header(response) -> dict:
    """The `PAYMENT-REQUIRED` header, decoded as the x402 client would."""
    raw = response.headers["payment-required"]
    return json.loads(base64.b64decode(raw))


# ===========================================================================
# A — the exact CDP / Bazaar regression case
# ===========================================================================

def test_01_bare_prices_history_returns_a_valid_x402_challenge(
    payment_harness, priced_engines, canonical_base_url
):
    """
    The production failure, inverted into its fixed behaviour.

    `GET /v1/prices/history` with no `symbol_exchange` and no payment is the
    request CDP sends to decide whether the resource is payable.  It must now
    return a complete, valid x402 `402` — protocol version, the canonical
    absolute resource URL, and Bazaar metadata — rather than the input error
    that made the resource look unpayable.
    """
    response = payment_harness.client.get(
        _BARE_PRICES_HISTORY, headers=unpaid_headers()
    )

    assert response.status_code == 402, (
        "the canonical resource URL must be challengeable without application "
        "parameters, or machine discovery cannot see that it is payable"
    )

    body = response.json()
    assert body["error"] == "payment_required"

    requirements = body["payment_required"]
    assert requirements["x402Version"] == 2
    assert requirements["resource"]["url"] == f"{_CANONICAL_BASE_URL}{_BARE_PRICES_HISTORY}"
    assert "bazaar" in requirements["extensions"]

    # The header is the machine-readable half of the same challenge and must
    # agree with the body; an indexer reads one or the other.
    header = _decoded_payment_required_header(response)
    assert header["x402Version"] == 2
    assert header["resource"]["url"] == requirements["resource"]["url"]

    _assert_nothing_moved(payment_harness)


def _callable_input_schema(bazaar: dict) -> dict:
    """
    The schema describing the endpoint's own parameters, in either challenge mode.

    Full challenges put the callable parameters directly at
    `schema.properties.input`; compact challenges wrap them in the protocol
    envelope at `schema.properties.input.properties.queryParams` (or `body`).
    Both are legitimate Bazaar shapes and this suite asserts the contract, not
    the packaging.
    """
    input_schema = bazaar["schema"]["properties"]["input"]
    properties = input_schema.get("properties", {})
    for envelope_key in ("queryParams", "body"):
        if envelope_key in properties:
            return properties[envelope_key]
    return input_schema


@pytest.mark.parametrize("challenge_mode", ["full", "compact"])
def test_02_bare_challenge_describes_the_inputs_it_was_not_given(
    payment_harness, priced_engines, canonical_base_url, challenge_mode
):
    """
    The property that makes the challenge useful rather than merely present.

    The request supplied no `symbol_exchange`; the challenge still declares it
    as the required input.  That is the point of issuing the challenge before
    application-input validation — an indexer learns both the payment contract
    and the request contract from one probe of the canonical URL.

    Asserted in both challenge modes.  PR2 requires compact and full challenges
    to agree on callable request semantics, and a mode that dropped the required
    input would leave the indexer that reads it no better off than the 400 did.
    """
    headers = unpaid_headers()
    headers["X-StockTrends-Challenge-Mode"] = challenge_mode

    response = payment_harness.client.get(_BARE_PRICES_HISTORY, headers=headers)
    assert response.status_code == 402

    bazaar = response.json()["payment_required"]["extensions"]["bazaar"]
    input_schema = _callable_input_schema(bazaar)

    assert "symbol_exchange" in input_schema["required"], (
        f"[{challenge_mode}] the Bazaar input schema must state the real "
        "required input contract even though the probe supplied none of it"
    )
    assert "symbol_exchange" in input_schema["properties"], challenge_mode

    # And the Stock Trends preview carries the same contract in the body, which
    # is what a non-Bazaar agent reads.
    preview = response.json()["stocktrends_preview"]
    assert "symbol_exchange" in preview["required_inputs"], challenge_mode
    assert preview["safe_example_request"]["query"]["symbol_exchange"] == "IBM-N"


def test_03_payment_bearing_bare_request_is_an_input_error_not_a_settlement(
    payment_harness, monkeypatch, canonical_base_url
):
    """
    The complementary case, and the heart of PR3.

    The identical URL, presented with a valid payment artifact, must still be
    rejected on its inputs before anything can move money.  Two requests, one
    difference — payment-bearing state — and deliberately opposite outcomes.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    response = payment_harness.client.get(
        _BARE_PRICES_HISTORY, headers=x402_headers()
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "missing_required_param"

    assert len(queries) == 0, (
        f"the paid service executed {len(queries)} quer(ies) for a request "
        "rejected before payment"
    )
    _assert_nothing_moved(payment_harness)

    row = payment_harness.logs.only_economics_row()
    assert row["billed_amount_usd"] == 0
    assert row["payment_status"] == "rejected", (
        "a request denied before the gate is terminal and non-consuming"
    )


def test_04_the_pair_diverges_only_on_payment_bearing_state(
    payment_harness, priced_engines, canonical_base_url
):
    """
    Both halves in one test, so the distinction cannot be half-implemented.

    A regression that made unpaid probes return 400 again, or one that let
    payment-bearing malformed requests reach the gate, breaks exactly one of
    these two assertions.
    """
    unpaid = payment_harness.client.get(
        _BARE_PRICES_HISTORY, headers=unpaid_headers()
    )
    paying = payment_harness.client.get(
        _BARE_PRICES_HISTORY, headers=x402_headers()
    )

    assert unpaid.status_code == 402
    assert paying.status_code == 400
    _assert_nothing_moved(payment_harness)


# ===========================================================================
# B — unpaid probes of recognized fixed-price routes are challenged
# ===========================================================================

@pytest.mark.parametrize(
    ("label", "url"),
    [
        ("valid input", _VALID_PRICES_HISTORY),
        ("missing semantic input", _SEMANTIC_INVALID_PRICES),
        ("structurally malformed query", _STRUCTURAL_INVALID_PRICES),
        ("no input at all", _BARE_PRICES_HISTORY),
    ],
)
def test_05_unpaid_get_is_challenged_whatever_its_input_says(
    payment_harness, priced_engines, label, url
):
    """
    Input validity is no longer part of the challenge decision for an eligible
    route — only route recognition, policy eligibility and payment-bearing
    state are.
    """
    response = payment_harness.client.get(url, headers=unpaid_headers())

    assert response.status_code == 402, label
    assert response.json()["error"] == "payment_required", label
    assert "payment-required" in response.headers, label
    _assert_nothing_moved(payment_harness)


@pytest.mark.parametrize(
    "url",
    [
        "/v1/prices/history",
        "/v1/prices/latest",
        "/v1/indicators/latest",
        "/v1/indicators/history",
        "/v1/stim/latest",
        "/v1/stim/history",
        "/v1/market/regime/latest",
        "/v1/agent/screener/top",
    ],
)
def test_06_representative_fixed_price_get_routes_are_challengeable_bare(
    payment_harness, priced_engines, url
):
    """
    The six symbol-level resources CDP reported as invalid, plus the two that
    already worked, asserted as one class.

    `market/regime/latest` needs no input and was already indexable; including
    it proves the change generalized the behaviour rather than special-casing
    the routes that were broken.
    """
    response = payment_harness.client.get(url, headers=unpaid_headers())

    assert response.status_code == 402, url
    assert response.json()["payment_required"]["x402Version"] == 2, url
    _assert_nothing_moved(payment_harness)


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("missing body", {"json": {}}),
        ("schema-invalid body", {"json": {"count": "not-a-number"}}),
        (
            "malformed JSON body",
            {"content": b"{", "headers_extra": {"Content-Type": "application/json"}},
        ),
    ],
)
def test_07_unpaid_post_is_challenged_for_an_eligible_fixed_price_route(
    payment_harness, label, kwargs
):
    """
    Paid POST routes are eligible on the same terms as paid GET routes.

    A body that does not parse is the strongest form of the case: the challenge
    is built from route and policy knowledge alone, so it does not need the
    body to be readable, let alone valid.
    """
    headers = unpaid_headers()
    headers.update(kwargs.pop("headers_extra", {}))

    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol", headers=headers, **kwargs
    )

    assert response.status_code == 402, label
    assert response.json()["error"] == "payment_required", label
    _assert_nothing_moved(payment_harness)


# ===========================================================================
# C — payment-bearing requests keep the validated path, unchanged
# ===========================================================================

def test_08_payment_bearing_structural_error_never_settles(
    payment_harness, monkeypatch
):
    """Framework-level rejection, ahead of the gate, with nothing executed."""
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    response = payment_harness.client.get(
        _STRUCTURAL_INVALID_PRICES, headers=x402_headers()
    )

    assert response.status_code == 422
    assert len(queries) == 0
    _assert_nothing_moved(payment_harness)
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0


def test_09_payment_bearing_semantic_error_never_settles(payment_harness, monkeypatch):
    """Endpoint-local semantic rejection, ahead of the gate, nothing executed."""
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    response = payment_harness.client.get(
        _SEMANTIC_INVALID_PRICES, headers=x402_headers()
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_symbol_exchange"
    assert len(queries) == 0
    _assert_nothing_moved(payment_harness)
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0


def test_10_payment_bearing_malformed_post_body_never_settles(payment_harness):
    """
    The POST counterpart, on a route that is now early-challengeable.

    Making a route challengeable before validation must not make it settleable
    before validation; these are the two halves the design keeps apart.
    """
    headers = x402_headers()
    headers["Content-Type"] = "application/json"

    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol", headers=headers, content=b"{"
    )

    assert response.status_code == 422
    _assert_nothing_moved(payment_harness)


def test_11_payment_bearing_valid_request_settles_exactly_once(
    payment_harness, monkeypatch
):
    """The happy path, with endpoint execution measured rather than assumed."""
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    response = payment_harness.client.get(
        _VALID_PRICES_HISTORY, headers=x402_headers()
    )

    assert response.status_code == 200
    assert response.json()["symbol_exchange"] == "IBM-N"
    assert len(queries) >= 1, "the paid endpoint did not execute"
    assert payment_harness.verify_count == 1
    assert payment_harness.settle_count == 1
    assert "payment-response" in response.headers

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "settled"
    assert row["billed_amount_usd"] == SENTINEL_UNIT_PRICE_USD


def test_12_valid_paid_post_settles_exactly_once(payment_harness, monkeypatch):
    """The POST happy path on a route the early challenge also serves."""
    from support.payment_harness import sequence_engine

    monkeypatch.setattr(decision_router, "get_engine", lambda: sequence_engine([[], []]))

    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol",
        headers=x402_headers(),
        json={"symbol_exchange": "IBM-N"},
    )

    # The stub carries no weekdates, so the endpoint reaches its data-dependent
    # 503 — a failure discovered by paid execution, and therefore chargeable.
    assert response.status_code == 503
    assert payment_harness.verify_count == 1
    assert payment_harness.settle_count == 1


def test_13_gate_remains_one_shot_for_a_paid_request(payment_harness, priced_engines):
    """
    No duplicate verification or settlement.

    The early challenge does not replace the deferred gate, and adding a second
    issuance point must not have created a second settlement point.
    """
    response = payment_harness.client.get(
        _VALID_PRICES_HISTORY, headers=x402_headers()
    )

    assert response.status_code == 200
    assert payment_harness.verify_count == 1, "payment was verified more than once"
    assert payment_harness.settle_count == 1, "payment was settled more than once"


def test_14_early_challenge_publishes_no_payment_gate(
    payment_harness, priced_engines, monkeypatch
):
    """
    A challenge-only request never installs a gate, so there is nothing to
    invoke twice — and nothing that could later be mistaken for enforcement
    that ran.

    The paired paid request is the positive control: the settlement seam is
    still published where it was, for the requests that can reach it.
    """
    constructed: list[int] = []
    original = metering_module.DeferredPaymentGate

    class RecordingGate(original):  # type: ignore[misc, valid-type]
        def __init__(self, enforce):
            constructed.append(1)
            super().__init__(enforce)

    monkeypatch.setattr(metering_module, "DeferredPaymentGate", RecordingGate)

    challenge = payment_harness.client.get(
        _BARE_PRICES_HISTORY, headers=unpaid_headers()
    )
    assert challenge.status_code == 402
    assert not constructed, (
        "a payment gate was published for a challenge-only request; the "
        "finaliser would read its own challenge as a pre-gate rejection"
    )

    paid = payment_harness.client.get(_VALID_PRICES_HISTORY, headers=x402_headers())
    assert paid.status_code == 200
    assert len(constructed) == 1, (
        "the deferred gate is no longer published for a payment-bearing "
        "request; the settlement seam has moved"
    )


# ===========================================================================
# D — route recognition: a challenge is never issued for a path that is not one
# ===========================================================================

@pytest.mark.parametrize(
    "url",
    [
        "/v1/stim/latst",
        "/v1/prices/histry",
        "/v1/prices/history/extra",
        "/v1/does-not-exist",
    ],
)
def test_15_unknown_paths_are_never_challenged(payment_harness, url):
    """
    A typo under a paid prefix must not be quoted a price.

    Recognition comes from the application's own routers, so a path that
    resolves to no route cannot reach the challenge at all.
    """
    response = payment_harness.client.get(url, headers=unpaid_headers())

    assert response.status_code != 402, (
        f"{url} was challenged; the middleware is answering paid-looking URLs"
    )
    assert response.status_code in {401, 404}, url
    _assert_nothing_moved(payment_harness)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "/v1/prices/history"),
        ("POST", "/v1/stim/latest"),
        ("GET", "/v1/decision/evaluate-symbol"),
        ("DELETE", "/v1/market/regime/latest"),
    ],
)
def test_16_wrong_methods_are_never_challenged(payment_harness, method, url):
    """A method the route does not accept keeps its method/auth rejection."""
    response = payment_harness.client.request(method, url, headers=unpaid_headers())

    assert response.status_code != 402, (
        f"{method} {url} was challenged for a method the route does not serve"
    )
    assert response.status_code in {401, 404, 405}, f"{method} {url}"
    _assert_nothing_moved(payment_harness)


def test_17_route_recognition_agrees_with_the_dispatcher():
    """
    The recognizer is asserted against the application it describes.

    Its whole value is that it is not a second routing table, so it is tested
    against real routes, real method mismatches and a real mount descent.
    """
    import main
    from api.route_recognition import RouteRecognition, recognize_route

    def _scope(path: str, method: str) -> dict:
        return {
            "type": "http",
            "path": path,
            "method": method,
            "root_path": "",
            "headers": [],
            "query_string": b"",
        }

    matched = recognize_route(main.app, _scope("/v1/prices/history", "GET"))
    assert matched.recognition is RouteRecognition.API_ROUTE
    assert matched.route_template == "/v1/prices/history", (
        "the mount prefix must be restored, or the template cannot be compared "
        "with the externally addressable path"
    )

    parameterized = recognize_route(
        main.app, _scope("/v1/intelligence/guidance/some-id", "GET")
    )
    assert parameterized.recognition is RouteRecognition.API_ROUTE
    assert parameterized.route_template == "/v1/intelligence/guidance/{artifact_id}"

    assert recognize_route(main.app, _scope("/v1/prices/history", "POST")).recognition is (
        RouteRecognition.METHOD_NOT_ALLOWED
    )
    assert recognize_route(main.app, _scope("/v1/stim/latst", "GET")).recognition is (
        RouteRecognition.NOT_FOUND
    )
    # A trailing-slash variant is a redirect candidate, not a match.  Reporting
    # it as recognized would challenge a URL the dispatcher answers with a 307.
    assert recognize_route(main.app, _scope("/v1/prices/history/", "GET")).recognition is (
        RouteRecognition.NOT_FOUND
    )
    assert recognize_route(None, _scope("/v1/prices/history", "GET")).recognition is (
        RouteRecognition.NOT_FOUND
    )


# ===========================================================================
# E — paid-route classification: what is eligible, and what is deliberately not
# ===========================================================================

def test_18_every_governed_route_has_an_explicit_classification():
    """
    Classification is total, and every exclusion is named.

    A governed route that fell through the classifier without a reason would be
    an unaudited decision either way.  `FIXED_PRICE` is the only eligible class;
    everything else is a documented refusal.
    """
    from payments.challenge import (
        EarlyChallengeClass,
        classify_early_challenge_route,
    )
    from payments.policy_provider import get_effective_endpoint_payment_policy

    classified: dict[tuple[str, str], EarlyChallengeClass] = {}

    for route, method in payment_governed_routes():
        path = v1_path(route)
        decision = classify_early_challenge_route(
            path,
            method,
            endpoint_policy=get_effective_endpoint_payment_policy(path, method),
            route_template=f"/v1{route.path}",
        )
        assert isinstance(decision.challenge_class, EarlyChallengeClass)
        classified[(method, path)] = decision.challenge_class

    assert classified, "no governed routes were classified; the audit is vacuous"

    eligible = {
        key for key, value in classified.items()
        if value is EarlyChallengeClass.FIXED_PRICE
    }
    assert len(eligible) >= 20, (
        f"only {len(eligible)} route(s) classified fixed-price; the early "
        "challenge would cover almost nothing"
    )


def test_19_paid_intelligence_artifact_routes_are_excluded_as_availability_gated():
    """
    The documented exclusion, asserted by reason rather than by path list.

    Paid Intelligence artifact routes confirm the artifact store is reachable
    and the artifact exists before any payment challenge, and answer 503/404
    when it is not.  Challenging them before that gate would quote a price for
    a product the system cannot serve.
    """
    from payments.challenge import (
        EarlyChallengeClass,
        classify_early_challenge_route,
    )
    from payments.policy_provider import get_effective_endpoint_payment_policy

    excluded = {
        EarlyChallengeClass.AVAILABILITY_GATED,
        EarlyChallengeClass.PARAMETERIZED_RESOURCE,
    }

    for path, template in (
        ("/v1/intelligence/guidance/latest", "/v1/intelligence/guidance/latest"),
        ("/v1/intelligence/research/latest", "/v1/intelligence/research/latest"),
        (
            "/v1/intelligence/guidance/market_guidance:N:2026-04-11:guidance:aff9aaeee1660a31",
            "/v1/intelligence/guidance/{artifact_id}",
        ),
        (
            "/v1/intelligence/research/market_research_report:N:2026-04-11:research:2a7d870d628448a0",
            "/v1/intelligence/research/{artifact_id}",
        ),
    ):
        decision = classify_early_challenge_route(
            path,
            "GET",
            endpoint_policy=get_effective_endpoint_payment_policy(path, "GET"),
            route_template=template,
        )
        assert decision.challenge_class in excluded, (
            f"{path} classified {decision.challenge_class}; a paid Intelligence "
            "artifact route must never be challenged ahead of its availability gate"
        )


def test_20_intelligence_availability_gate_still_precedes_any_challenge(
    payment_harness, monkeypatch
):
    """
    The exclusion, measured through the real HTTP boundary.

    With no artifact store configured, an unpaid probe of a paid Intelligence
    route must still fail closed rather than be handed a challenge.
    """
    from services.intelligence_artifact_store import STORE_ENV_VAR

    monkeypatch.delenv(STORE_ENV_VAR, raising=False)

    response = payment_harness.client.get(
        "/v1/intelligence/guidance/latest", headers=unpaid_headers()
    )

    assert response.status_code == 503
    assert response.status_code != 402
    _assert_nothing_moved(payment_harness)


def test_21_prefix_governed_routes_without_an_exact_policy_are_not_eligible():
    """
    Prefix governance is not enough to quote a price.

    `/v1/stim` is an enforcement prefix, so a route under it is payment-governed
    without any exact endpoint policy naming it — and therefore without a
    pricing rule that fixes an amount.  Treating that as challengeable would
    make the middleware a "paid-looking URL -> 402" mechanism.
    """
    from payments.challenge import (
        EarlyChallengeClass,
        classify_early_challenge_route,
    )
    from payments.policy_provider import (
        get_effective_endpoint_payment_policy,
        is_agent_pay_enforcement_path,
    )

    probe = "/v1/stim/_pr3_probe"

    assert is_agent_pay_enforcement_path(probe, "GET"), (
        "the probe path is not prefix-governed; this test proves nothing"
    )

    decision = classify_early_challenge_route(
        probe,
        "GET",
        endpoint_policy=get_effective_endpoint_payment_policy(probe, "GET"),
        route_template=probe,
    )
    assert decision.challenge_class is EarlyChallengeClass.NO_ENDPOINT_POLICY


# ===========================================================================
# F — a challenge is not a settlement, in the economics
# ===========================================================================

def test_22_challenge_only_request_collects_nothing(payment_harness, priced_engines):
    """
    The accounting shape of an issued challenge.

    `pending` is a live request an agent may still pay for — deliberately not
    `presented` (which the billing runbook counts as usage), not `rejected`
    (which is terminal), and not `settled`.  The quoted price and the STC
    measure are still recorded, because they are a price and a measure, not a
    claim of collection.
    """
    payment_harness.client.get(_BARE_PRICES_HISTORY, headers=unpaid_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "pending"
    assert row["billed_amount_usd"] == 0, "a challenge collected nothing"
    assert row["payment_reference"] is None, "a challenge references no payment"
    assert row["payment_amount_native"] is None
    assert row["payment_amount_usd"] is None
    assert row["unit_price_usd"] == SENTINEL_UNIT_PRICE_USD
    assert row["stc_cost"] == SENTINEL_STC_COST


def test_23_challenge_only_request_is_not_recorded_as_success(
    payment_harness, priced_engines
):
    """The request event records a challenge, never a delivered paid result."""
    payment_harness.client.get(_BARE_PRICES_HISTORY, headers=unpaid_headers())

    event = payment_harness.logs.only_event_row()
    assert event["status_code"] == 402
    assert event["success"] == 0
    assert event["error_code"] == "payment_required"
    assert event["payment_rail"] == "x402"
    assert event["payment_network"], "the challenge's network context was lost"
    assert event["payment_token"], "the challenge's token context was lost"


def test_24_challenge_only_request_is_distinguishable_from_every_payment_state(
    payment_harness, priced_engines
):
    """
    A challenge must not be confusable with a verification failure, a
    settlement failure, or a settlement.

    Asserted as a set of distinct statuses rather than one at a time, because
    the risk is collapse between them rather than any single wrong value.
    """
    observed: dict[str, str] = {}

    payment_harness.client.get(_BARE_PRICES_HISTORY, headers=unpaid_headers())
    observed["challenge"] = payment_harness.logs.only_economics_row()["payment_status"]

    payment_harness.logs.economics.clear()
    payment_harness.facilitator.verify_valid = False
    payment_harness.client.get(_VALID_PRICES_HISTORY, headers=x402_headers())
    observed["verification failure"] = payment_harness.logs.only_economics_row()["payment_status"]

    payment_harness.logs.economics.clear()
    payment_harness.facilitator.verify_valid = True
    payment_harness.facilitator.settle_valid = False
    payment_harness.client.get(_VALID_PRICES_HISTORY, headers=x402_headers())
    observed["settlement failure"] = payment_harness.logs.only_economics_row()["payment_status"]

    payment_harness.logs.economics.clear()
    payment_harness.facilitator.settle_valid = True
    payment_harness.client.get(_VALID_PRICES_HISTORY, headers=x402_headers())
    observed["settlement"] = payment_harness.logs.only_economics_row()["payment_status"]

    payment_harness.logs.economics.clear()
    payment_harness.client.get(_SEMANTIC_INVALID_PRICES, headers=x402_headers())
    observed["endpoint client error"] = payment_harness.logs.only_economics_row()["payment_status"]

    assert observed["challenge"] == "pending"
    assert observed["settlement"] == "settled"
    assert len(set(observed.values())) == len(observed), (
        f"two payment states share one status and cannot be told apart: {observed}"
    )
    assert observed["challenge"] not in {
        observed["verification failure"],
        observed["settlement failure"],
        observed["settlement"],
        observed["endpoint client error"],
    }, f"a challenge is indistinguishable from another payment state: {observed}"


# ===========================================================================
# G — behavioural purity: issuing a challenge touches no data
# ===========================================================================

class UnpaidDataAccess(RuntimeError):
    """A challenge-only request reached for data or a service."""


@pytest.fixture
def poisoned_data_access(monkeypatch):
    """
    Replace every router data entry point with a recording sentinel.

    `test_23` asserts that no *money* moved.  This asserts the other half: that
    no paid work was done either.  A challenge built from the static discovery
    registry needs no engine, so any touch here means challenge generation
    started doing endpoint work.
    """
    import importlib

    touched: list[str] = []
    targets: list[tuple[object, str]] = []

    modules = {
        importlib.import_module(route.endpoint.__module__)
        for route, _method in payment_governed_routes()
    }
    for module in modules:
        for name in ("get_engine", "configured_intelligence_artifact_store"):
            if hasattr(module, name):
                targets.append((module, name))

    for module, name in targets:
        label = f"{module.__name__}.{name}"

        def sentinel(*_args, _label=label, **_kwargs):
            touched.append(_label)
            raise UnpaidDataAccess(f"{_label} was called for an unpaid request")

        monkeypatch.setattr(module, name, sentinel)

    assert len(targets) > 10, (
        f"only {len(targets)} data entry point(s) poisoned; the guard is vacuous"
    )
    return touched


@pytest.mark.parametrize(
    "url",
    [
        "/v1/prices/history",
        "/v1/prices/history?symbol_exchange=IBM",
        "/v1/indicators/latest",
        "/v1/stim/latest",
        "/v1/agent/screener/top",
    ],
)
def test_25_issuing_a_challenge_performs_no_data_access(
    payment_harness, poisoned_data_access, url
):
    """No database, no service, no artifact store — for any challenged probe."""
    response = payment_harness.client.get(url, headers=unpaid_headers())

    assert response.status_code == 402, url
    assert not poisoned_data_access, (
        f"{url}: challenge generation reached "
        f"{sorted(set(poisoned_data_access))}"
    )


def test_26_the_poison_fixture_actually_bites(payment_harness, poisoned_data_access):
    """
    Positive control.

    `test_25` asserts an absence, which a fixture patching the wrong names would
    satisfy perfectly.  A paid request must reach the endpoint and trip a
    sentinel.
    """
    with pytest.raises(UnpaidDataAccess):
        payment_harness.client.get(_VALID_PRICES_HISTORY, headers=x402_headers())

    assert "routers.prices.get_engine" in poisoned_data_access


# ===========================================================================
# H — the other rails are untouched
# ===========================================================================

def test_27_free_routes_are_unchanged(payment_harness):
    """A public discovery surface neither pays nor is challenged."""
    for url in ("/v1/ai/tools", "/v1/pricing/catalog", "/v1/meta/stim"):
        response = payment_harness.client.get(url, headers=unpaid_headers())

        assert response.status_code == 200, url
        assert response.headers.get("x-stocktrends-payment-required") == "false", url

    _assert_nothing_moved(payment_harness)


@pytest.fixture
def subscription_client(payment_harness, monkeypatch):
    """A caller authenticated by API key on a paid plan."""
    import middleware.api_key as api_key_module

    monkeypatch.setattr(
        api_key_module.ApiKeyMiddleware,
        "_authenticate_api_key",
        lambda _self, _path, _key: (
            True,
            {
                "api_key_id": "key-1",
                "customer_id": "cust-1",
                "subscription_id": "sub-1",
                "plan_code": "pro",
                "actor_type": "external_customer",
                "monthly_quota": 100000,
            },
        ),
    )
    return payment_harness


def test_28_subscription_requests_are_never_challenged(
    subscription_client, priced_engines
):
    """
    A quota-backed caller is not an unpaid probe.

    The valid request is served and the invalid one keeps its input error — the
    subscription rail sees no x402 anywhere, before or after PR3.
    """
    served = subscription_client.client.get(
        _VALID_PRICES_HISTORY, headers={"X-API-Key": "test-key"}
    )
    assert served.status_code == 200

    rejected = subscription_client.client.get(
        _SEMANTIC_INVALID_PRICES, headers={"X-API-Key": "test-key"}
    )
    assert rejected.status_code == 400, (
        "a subscription caller was handed a payment challenge for a request it "
        "is already entitled to make"
    )
    assert rejected.status_code != 402

    _assert_nothing_moved(subscription_client)


def test_29_bare_canonical_probe_on_subscription_is_not_challenged(
    subscription_client, priced_engines
):
    """The bare URL too: an entitled caller gets its input error, not a price."""
    response = subscription_client.client.get(
        _BARE_PRICES_HISTORY, headers={"X-API-Key": "test-key"}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "missing_required_param"
    _assert_nothing_moved(subscription_client)


def test_30_mpp_lifecycle_is_unchanged(payment_harness, priced_engines):
    """
    MPP authorizes and captures exactly as before.

    MPP is session-based rather than challenge/response, and an MPP caller
    always presents payment material, so it is never an unpaid canonical probe
    and the early path must never intercept it.
    """
    response = payment_harness.client.get(_VALID_PRICES_HISTORY, headers=mpp_headers())

    assert response.status_code == 200
    assert payment_harness.mpp.authorize_count == 1
    assert payment_harness.mpp.capture_count == 1
    assert payment_harness.mpp.void_count == 0
    assert payment_harness.settle_count == 0
    assert payment_harness.logs.only_economics_row()["payment_status"] == "captured"


def test_31_mpp_invalid_request_still_reaches_no_control_plane(
    payment_harness, priced_engines
):
    """The MPP rejection ordering is untouched: no authorize, no capture, no void."""
    response = payment_harness.client.get(
        _SEMANTIC_INVALID_PRICES, headers=mpp_headers()
    )

    assert response.status_code == 400
    assert response.status_code != 402, (
        "an MPP request was answered with an x402 challenge; the rails have been "
        "collapsed"
    )
    assert payment_harness.mpp.authorize_count == 0
    assert payment_harness.mpp.capture_count == 0
    assert payment_harness.mpp.void_count == 0


def test_32_mpp_bare_canonical_probe_is_not_challenged(payment_harness, priced_engines):
    """
    An MPP caller on the bare URL keeps MPP semantics.

    It must not be handed an x402 challenge in place of its session-based
    rejection: that would describe the wrong rail to a caller that already has
    a session.
    """
    response = payment_harness.client.get(_BARE_PRICES_HISTORY, headers=mpp_headers())

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "missing_required_param"
    assert payment_harness.mpp.authorize_count == 0


# ===========================================================================
# I — one definition of a challenge
# ===========================================================================

def test_33_both_issuance_points_compose_the_same_challenge(canonical_base_url):
    """
    A challenge obtained before validation and one obtained at the gate must
    describe the same payable contract.

    Two independent compositions could disagree about accepted methods, the
    preview, or the resource identity, and an agent comparing them would see
    two different resources at one URL.  Asserted by building both.
    """
    from decimal import Decimal

    from payments.challenge import decorate_x402_challenge, issue_x402_challenge
    from payments.enforcement import enforce_x402_payment

    path, method = "/v1/prices/history", "GET"
    amount = Decimal("0.15")
    rule = "prices_history_paid"

    gate_result = enforce_x402_payment(
        headers={},
        path=path,
        method=method,
        amount_usd=amount,
        validation_valid=True,
        validation_error=None,
        validation_detail=None,
        validated_payment_reference=None,
        validated_payment_network=None,
        validated_payment_token=None,
        validated_payment_amount_native=None,
        replay_checker=lambda _reference: False,
    )
    assert gate_result.outcome == "challenge"

    accepted = "subscription,x402,mpp"

    via_gate = decorate_x402_challenge(
        path=path,
        method=method,
        challenge_body=gate_result.challenge_body,
        payment_required_header=gate_result.payment_required_header,
        pricing_rule_id=rule,
        amount_usd=amount,
        accepted_payment_methods=accepted,
        payment_network=gate_result.payment_network,
        payment_token=gate_result.payment_token,
    )
    via_early = issue_x402_challenge(
        path=path,
        method=method,
        amount_usd=amount,
        pricing_rule_id=rule,
        accepted_payment_methods=accepted,
    )

    assert via_early.body == via_gate.body
    assert via_early.payment_required_header == via_gate.payment_required_header
    assert via_early.accepted_payment_methods == via_gate.accepted_payment_methods
    assert via_early.payment_network == via_gate.payment_network
    assert via_early.payment_token == via_gate.payment_token


def test_33b_the_published_precondition_matches_the_runtime(payment_harness):
    """
    The OpenAPI contract's `serviceable_request_required_before_challenge` is
    checked against what the route actually does.

    That field is what an x402 client reads to decide whether probing a bare
    canonical URL is worthwhile, and a stale `true` on it is precisely the claim
    that made CDP stop looking.  So it is not asserted against the classifier
    that produced it — that would be circular — but against a real bare probe.
    """
    schema = payment_harness.client.get("/v1/openapi.json").json()

    probed = 0
    for path, path_item in schema["paths"].items():
        operation = path_item.get("get")
        if not isinstance(operation, dict):
            continue
        payment = operation.get("x-stocktrends-payment")
        if not payment or payment.get("serviceable_request_required_before_challenge"):
            continue
        if "{" in path:
            continue

        response = payment_harness.client.get(f"/v1{path}", headers=unpaid_headers())
        assert response.status_code == 402, (
            f"/v1{path} publishes "
            "serviceable_request_required_before_challenge=false but a bare "
            f"unpaid probe returned {response.status_code}"
        )
        probed += 1

    assert probed >= 15, (
        f"only {probed} operation(s) published the relaxed precondition; the "
        "contract-to-runtime binding is close to vacuous"
    )
    _assert_nothing_moved(payment_harness)


def test_34_challenge_issuance_cannot_reach_the_facilitator():
    """
    Structural guard on the separation itself.

    `payments.challenge` is the issuance half and must never acquire the ability
    to verify or settle.  A future edit that imported the facilitator functions
    here would put settlement back in front of validation, which is exactly the
    defect the payment-boundary work removed.
    """
    import inspect

    import payments.challenge as challenge_module

    source = inspect.getsource(challenge_module)
    for forbidden in (
        "verify_with_facilitator",
        "settle_with_facilitator",
        "authorize_mpp_payment",
        "capture_mpp_payment",
        "enforce_payment_rail",
        "get_engine",
    ):
        assert forbidden not in source, (
            f"payments.challenge references {forbidden}; challenge issuance must "
            "remain incapable of moving money or doing paid work"
        )


def test_35_only_real_x402_proof_counts_as_payment():
    """
    The discriminator the whole design turns on, stated exactly.

    Two earlier revisions were too broad, and both failed the same way: a caller
    holding no authorization was classified payment-bearing, skipped the
    challenge, and received an application input error for a bare canonical
    probe — the exact failure PR3 exists to remove.

    The first accepted any descriptive Stock Trends payment header.  The second
    accepted `Authorization: x402 …`, which no part of verify/settle consumes as
    an artifact and which the published contract does not advertise.

    Only the published `X402_PROOF_HEADERS` carriers count.  Rail identification
    is a separate question and lives in `is_x402_payment_method`;
    `test_35c` pins that separation.
    """
    from payments.challenge import presents_x402_payment_proof
    from payments.x402_contract import X402_PROOF_HEADERS

    # Nothing at all.
    assert not presents_x402_payment_proof(None)
    assert not presents_x402_payment_proof({})
    assert not presents_x402_payment_proof({"x-stocktrends-agent-id": "agent"})

    # Descriptive metadata is not authorization.
    for header in (
        "X-StockTrends-Payment-Reference",
        "X-StockTrends-Payment-Amount",
        "X-StockTrends-Payment-Network",
        "X-StockTrends-Payment-Token",
        "X-StockTrends-Payment-Channel-Id",
        "X-StockTrends-Payment-Method",
    ):
        assert not presents_x402_payment_proof({header: "something"}), (
            f"{header} was treated as x402 payment proof; a caller that merely "
            "describes a payment would have its challenge suppressed"
        )
        assert not presents_x402_payment_proof({header.lower(): "something"}), header

    # Every published proof carrier does count, in either casing.
    assert X402_PROOF_HEADERS, "the proof-header contract is empty"
    for header in X402_PROOF_HEADERS:
        assert presents_x402_payment_proof({header: "artifact"}), header
        assert presents_x402_payment_proof({header.lower(): "artifact"}), header

    # `Authorization: x402` is a rail hint, not an artifact.  The enforcement
    # path reads neither `has_payment_signature` nor `extract_payment_signature`
    # from it, so accepting it here would mean the guard and enforcement
    # disagreed about whether the very same request had paid.
    assert not presents_x402_payment_proof({"authorization": "x402 artifact"}), (
        "Authorization: x402 was treated as payment proof; verify/settle never "
        "consumes it as an artifact, so this caller is unpaid and needs the "
        "challenge"
    )
    assert not presents_x402_payment_proof({"Authorization": "x402 artifact"})
    assert not presents_x402_payment_proof({"authorization": "Bearer token"})


def test_35c_proof_and_rail_identification_are_separate_questions():
    """
    Rail identification stays broad; payment proof stays exact.

    `is_x402_payment_method` answers "which rail is this on?" and legitimately
    reads a declared method and an `Authorization: x402` hint.  Narrowing the
    proof predicate must not have narrowed rail selection with it, because rail
    resolution is pre-existing behaviour the early-challenge guard depends on.
    """
    from payments.x402 import has_x402_payment_proof, is_x402_payment_method

    hint = {"Authorization": "x402 something"}
    assert is_x402_payment_method(hint), (
        "the Authorization rail hint stopped identifying the x402 rail"
    )
    assert not has_x402_payment_proof(hint)

    declared = {"x-stocktrends-payment-method": "x402"}
    assert is_x402_payment_method(declared)
    assert not has_x402_payment_proof(declared)

    artifact = {"X-Payment": "an-artifact"}
    assert is_x402_payment_method(artifact)
    assert has_x402_payment_proof(artifact)

    assert not is_x402_payment_method({"x-stocktrends-payment-method": "mpp"})
    assert not has_x402_payment_proof({"x-stocktrends-payment-method": "mpp"})


def test_35b_the_guard_has_no_proof_header_list_of_its_own():
    """
    Structural guard: one definition of an x402 authorization carrier.

    A second list inside the challenge layer would drift from the one the
    facilitator path reads, and the drift would be invisible until a paying
    client was handed a challenge or an unpaid one was handed an input error.

    Asserted against executable code rather than the whole file, so prose that
    *describes* the contract is allowed while a header literal the guard would
    actually read is not.
    """
    import ast
    import inspect

    import payments.challenge as challenge_module

    tree = ast.parse(inspect.getsource(challenge_module))

    # Every docstring node, so prose can describe the contract freely.
    docstrings = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
        ):
            value = node.body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                docstrings.add(id(value))

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]

    joined = " ".join(literals).lower()
    for header in ("x-payment", "payment-signature", "x-stocktrends-payment-"):
        assert header not in joined, (
            f"payments.challenge carries the header literal {header!r} in "
            "executable code; the proof contract must come from payments.x402 "
            "alone or the two definitions will drift"
        )

    guard_source = inspect.getsource(challenge_module.presents_x402_payment_proof)
    assert "has_x402_payment_proof(" in guard_source, (
        "the early-challenge guard no longer delegates to the canonical proof "
        "predicate"
    )


def test_36_the_challenge_advertises_every_supported_rail(
    payment_harness, priced_engines
):
    """
    The multi-rail boundary survives.

    An x402 challenge is the transport that issued it, not the whole price:
    subscription and MPP remain available for the same STC price, and the
    challenge must keep saying so.
    """
    response = payment_harness.client.get(
        _BARE_PRICES_HISTORY, headers=unpaid_headers()
    )

    assert response.status_code == 402
    assert set(response.json()["accepted_payment_methods"]) == {
        "subscription",
        "x402",
        "mpp",
    }
    assert response.headers["x-stocktrends-accepted-payment-methods"] == (
        "subscription,x402,mpp"
    )
    assert response.headers["x-stocktrends-pricing-rule"] == "prices_history_paid"
    assert response.headers["x-stocktrends-payment-required"] == "true"
    assert response.headers["cache-control"] == "no-store, private"
