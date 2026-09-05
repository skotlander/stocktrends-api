"""
Codex-review remediation suite for PR3.

Five findings, each reproduced as the reviewer found it and then pinned closed:

1. P1 — one request could mix two payment-policy snapshots, emitting a `402`
   whose pricing rule and amount came from configuration A and whose accepted
   rails came from configuration B.
2. P2 — descriptive Stock Trends payment headers were treated as x402 payment
   proof, so a caller that merely *described* a payment had its challenge
   suppressed and received an input error instead.
3. P1 — the `api_request_logs` row marked a challenge-only request billable,
   because billability was derived from the pre-execution `PricingDecision`
   rather than from what a rail actually collected.
4. P2 — published lifecycle guidance contradicted itself: the tools manifest
   still said a `402` follows request validation, while the plugin metadata
   published an unqualified global "no serviceable request required".
5. P1 (pre-existing, elevated) — a missing, inactive or unreadable pricing row
   collapsed to `Decimal("0")`, which a payment-required resource could then
   publish and enforce as a zero-dollar x402 challenge.

Plus one payment-integrity hole found while remediating finding 5: a
payment-required request declaring an unsupported `X-StockTrends-Payment-Method`
resolved to no enforceable rail and fell straight through the gate, serving paid
data for nothing.

As elsewhere in this repository, status codes are never the whole assertion.
Non-settlement is measured against the facilitator and MPP spies, and
non-execution against a query counter.
"""

from __future__ import annotations

import time
from dataclasses import replace
from decimal import Decimal

import pytest
from support.payment_harness import (
    AGENT_HEADERS,
    counting_engine,
    mpp_headers,
    rows_engine,
    unpaid_headers,
    x402_headers,
)

import middleware.api_key as api_key_module
import middleware.metering as metering_module
import routers.prices as prices_router
import routers.stim as stim_router
from middleware.metering import PriceResolution, ResolvedPrice
from payments import policy_provider

#: Captured at import, before any fixture replaces it, so a test can put the
#: genuine resolver back and exercise a real catalogue failure through it.
_REAL_RESOLVE_REQUEST_PRICING = metering_module.resolve_request_pricing

_BARE = "/v1/prices/history"
_VALID = "/v1/prices/history?symbol_exchange=IBM-N"
_SEMANTIC_INVALID = "/v1/prices/history?symbol_exchange=IBM"
_STRUCTURAL_INVALID = "/v1/prices/history?symbol_exchange=IBM-N&limit=0"

_PRICE_ROW = {
    "weekdate": "2026-01-02", "exchange": "N", "symbol": "IBM", "type": "CS",
    "currency_code": "USD", "price": 100.0, "adj_close": 100.0,
    "pr_week_hi": 101.0, "pr_week_lo": 99.0, "volume": 1000, "trades": 10,
    "split_fact": 1.0, "pr_change": 0.5,
}


@pytest.fixture
def priced_engines(monkeypatch):
    for module in (prices_router, stim_router):
        monkeypatch.setattr(module, "get_engine", lambda: rows_engine([_PRICE_ROW]))


def _assert_nothing_moved(harness) -> None:
    assert harness.verify_count == 0, "facilitator verify must not run"
    assert harness.settle_count == 0, "facilitator settle must not run"
    assert harness.mpp.authorize_count == 0, "MPP must not authorize"
    assert harness.mpp.capture_count == 0, "MPP must not capture"
    assert harness.mpp.void_count == 0, "MPP must not void"


# ===========================================================================
# FINDING 1 — one request, one payment-policy snapshot
# ===========================================================================

def _two_snapshots():
    """
    Two observably different payment policies for `/v1/prices/history`.

    They differ in the two things that must never be seen together in one
    response: the pricing rule the amount is resolved from, and the rails the
    challenge advertises.
    """
    base = policy_provider._default_policy_config()

    def _with(rule_id: str, rails: tuple[str, ...]):
        policies = tuple(
            replace(policy, pricing_rule_id=rule_id, allowed_rails=rails)
            if policy.path_pattern == _BARE and policy.method == "GET"
            else policy
            for policy in base.endpoint_payment_policies
        )
        return replace(base, endpoint_payment_policies=policies, ttl_seconds=0)

    snapshot_a = _with("snapshot_a_rule", ("subscription", "x402", "mpp"))
    snapshot_b = _with("snapshot_b_rule", ("x402",))
    return snapshot_a, snapshot_b


@pytest.fixture
def refreshing_policy(monkeypatch):
    """
    A control plane that hands out a *different* snapshot on every read.

    This is the Codex reproduction, sharpened: rather than relying on a short
    TTL and hoping a refresh lands mid-request, every single call returns the
    next configuration.  Any code path that reads policy more than once per
    request is therefore guaranteed to see two, and the coherence assertions
    below become a real measurement rather than a race.
    """
    snapshot_a, snapshot_b = _two_snapshots()
    reads: list[str] = []
    sequence = [snapshot_a, snapshot_b]

    def _next(*_args, **_kwargs):
        config = sequence[min(len(reads), len(sequence) - 1)]
        reads.append(config.endpoint_payment_policies[0].pricing_rule_id)
        # Alternate for every subsequent read so the hazard never subsides.
        sequence.append(snapshot_a if config is snapshot_b else snapshot_b)
        return config

    monkeypatch.setattr(policy_provider, "get_runtime_payment_policy_config", _next)
    return {"a": snapshot_a, "b": snapshot_b, "reads": reads}


def _rule_for(snapshot) -> str:
    for policy in snapshot.endpoint_payment_policies:
        if policy.path_pattern == _BARE and policy.method == "GET":
            return policy.pricing_rule_id
    raise AssertionError("prices/history policy missing from the snapshot")


def _rails_for(snapshot) -> set[str]:
    for policy in snapshot.endpoint_payment_policies:
        if policy.path_pattern == _BARE and policy.method == "GET":
            return set(policy.allowed_rails)
    raise AssertionError("prices/history policy missing from the snapshot")


def test_01_one_challenge_never_mixes_two_policy_snapshots(
    payment_harness, priced_engines, refreshing_policy, monkeypatch
):
    """
    The reproduction, and the invariant.

    The response must be coherent with snapshot A *or* with snapshot B.  A
    challenge quoting A's pricing rule while advertising B's rails describes a
    resource that never existed in either configuration, and an agent that pays
    against it has paid for something the system never offered.
    """
    seen_rules: list[str] = []
    monkeypatch.setattr(
        metering_module,
        "resolve_request_pricing",
        lambda rule_name: (
            seen_rules.append(rule_name),
            ResolvedPrice.priced(Decimal("0.15"), Decimal("0.15")),
        )[1],
    )

    response = payment_harness.client.get(_BARE, headers=unpaid_headers())

    assert response.status_code == 402
    assert refreshing_policy["reads"], "the policy provider was never consulted"

    body = response.json()
    advertised = set(body["accepted_payment_methods"])
    quoted_rule = response.headers["x-stocktrends-pricing-rule"]

    assert seen_rules, "pricing was never resolved"
    assert quoted_rule == seen_rules[-1], (
        "the pricing rule in the response header differs from the one the "
        "amount was resolved against"
    )

    coherent = [
        snapshot
        for snapshot in (refreshing_policy["a"], refreshing_policy["b"])
        if _rule_for(snapshot) == quoted_rule and _rails_for(snapshot) == advertised
    ]
    assert coherent, (
        "the challenge mixed payment-policy snapshots: it quoted rule "
        f"{quoted_rule!r} while advertising rails {sorted(advertised)}. "
        f"Snapshot A is ({_rule_for(refreshing_policy['a'])}, "
        f"{sorted(_rails_for(refreshing_policy['a']))}); snapshot B is "
        f"({_rule_for(refreshing_policy['b'])}, "
        f"{sorted(_rails_for(refreshing_policy['b']))})"
    )
    _assert_nothing_moved(payment_harness)


def test_01b_the_snapshot_is_bound_once_and_reused(payment_harness, priced_engines):
    """
    The mechanism, asserted directly rather than only through its effect.

    One request binds exactly one snapshot object, and every later ask returns
    that identical object — not an equal one.
    """
    bound: list[object] = []
    original = api_key_module.payment_policy_snapshot_for_request

    def _recording(request_state):
        snapshot = original(request_state)
        bound.append(snapshot)
        return snapshot

    api_key_module.payment_policy_snapshot_for_request = _recording
    try:
        response = payment_harness.client.get(_BARE, headers=unpaid_headers())
    finally:
        api_key_module.payment_policy_snapshot_for_request = original

    assert response.status_code == 402
    assert bound, "ApiKeyMiddleware did not bind a policy snapshot"
    assert all(snapshot is bound[0] for snapshot in bound), (
        "the snapshot accessor returned more than one object for one request"
    )


def test_01c_api_key_and_metering_share_one_snapshot(payment_harness, priced_engines):
    """
    Both middlewares answer to the same configuration.

    ApiKeyMiddleware decides anonymous agent-pay entry from payment policy, and
    MeteringMiddleware decides enforcement and eligibility from it.  If they
    could read different snapshots, a request could be admitted as agent-pay
    under one configuration and priced or refused under another.

    Measured by identity: the object the auth layer bound is the object the
    metering layer used to answer its own policy question.
    """
    observed: dict[str, object] = {}

    original_bind = api_key_module.payment_policy_snapshot_for_request
    original_policy = metering_module.get_effective_endpoint_payment_policy_from_config

    def _recording_bind(request_state):
        snapshot = original_bind(request_state)
        observed.setdefault("api_key", snapshot)
        return snapshot

    def _recording_policy(config, path, method=None):
        observed["metering"] = config
        return original_policy(config, path, method)

    api_key_module.payment_policy_snapshot_for_request = _recording_bind
    metering_module.get_effective_endpoint_payment_policy_from_config = _recording_policy
    try:
        response = payment_harness.client.get(_BARE, headers=unpaid_headers())
    finally:
        api_key_module.payment_policy_snapshot_for_request = original_bind
        metering_module.get_effective_endpoint_payment_policy_from_config = original_policy

    assert response.status_code == 402
    assert "api_key" in observed, "ApiKeyMiddleware bound no snapshot"
    assert "metering" in observed, "MeteringMiddleware asked no policy question"
    assert observed["metering"] is observed["api_key"], (
        "MeteringMiddleware answered a policy question against a different "
        "snapshot than the one ApiKeyMiddleware admitted the request under"
    )


def test_01d_challenge_composition_performs_no_policy_lookup(
    payment_harness, priced_engines, monkeypatch
):
    """
    The issuance layer holds no live policy handle.

    `payments.challenge` is handed the accepted rails its caller already
    resolved.  If it read policy itself, a refresh during composition could pair
    one snapshot's amount with another's rails — and a layer documented as
    performing no configuration I/O would be doing exactly that.
    """
    import payments.challenge as challenge_module

    calls: list[str] = []
    monkeypatch.setattr(
        policy_provider,
        "get_runtime_payment_policy_config",
        lambda *a, **kw: (calls.append("policy"), policy_provider._default_policy_config())[1],
    )

    # Composition in isolation: no request, no middleware, no policy read.
    challenge = challenge_module.issue_x402_challenge(
        path=_BARE,
        method="GET",
        amount_usd=Decimal("0.15"),
        pricing_rule_id="prices_history_paid",
        accepted_payment_methods="subscription,x402,mpp",
    )

    assert challenge.accepted_payment_methods == "subscription,x402,mpp"
    assert not calls, (
        "challenge composition read live payment policy; the accepted rails "
        "must come from the caller's request-scoped snapshot"
    )

    source = __import__("inspect").getsource(challenge_module)
    for forbidden in (
        "get_runtime_payment_policy_config",
        "get_accepted_payment_methods_for_path",
    ):
        assert forbidden not in source, (
            f"payments.challenge references {forbidden}; the issuance layer must "
            "not resolve payment policy of its own"
        )


def test_01e_from_config_helpers_agree_with_their_convenience_wrappers():
    """
    Snapshot-aware helpers and their convenience wrappers are one definition.

    The wrappers exist for non-request callers and must be exactly "read the
    current config, then ask the snapshot-aware helper".  Two implementations
    would let request processing and tooling disagree about the same policy.
    """
    config = policy_provider.get_runtime_payment_policy_config()

    cases = [
        ("is_free_metered_path", ("/v1/ai/context",), {}),
        ("is_agent_pay_route", (_BARE, "GET"), {}),
        ("get_agent_pay_auth_bypass_methods", (_BARE, "GET"), {}),
        ("is_agent_pay_enforcement_path", (_BARE, "GET"), {}),
        ("get_allowed_payment_rails_for_path", (_BARE, "GET"), {}),
    ]
    for name, args, kwargs in cases:
        wrapper = getattr(policy_provider, name)
        snapshot_aware = getattr(policy_provider, f"{name}_from_config")
        assert wrapper(*args, **kwargs) == snapshot_aware(config, *args, **kwargs), name

    assert policy_provider.get_accepted_payment_methods_for_path(
        _BARE, "prices_history_paid", method="GET"
    ) == policy_provider.get_accepted_payment_methods_for_path_from_config(
        config, _BARE, "prices_history_paid", method="GET"
    )

    assert policy_provider.is_agent_pay_auth_candidate(
        _BARE, "x402", "agent-1", method="GET"
    ) == policy_provider.is_agent_pay_auth_candidate_from_config(
        config, _BARE, "x402", "agent-1", method="GET"
    )


@pytest.mark.parametrize(
    ("label", "url", "headers_factory"),
    [
        ("unpaid challenge", _BARE, unpaid_headers),
        ("payment-bearing settlement", _VALID, x402_headers),
        ("MPP capture", _VALID, mpp_headers),
        ("unknown path", "/v1/does-not-exist", unpaid_headers),
    ],
)
def test_01g_a_request_reads_payment_policy_exactly_once(
    payment_harness, priced_engines, monkeypatch, label, url, headers_factory
):
    """
    The strongest statement of the fix: mixing is impossible by construction.

    Coherence assertions can only sample the hazard; a read count of exactly one
    removes it.  If the control plane is consulted once per request, there is no
    second snapshot for any decision to drift onto — whatever the TTL does.
    """
    reads: list[int] = []
    real = policy_provider.get_runtime_payment_policy_config
    monkeypatch.setattr(
        policy_provider,
        "get_runtime_payment_policy_config",
        lambda *a, **kw: (reads.append(1), real(*a, **kw))[1],
    )

    payment_harness.client.get(url, headers=headers_factory())

    assert len(reads) == 1, (
        f"{label}: payment policy was read {len(reads)} times in one request; "
        "every read after the first is a snapshot the request could drift onto"
    )


def test_01h_published_documents_are_built_from_one_snapshot(
    payment_harness, monkeypatch
):
    """
    A published contract cannot describe half its resources under one
    configuration and the rest under another.

    The tools manifest previously resolved payment policy once per tool, so a
    refresh mid-build produced a document that was internally inconsistent.
    """
    reads: list[int] = []
    real = policy_provider.get_runtime_payment_policy_config
    monkeypatch.setattr(
        policy_provider,
        "get_runtime_payment_policy_config",
        lambda *a, **kw: (reads.append(1), real(*a, **kw))[1],
    )

    response = payment_harness.client.get("/v1/ai/tools", headers=unpaid_headers())
    assert response.status_code == 200
    assert len(reads) == 1, (
        f"the tools manifest read payment policy {len(reads)} times while "
        "building one document"
    )


def test_01f_a_ttl_expiry_between_layers_cannot_split_a_request(
    payment_harness, priced_engines, monkeypatch
):
    """
    The original reproduction shape: a genuinely expiring cache.

    Rather than replacing the accessor, this lets the real TTL machinery run
    with `ttl_seconds=0` so every read genuinely re-fetches.  The response must
    still be internally coherent.
    """
    snapshot_a, snapshot_b = _two_snapshots()
    fetched: list[object] = []

    def _fetch():
        config = snapshot_b if fetched else snapshot_a
        fetched.append(config)
        return config

    monkeypatch.setattr(policy_provider, "_fetch_runtime_payment_policy_config", _fetch)
    monkeypatch.setattr(policy_provider, "_cached_config", None)
    monkeypatch.setattr(policy_provider, "_cached_at", 0.0)
    monkeypatch.setattr(policy_provider, "_last_known_good_config", None)
    monkeypatch.setenv("PAYMENT_POLICY_CONFIG_URL", "https://control-plane.invalid/policy")

    seen_rules: list[str] = []
    monkeypatch.setattr(
        metering_module,
        "resolve_request_pricing",
        lambda rule_name: (
            seen_rules.append(rule_name),
            ResolvedPrice.priced(Decimal("0.15"), Decimal("0.15")),
        )[1],
    )

    time.sleep(0)  # ttl_seconds=0 means every read is already stale
    response = payment_harness.client.get(_BARE, headers=unpaid_headers())

    assert response.status_code == 402
    advertised = set(response.json()["accepted_payment_methods"])
    quoted_rule = response.headers["x-stocktrends-pricing-rule"]

    coherent = [
        snapshot
        for snapshot in (snapshot_a, snapshot_b)
        if _rule_for(snapshot) == quoted_rule and _rails_for(snapshot) == advertised
    ]
    assert coherent, (
        f"a TTL expiry split the request: rule {quoted_rule!r} advertised with "
        f"rails {sorted(advertised)}"
    )


# ===========================================================================
# FINDING 2 — informational headers are not payment proof
# ===========================================================================

_METADATA_ONLY_HEADERS = [
    ("network", {"X-StockTrends-Payment-Network": "eip155:8453"}),
    ("token", {"X-StockTrends-Payment-Token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}),
    ("amount", {"X-StockTrends-Payment-Amount": "150000"}),
    ("reference", {"X-StockTrends-Payment-Reference": "ref-without-an-artifact"}),
    ("channel id", {"X-StockTrends-Payment-Channel-Id": "chan-1"}),
    ("method declaration", {"X-StockTrends-Payment-Method": "x402"}),
]


@pytest.mark.parametrize(
    ("label", "extra_headers"),
    _METADATA_ONLY_HEADERS,
    ids=[case[0] for case in _METADATA_ONLY_HEADERS],
)
def test_02_metadata_only_headers_never_suppress_the_challenge(
    payment_harness, monkeypatch, label, extra_headers
):
    """
    Describing a payment is not making one.

    Each of these headers states something *about* an intended payment while
    presenting no x402 authorization artifact.  Treating any of them as proof
    sent the caller down the payment-bearing path, where a bare probe is
    answered with an input error — which is precisely the discovery failure PR3
    exists to remove.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    headers = dict(AGENT_HEADERS)
    headers.update(extra_headers)

    response = payment_harness.client.get(_BARE, headers=headers)

    assert response.status_code == 402, label
    assert response.json()["error"] == "payment_required", label
    assert "payment-required" in response.headers, label
    assert response.json()["payment_required"]["x402Version"] == 2, label
    assert len(queries) == 0, f"{label}: the paid endpoint executed"
    _assert_nothing_moved(payment_harness)


def test_02b_all_metadata_headers_together_still_do_not_pay(
    payment_harness, priced_engines
):
    """Every descriptive header at once is still not an authorization."""
    headers = dict(AGENT_HEADERS)
    for _label, extra in _METADATA_ONLY_HEADERS:
        headers.update(extra)

    response = payment_harness.client.get(_BARE, headers=headers)

    assert response.status_code == 402
    assert response.json()["error"] == "payment_required"
    _assert_nothing_moved(payment_harness)


def test_02c_real_proof_with_incomplete_input_still_wins(payment_harness, monkeypatch):
    """
    Positive control: an actual artifact takes the validated path.

    The corrected discriminator must not have made *everything* unpaid — a
    caller presenting real proof still gets its input error, and still settles
    nothing.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    response = payment_harness.client.get(_BARE, headers=x402_headers())

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "missing_required_param"
    assert len(queries) == 0
    _assert_nothing_moved(payment_harness)


def test_02d_valid_mpp_authorization_is_never_intercepted(payment_harness, priced_engines):
    """
    MPP keeps its rail.

    The early challenge speaks only for x402, and an MPP request resolves to the
    MPP rail — so narrowing the x402 proof discriminator must not have pulled a
    session-based caller into a per-request challenge.
    """
    response = payment_harness.client.get(_VALID, headers=mpp_headers())

    assert response.status_code == 200
    assert payment_harness.mpp.authorize_count == 1
    assert payment_harness.mpp.capture_count == 1
    assert payment_harness.settle_count == 0
    assert payment_harness.logs.only_economics_row()["payment_status"] == "captured"


def test_02e_incomplete_mpp_request_is_not_answered_with_an_x402_challenge(
    payment_harness, priced_engines
):
    """An MPP caller missing required headers keeps its MPP-shaped rejection."""
    headers = dict(AGENT_HEADERS)
    headers["X-StockTrends-Payment-Method"] = "mpp"

    response = payment_harness.client.get(_VALID, headers=headers)

    assert response.status_code == 402
    assert response.json()["error"] != "payment_required", (
        "an incomplete MPP request was answered with an x402 challenge; the "
        "rails have been collapsed"
    )
    assert payment_harness.mpp.authorize_count == 0


def test_02f_semantic_invalid_mpp_request_still_rejects_before_the_control_plane(
    payment_harness, priced_engines
):
    """The MPP ordering guarantee is untouched by the narrowed discriminator."""
    response = payment_harness.client.get(_SEMANTIC_INVALID, headers=mpp_headers())

    assert response.status_code == 400
    assert payment_harness.mpp.authorize_count == 0
    assert payment_harness.mpp.capture_count == 0
    assert payment_harness.mpp.void_count == 0


# ===========================================================================
# FINDING 3 — a challenge is not billable execution
# ===========================================================================

def _event_billability(harness) -> int:
    return harness.logs.only_event_row()["is_billable"]


def test_03_early_challenge_is_not_billable(payment_harness, priced_engines):
    """
    The finding, exactly.

    HTTP 402, `success = 0`, `payment_status = pending`, `billed = 0`, no
    payment reference — and yet `api_request_logs.is_billable` said `1`.
    """
    payment_harness.client.get(_BARE, headers=unpaid_headers())

    assert _event_billability(payment_harness) == 0
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0


def test_03b_gate_issued_challenge_is_not_billable(payment_harness):
    """
    The same claim for the other issuance point.

    `/v1/stim/_billability_probe` is prefix-governed with no exact endpoint
    policy, so it is not early-challenge eligible and its challenge comes from
    the deferred gate.  Both issuance points must report identically.
    """
    from test_settlement_ordering import temporary_v1_route

    with temporary_v1_route("/stim/_billability_probe", wrap=True):
        response = payment_harness.client.get(
            "/v1/stim/_billability_probe", headers=unpaid_headers()
        )

    assert response.status_code == 402
    assert response.json()["error"] == "payment_required"
    assert _event_billability(payment_harness) == 0
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0


@pytest.mark.parametrize(
    ("label", "headers_factory", "setup", "url"),
    [
        ("pre-gate input rejection", x402_headers, None, _SEMANTIC_INVALID),
        ("structural rejection", x402_headers, None, _STRUCTURAL_INVALID),
        (
            "verification failure",
            x402_headers,
            lambda h: setattr(h.facilitator, "verify_valid", False),
            _VALID,
        ),
        (
            "settlement failure",
            x402_headers,
            lambda h: setattr(h.facilitator, "settle_valid", False),
            _VALID,
        ),
        (
            "replay",
            lambda: x402_headers(reference="already-spent"),
            lambda h: h.mark_reference_used("already-spent"),
            _VALID,
        ),
        ("underpaid artifact", lambda: x402_headers(amount="1"), None, _VALID),
        ("malformed artifact", lambda: {**AGENT_HEADERS, "X-Payment": "garbage"}, None, _VALID),
    ],
)
def test_03c_uncollected_x402_outcomes_are_never_billable(
    payment_harness, priced_engines, label, headers_factory, setup, url
):
    """Every x402 state in which no money moved reports `is_billable = 0`."""
    if setup is not None:
        setup(payment_harness)

    payment_harness.client.get(url, headers=headers_factory())

    assert _event_billability(payment_harness) == 0, label
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0, label


def test_03d_settled_x402_is_billable(payment_harness, priced_engines):
    """Positive control: collection is what makes a machine-payment row billable."""
    response = payment_harness.client.get(_VALID, headers=x402_headers())

    assert response.status_code == 200
    assert payment_harness.settle_count == 1
    assert _event_billability(payment_harness) == 1
    assert payment_harness.logs.only_economics_row()["payment_status"] == "settled"


def test_03e_captured_mpp_is_billable(payment_harness, priced_engines):
    """
    The ordering consequence, measured.

    MPP capture happens after the response is known.  A request event built
    before capture cannot see the collection, so the event build had to move
    behind it — this is the test that fails if it moves back.
    """
    response = payment_harness.client.get(_VALID, headers=mpp_headers())

    assert response.status_code == 200
    assert payment_harness.mpp.capture_count == 1
    assert _event_billability(payment_harness) == 1
    assert payment_harness.logs.only_economics_row()["payment_status"] == "captured"


def test_03f_failed_mpp_capture_is_not_billable(payment_harness, priced_engines):
    """A capture the control plane refused collected nothing."""
    payment_harness.mpp.capture_success = False

    payment_harness.client.get(_VALID, headers=mpp_headers())

    assert _event_billability(payment_harness) == 0
    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "capture_failed"
    assert row["billed_amount_usd"] == 0


def test_03g_failed_mpp_authorization_is_not_billable(payment_harness, priced_engines):
    """Nothing was reserved, so nothing was consumed."""
    payment_harness.mpp.authorize_success = False

    payment_harness.client.get(_VALID, headers=mpp_headers())

    assert _event_billability(payment_harness) == 0
    assert payment_harness.mpp.capture_count == 0
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0


def test_03h_voided_mpp_authorization_is_not_billable(payment_harness, monkeypatch):
    """
    An authorization opened and then compensated collected nothing.

    The endpoint is made to fail after authorization so the finaliser voids,
    which is the only way to reach the void branch through the real stack.
    """
    monkeypatch.setattr(prices_router, "get_engine", lambda: rows_engine([]))

    response = payment_harness.client.get(
        "/v1/prices/latest?symbol_exchange=ZZZZ-N", headers=mpp_headers()
    )

    assert response.status_code == 404
    assert payment_harness.mpp.authorize_count == 1
    assert payment_harness.mpp.void_count == 1
    assert payment_harness.mpp.capture_count == 0
    assert _event_billability(payment_harness) == 0
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0


@pytest.fixture
def subscription_client(payment_harness, monkeypatch):
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


def test_03i_served_subscription_request_remains_billable(
    subscription_client, priced_engines
):
    """
    Subscription semantics are deliberately untouched.

    A quota-backed request collects nothing per request — the collection
    happened at subscription time — so it must keep its existing billable
    behaviour rather than being swept into the collection rule.
    """
    response = subscription_client.client.get(_VALID, headers={"X-API-Key": "test-key"})

    assert response.status_code == 200
    assert _event_billability(subscription_client) == 1
    row = subscription_client.logs.only_economics_row()
    assert row["payment_rail"] == "subscription"
    assert row["billed_amount_usd"] == 0


def test_03j_free_routes_remain_non_billable(payment_harness):
    """Free and free-metered behaviour is unchanged."""
    response = payment_harness.client.get("/v1/ai/tools", headers=unpaid_headers())

    assert response.status_code == 200
    assert payment_harness.logs.only_event_row()["is_billable"] == 0


def test_03k_billability_is_not_inferred_from_the_status_code():
    """
    The rule is stated over payment state, not over HTTP.

    A 402 is issued for a challenge, a replay and a settlement failure alike,
    and a settled request can end in a 404 the caller still paid for.  Asserted
    as a unit so a future `if status_code == 402` shortcut is caught.
    """
    import inspect

    from middleware.metering import resolved_is_billable
    from pricing.classifier import PricingDecision

    paid = PricingDecision(
        is_metered=1,
        access_granted=True,
        deny_reason=None,
        log_pricing_rule_id="prices_history_paid",
        log_payment_method="x402",
        econ_pricing_rule_id="prices_history_paid",
        econ_payment_required=1,
        econ_payment_status="pending",
        econ_payment_method="x402",
    )
    assert resolved_is_billable(paid, collected=True) == 1
    assert resolved_is_billable(paid, collected=False) == 0

    subscription = replace(paid, econ_payment_required=0, econ_payment_method="subscription")
    assert resolved_is_billable(subscription, collected=False) == 1, (
        "subscription billability must not depend on per-request collection"
    )

    denied = replace(paid, access_granted=False)
    assert resolved_is_billable(denied, collected=True) == 0

    source = inspect.getsource(resolved_is_billable)
    assert "status_code" not in source, (
        "billability is being inferred from the HTTP status; it must be derived "
        "from payment and execution state"
    )


# ===========================================================================
# FINDING 4 — cross-surface lifecycle parity
# ===========================================================================

_FIXED_PRICE = ("GET", "/v1/prices/history")
_AVAILABILITY_GATED = ("GET", "/v1/intelligence/guidance/latest")
_PARAMETERIZED = ("GET", "/v1/intelligence/guidance/{artifact_id}")


def _openapi_lifecycle(client, method: str, path: str) -> dict:
    schema = client.get("/v1/openapi.json").json()
    operation = schema["paths"][path.removeprefix("/v1")][method.lower()]
    return operation["x-stocktrends-payment"]["challenge_lifecycle"]


def _discovery_lifecycle(client, method: str, path: str) -> dict:
    manifest = client.get("/.well-known/x402").json()
    for resource in manifest["resources"]:
        if resource["method"] == method and resource["path"] == path:
            return resource["challenge_lifecycle"]
    raise AssertionError(f"{method} {path} missing from the x402 manifest")


def _tools_lifecycle(client, path: str) -> dict | None:
    manifest = client.get("/v1/ai/tools").json()
    for tool in manifest["tools"]:
        if tool.get("endpoint") == path:
            return tool.get("challenge_lifecycle")
    return None


@pytest.mark.parametrize(
    ("label", "method", "path", "expected_class", "requires_serviceable"),
    [
        ("fixed price", *_FIXED_PRICE, "fixed_price", False),
        ("availability gated", *_AVAILABILITY_GATED, "availability_gated", True),
        # By-id artifact routes are both availability-gated and parameterized.
        # `challenge_class` names the reason the resource is excluded from early
        # challenge, which for these is availability; `parameterized_resource`
        # is reported independently in `test_04b`.
        ("parameterized", *_PARAMETERIZED, "availability_gated", True),
    ],
)
def test_04_lifecycle_classification_agrees_across_surfaces(
    payment_harness, label, method, path, expected_class, requires_serviceable
):
    """
    One classification, rendered everywhere.

    A global boolean cannot state this contract truthfully, because two of the
    three classes are exceptions to it.  Each surface must therefore publish the
    resource's own class, and all of them must agree — an agent that reads the
    OpenAPI extension and an indexer that reads the manifest must not be told
    different things about the same URL.
    """
    surfaces = {
        "openapi": _openapi_lifecycle(payment_harness.client, method, path),
        "x402_discovery": _discovery_lifecycle(payment_harness.client, method, path),
    }

    for surface, lifecycle in surfaces.items():
        assert lifecycle["challenge_class"] == expected_class, f"{label}/{surface}"
        assert lifecycle["serviceable_request_required_before_challenge"] is (
            requires_serviceable
        ), f"{label}/{surface}"
        assert lifecycle["serviceable_request_required_before_settlement"] is True, (
            f"{label}/{surface}: settlement always requires a serviceable request"
        )
        assert lifecycle["bare_canonical_probe_returns_challenge"] is (
            not requires_serviceable
        ), f"{label}/{surface}"

    assert surfaces["openapi"] == surfaces["x402_discovery"], (
        f"{label}: the OpenAPI and x402 discovery surfaces disagree"
    )


def test_04b_availability_and_parameterized_flags_are_specific(payment_harness):
    """Each exception is named as itself, not lumped into one 'not eligible'."""
    gated = _discovery_lifecycle(payment_harness.client, *_AVAILABILITY_GATED)
    assert gated["availability_gate_precedes_challenge"] is True
    assert gated["parameterized_resource"] is False

    parameterized = _discovery_lifecycle(payment_harness.client, *_PARAMETERIZED)
    assert parameterized["parameterized_resource"] is True, (
        "a by-id route publishes no parameterization flag, so a client cannot "
        "tell it has no bare canonical URL to probe"
    )
    assert parameterized["availability_gate_precedes_challenge"] is True, (
        "a by-id artifact route is availability-gated as well as parameterized; "
        "reporting only one understates the other"
    )

    fixed = _discovery_lifecycle(payment_harness.client, *_FIXED_PRICE)
    assert fixed["availability_gate_precedes_challenge"] is False
    assert fixed["parameterized_resource"] is False


def test_04c_tools_manifest_publishes_the_same_lifecycle(payment_harness):
    """The manifest agents are told to fetch first carries the same contract."""
    lifecycle = _tools_lifecycle(payment_harness.client, "/v1/prices/history")
    assert lifecycle is not None, "/v1/prices/history publishes no challenge_lifecycle"
    assert lifecycle == _openapi_lifecycle(payment_harness.client, *_FIXED_PRICE)

    free = _tools_lifecycle(payment_harness.client, "/v1/ai/tools")
    assert free is None, (
        "a free tool was given a payment precondition it has no use for"
    )


def test_04d_global_prose_is_qualified_not_universal(payment_harness):
    """
    No surface asserts a universal that has documented exceptions.

    The plugin metadata and the discovery manifest both previously published a
    bare `serviceable_request_required_before_challenge: false`, which is false
    for availability-gated and parameterized resources.
    """
    plugin = payment_harness.client.get("/.well-known/ai-plugin.json").json()
    access = plugin["x_stocktrends_access"]

    assert "serviceable_request_required_before_challenge" not in access, (
        "the plugin metadata still publishes an unqualified global boolean"
    )
    assert access["serviceable_request_required_before_challenge_for_fixed_price"] is False
    assert access["serviceable_request_required_before_settlement"] is True
    assert "fixed-price" in access["serviceable_request_required_before_challenge_scope"]
    assert set(access["per_resource_lifecycle"]) == {"openapi", "x402_discovery"}

    manifest = payment_harness.client.get("/.well-known/x402").json()
    lifecycle = manifest["request_lifecycle"]
    assert "serviceable_request_required_before_challenge" not in lifecycle
    assert lifecycle["per_resource_precondition_field"] == "resources[].challenge_lifecycle"


def test_04e_tools_flow_no_longer_claims_a_402_follows_validation(payment_harness):
    """
    The contradictory quickstart line, corrected.

    `/v1/ai/tools` told agents a 402 arrives "after request validation", which
    stopped being true for eligible fixed-price resources at PR3.
    """
    manifest = payment_harness.client.get("/v1/ai/tools").json()

    flow = " ".join(manifest["recommended_first_call"]["expected_flow"]).lower()
    assert "402 after request validation" not in flow, (
        "the expected flow still claims a challenge follows request validation"
    )
    assert "fixed-price" in flow, (
        "the expected flow does not say which resources are challengeable bare"
    )

    quickstart = " ".join(step.get("note", "") for step in manifest["quickstart"]).lower()
    assert "challenge_lifecycle" in quickstart, (
        "the quickstart does not point agents at the per-resource contract"
    )


def test_04f_published_lifecycle_matches_runtime(payment_harness, priced_engines):
    """
    The contract is checked against behaviour, not against its own generator.

    A resource that publishes `bare_canonical_probe_returns_challenge: true`
    must actually answer a bare probe with a challenge.
    """
    fixed = _openapi_lifecycle(payment_harness.client, *_FIXED_PRICE)
    assert fixed["bare_canonical_probe_returns_challenge"] is True

    response = payment_harness.client.get(_BARE, headers=unpaid_headers())
    assert response.status_code == 402

    gated = _openapi_lifecycle(payment_harness.client, *_AVAILABILITY_GATED)
    assert gated["bare_canonical_probe_returns_challenge"] is False

    gated_response = payment_harness.client.get(
        "/v1/intelligence/guidance/latest", headers=unpaid_headers()
    )
    assert gated_response.status_code in {404, 503}, (
        "an availability-gated resource was challenged ahead of its gate"
    )


# ===========================================================================
# FINDING 5 — an unresolved price must never become a zero-dollar challenge
# ===========================================================================

@pytest.fixture
def pricing_rule_missing(monkeypatch):
    """No active catalogue row for the rule the endpoint policy names."""
    monkeypatch.setattr(
        metering_module,
        "resolve_request_pricing",
        lambda _rule: ResolvedPrice.unresolved(PriceResolution.RULE_NOT_FOUND),
    )


@pytest.fixture
def pricing_lookup_failing(monkeypatch):
    """The pricing catalogue itself cannot be consulted."""

    def _boom(_rule_name):
        raise RuntimeError("pricing database unreachable")

    # The genuine resolver is restored so the failure travels through the real
    # `resolve_request_pricing` implementation rather than a stub of it.
    monkeypatch.setattr(metering_module, "get_active_pricing_rule", _boom)
    monkeypatch.setattr(
        metering_module, "resolve_request_pricing", _REAL_RESOLVE_REQUEST_PRICING
    )


@pytest.fixture
def pricing_row_is_zero(monkeypatch):
    """
    A catalogue row that resolves successfully but quotes nothing.

    Distinct from a failed lookup, and equally unusable: a payment-required
    resource cannot verify or settle against zero.
    """
    monkeypatch.setattr(
        metering_module,
        "resolve_request_pricing",
        lambda _rule: ResolvedPrice.priced(Decimal(0), Decimal(0)),
    )


@pytest.mark.parametrize(
    "pricing_failure",
    ["pricing_rule_missing", "pricing_lookup_failing", "pricing_row_is_zero"],
)
def test_05_unpaid_probe_fails_closed_when_the_price_is_unusable(
    payment_harness, monkeypatch, request, pricing_failure
):
    """
    The elevated pre-existing defect.

    An exact endpoint policy naming a pricing rule does not prove a usable price
    was resolved.  Publishing a zero-amount `402` would advertise a
    payment-required resource as free, and any agent honouring it would receive
    paid data for nothing.
    """
    request.getfixturevalue(pricing_failure)
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    response = payment_harness.client.get(_BARE, headers=unpaid_headers())

    assert response.status_code == 503, pricing_failure
    assert response.json()["error"] == "pricing_unavailable", pricing_failure
    assert "payment-required" not in response.headers, (
        f"{pricing_failure}: a PAYMENT-REQUIRED header was published for a "
        "resource whose price could not be resolved"
    )
    assert "payment_required" not in response.json(), pricing_failure
    assert response.headers["x-stocktrends-accepted-payment-methods"] == "none", (
        f"{pricing_failure}: rails were advertised for an unpayable resource"
    )
    assert len(queries) == 0, f"{pricing_failure}: the paid endpoint executed"
    _assert_nothing_moved(payment_harness)

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "rejected", pricing_failure
    assert row["billed_amount_usd"] == 0, pricing_failure
    assert payment_harness.logs.only_event_row()["is_billable"] == 0, pricing_failure


@pytest.mark.parametrize(
    "pricing_failure",
    ["pricing_rule_missing", "pricing_lookup_failing", "pricing_row_is_zero"],
)
def test_05b_payment_bearing_valid_request_fails_closed_before_settlement(
    payment_harness, monkeypatch, request, pricing_failure
):
    """
    A paying caller is refused rather than charged an unknown amount.

    Verification, settlement and MPP authorization all sit behind this check, so
    an unresolvable price cannot be settled against and cannot serve paid work.
    """
    request.getfixturevalue(pricing_failure)
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    response = payment_harness.client.get(_VALID, headers=x402_headers())

    assert response.status_code == 503, pricing_failure
    assert response.json()["error"] == "pricing_unavailable", pricing_failure
    assert len(queries) == 0, f"{pricing_failure}: the paid endpoint executed"
    _assert_nothing_moved(payment_harness)
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0


@pytest.mark.parametrize(
    "pricing_failure",
    ["pricing_rule_missing", "pricing_lookup_failing", "pricing_row_is_zero"],
)
def test_05c_malformed_payment_bearing_input_still_wins_over_pricing(
    payment_harness, monkeypatch, request, pricing_failure
):
    """
    Ordering preserved: the input error comes first.

    Price resolution is captured early as data but acted on at the gate, which
    the request only reaches after structural and semantic validation.  A
    malformed paying request therefore still learns what is wrong with its
    request rather than being told the service cannot price it.
    """
    request.getfixturevalue(pricing_failure)
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    semantic = payment_harness.client.get(_SEMANTIC_INVALID, headers=x402_headers())
    assert semantic.status_code == 400, pricing_failure
    assert semantic.json()["detail"]["error"] == "invalid_symbol_exchange", pricing_failure

    structural = payment_harness.client.get(_STRUCTURAL_INVALID, headers=x402_headers())
    assert structural.status_code == 422, pricing_failure

    assert len(queries) == 0
    _assert_nothing_moved(payment_harness)


@pytest.mark.parametrize(
    "pricing_failure",
    ["pricing_rule_missing", "pricing_lookup_failing", "pricing_row_is_zero"],
)
def test_05d_mpp_never_authorizes_against_an_unusable_price(
    payment_harness, monkeypatch, request, pricing_failure
):
    """MPP reserves nothing when the amount to reserve is unknown."""
    request.getfixturevalue(pricing_failure)
    monkeypatch.setattr(prices_router, "get_engine", lambda: rows_engine([_PRICE_ROW]))

    response = payment_harness.client.get(_VALID, headers=mpp_headers())

    assert response.status_code == 503, pricing_failure
    assert payment_harness.mpp.authorize_count == 0, pricing_failure
    assert payment_harness.mpp.capture_count == 0, pricing_failure


def test_05e_positive_control_a_real_price_behaves_normally(
    payment_harness, priced_engines
):
    """
    The guard must reject unpriceable resources, not priced ones.

    Without this, every assertion above would pass just as well if the guard
    refused everything.
    """
    challenge = payment_harness.client.get(_BARE, headers=unpaid_headers())
    assert challenge.status_code == 402
    assert challenge.json()["payment_required"]["x402Version"] == 2

    settled = payment_harness.client.get(_VALID, headers=x402_headers())
    assert settled.status_code == 200
    assert payment_harness.settle_count == 1


def test_05f_resolution_state_is_explicit_not_a_bare_zero():
    """
    "Costs nothing" and "could not be priced" are different facts.

    Encoding both as `Decimal("0")` is what allowed a failed lookup to be
    published as a payment requirement of zero.
    """
    missing = ResolvedPrice.unresolved(PriceResolution.RULE_NOT_FOUND)
    failed = ResolvedPrice.unresolved(PriceResolution.LOOKUP_FAILED)
    genuinely_free = ResolvedPrice.priced(Decimal(0), Decimal(0))
    priced = ResolvedPrice.priced(Decimal("0.15"), Decimal("0.15"))

    assert missing.unit_price_usd == genuinely_free.unit_price_usd, (
        "the amounts coincide, which is exactly why the state must be explicit"
    )
    assert missing.resolution is not genuinely_free.resolution
    assert missing.failure_reason == "pricing_rule_not_found"
    assert failed.failure_reason == "pricing_lookup_failed"
    assert genuinely_free.failure_reason is None

    assert not missing.usable_for_machine_payment
    assert not failed.usable_for_machine_payment
    assert not genuinely_free.usable_for_machine_payment, (
        "a resolved zero is still not an amount a rail may act on"
    )
    assert priced.usable_for_machine_payment


def test_05g_a_missing_rule_name_is_its_own_state():
    """A free path has no rule to resolve, and that is not a failure."""
    resolved = metering_module.resolve_request_pricing(None)
    assert resolved.resolution is PriceResolution.NO_RULE_NAME
    assert not resolved.usable_for_machine_payment
    assert resolved.unit_price_usd == Decimal(0)


def test_05h_free_and_subscription_paths_are_unaffected_by_pricing_failure(
    subscription_client, priced_engines, pricing_rule_missing
):
    """
    The guard is scoped to direct machine payment.

    A quota-backed caller and a free surface must not be taken down by a paid
    resource's pricing row being unavailable.
    """
    free = subscription_client.client.get("/v1/ai/tools", headers=unpaid_headers())
    assert free.status_code == 200

    served = subscription_client.client.get(_VALID, headers={"X-API-Key": "test-key"})
    assert served.status_code == 200, (
        "a subscription caller was refused because a machine-payment price "
        "could not be resolved"
    )


# ===========================================================================
# ADDITIONAL — a payment-required request with no enforceable rail
#
# Found while remediating finding 5, and pre-existing at 57b7437: a caller
# declaring an unsupported `X-StockTrends-Payment-Method` resolved to rail
# "none", so `enforce_payment_rail` was never called, the gate returned
# "proceed", and the endpoint served paid data for nothing.
# ===========================================================================

def test_06_unsupported_payment_method_never_serves_paid_data(
    payment_harness, monkeypatch
):
    """
    Fail closed when there is no rail to enforce.

    This is not a challenge and not a settlement failure: the caller named a
    payment method the system does not implement, so there is no rail that could
    verify anything — and therefore nothing that may be served.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    headers = dict(AGENT_HEADERS)
    headers["X-StockTrends-Payment-Method"] = "definitely-not-a-rail"

    response = payment_harness.client.get(_VALID, headers=headers)

    assert response.status_code == 402
    assert response.json()["error"] == "unsupported_payment_rail"
    assert set(response.json()["accepted_payment_methods"]) == {
        "subscription",
        "x402",
        "mpp",
    }
    assert len(queries) == 0, (
        "the paid endpoint executed for a request that named no enforceable rail"
    )
    _assert_nothing_moved(payment_harness)

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "rejected"
    assert row["billed_amount_usd"] == 0
    assert payment_harness.logs.only_event_row()["is_billable"] == 0


def test_06b_supported_rails_are_unaffected(payment_harness, priced_engines):
    """Positive control: the two real rails still work."""
    settled = payment_harness.client.get(_VALID, headers=x402_headers())
    assert settled.status_code == 200
    assert payment_harness.settle_count == 1

    payment_harness.logs.economics.clear()
    payment_harness.logs.events.clear()

    captured = payment_harness.client.get(_VALID, headers=mpp_headers())
    assert captured.status_code == 200
    assert payment_harness.mpp.capture_count == 1
