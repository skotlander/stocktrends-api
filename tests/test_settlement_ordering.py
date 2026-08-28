"""
Settlement-ordering acceptance suite.

Governing invariant
-------------------
    No deterministic client-input failure or route-miss condition knowable
    before paid service execution may cause x402 settlement.

This is deliberately NOT "no settled request may return 4xx".  Data- and
service-dependent failures — a DB-backed symbol-not-found, an ambiguous symbol,
an empty candidate set, a downstream outage — are discovered only by running the
paid service, and remain chargeable.  `test_25_*` asserts that permission
explicitly so a future over-correction cannot quietly break it.

Every case asserts against the facilitator spy, not the status code.  A 400 tells
you nothing about whether money moved.

Markers
-------
`pytest.mark.xfail(strict=True)` marked the cases the defect broke.  Strict mode
meant PR 2 and PR 3 could not land silently: the moment behaviour was fixed, each
xfail became an unexpected pass and the suite went red until the marker was
removed.  That handshake is complete — PR 2 moved the payment gate behind
FastAPI's own validation, PR 3 put request-only semantic validation in front of
that gate, and all eight markers have been removed against the new ordering.
No marker remains in this file, and none should be added without justification:
an xfail here is an admission that money can still move for a request that could
never have been served.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import pytest

import routers.decision as decision_router
import routers.portfolio as portfolio_router
import routers.prices as prices_router
import routers.screener as screener_router
import routers.stim as stim_router
from support.payment_harness import (
    SENTINEL_STC_COST,
    SENTINEL_UNIT_PRICE_USD,
    counting_engine,
    malformed_x402_headers,
    mpp_headers,
    rows_engine,
    sequence_engine,
    unpaid_headers,
    x402_headers,
)

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_PRICE_ROW = {
    "weekdate": "2026-01-02", "exchange": "N", "symbol": "IBM", "type": "CS",
    "currency_code": "USD", "price": 100.0, "adj_close": 100.0,
    "pr_week_hi": 101.0, "pr_week_lo": 99.0, "volume": 1000, "trades": 10,
    "split_fact": 1.0, "pr_change": 0.5,
}

_VALID_PRICES_QUERY = "/v1/prices/history?symbol_exchange=IBM-N"


@pytest.fixture
def priced_engines(monkeypatch):
    """Stub every router engine the suite touches with a benign result set."""
    for module in (prices_router, stim_router, screener_router):
        monkeypatch.setattr(module, "get_engine", lambda: rows_engine([_PRICE_ROW]))


def _assert_no_settlement(harness, *, verify_expected: int = 0) -> None:
    assert harness.settle_count == 0, (
        f"settlement occurred {harness.settle_count}x for a request that must "
        "never reach the payment gate"
    )
    assert harness.verify_count == verify_expected, (
        f"facilitator verify called {harness.verify_count}x, expected "
        f"{verify_expected}"
    )


# ===========================================================================
# 1-4 — invalid GET input presented with a valid payment proof
# ===========================================================================

def test_01_malformed_symbol_exchange_does_not_settle(payment_harness, monkeypatch):
    """
    The representative case, asserted on all four axes at once.

    Non-execution of the paid service is proved by the query counter rather than
    inferred from the status code: a 400 is equally consistent with "the endpoint
    never ran" and "the endpoint ran, queried, and then rejected the input".

    Assertion order is preserved from when this case was a strict xfail: the
    query-count assertion is deliberately ahead of the facilitator assertions, so
    the endpoint-nonexecution measurement is reached and exercised rather than
    short-circuited by the failure the marker expected.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM", headers=x402_headers()
    )

    # 1. The expected deterministic client-input error.
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_symbol_exchange"

    # 2. The paid service did not execute.
    assert len(queries) == 0, (
        f"the paid service executed {len(queries)} quer(ies) for a request that "
        "must be rejected before payment"
    )

    # 3-4. No money moved.
    assert payment_harness.verify_count == 0, "facilitator verify must not run"
    assert payment_harness.settle_count == 0, "facilitator settle must not run"


def test_02_constraint_violation_does_not_settle(payment_harness, priced_engines):
    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM-N&limit=0", headers=x402_headers()
    )

    assert response.status_code == 422
    _assert_no_settlement(payment_harness)


def test_03_invalid_enum_does_not_settle(payment_harness, priced_engines):
    response = payment_harness.client.get(
        "/v1/agent/screener/top?sort=bogus", headers=x402_headers()
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_sort"
    _assert_no_settlement(payment_harness)


def test_04_invalid_exchange_domain_does_not_settle(payment_harness, priced_engines):
    response = payment_harness.client.get(
        "/v1/stim/latest?symbol_exchange=IBM-Z", headers=x402_headers()
    )

    assert response.status_code == 400
    _assert_no_settlement(payment_harness)


# ===========================================================================
# 5-7 — invalid POST bodies presented with a valid payment proof
# ===========================================================================

def test_05_malformed_json_body_does_not_settle(payment_harness):
    headers = x402_headers()
    headers["Content-Type"] = "application/json"

    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol", headers=headers, content=b"{"
    )

    assert response.status_code == 422
    _assert_no_settlement(payment_harness)


def test_06_schema_invalid_body_does_not_settle(payment_harness):
    response = payment_harness.client.post(
        "/v1/portfolio/construct", headers=x402_headers(), json={"count": 99}
    )

    assert response.status_code == 422
    _assert_no_settlement(payment_harness)


def test_07_semantic_invalid_body_does_not_settle(payment_harness):
    """
    An empty body is schema-valid but semantically incomplete.

    Every field of `EvaluateSymbolRequest` is optional, so FastAPI accepts `{}`
    and the endpoint used to raise its 422 from inside the body, after
    settlement.  The rejection is decided entirely by the request, so it now
    comes from the registered semantic validator ahead of the gate — same status,
    same detail, no money moved.
    """
    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol", headers=x402_headers(), json={}
    )

    assert response.status_code == 422
    _assert_no_settlement(payment_harness)


def test_07b_semantic_invalid_enum_body_does_not_settle(payment_harness):
    response = payment_harness.client.post(
        "/v1/portfolio/construct", headers=x402_headers(), json={"bias": "sideways"}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_bias"
    _assert_no_settlement(payment_harness)


# ===========================================================================
# 8-9 — discovery precedence: invalid input beats the payment challenge
# ===========================================================================

def test_08_invalid_unpaid_request_returns_input_error_not_challenge(
    payment_harness, priced_engines
):
    """
    Contract-visible and intentional: an unpaid request that could never have
    been served returns the deterministic client-input error rather than a
    payment challenge.  Pricing context headers stay present so an agent can
    still discover the price after correcting its request.

    This precedence is why `tests/test_402_preview.py` names a real instrument
    in the requests it uses to inspect challenge shape — a paid instrument
    endpoint called with no instrument is now answered before payment.
    """
    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM", headers=unpaid_headers()
    )

    assert response.status_code == 400
    assert "payment-required" not in response.headers
    assert response.headers["x-stocktrends-pricing-rule"] == "prices_history_paid"
    _assert_no_settlement(payment_harness)


def test_09_valid_unpaid_request_returns_challenge(payment_harness, priced_engines):
    response = payment_harness.client.get(
        _VALID_PRICES_QUERY, headers=unpaid_headers()
    )

    assert response.status_code == 402
    assert "payment-required" in response.headers
    assert response.json()["error"] == "payment_required"
    _assert_no_settlement(payment_harness)


# ===========================================================================
# 10-11 — payment presented but not acceptable
# ===========================================================================

def test_10_malformed_payment_syntax_rejected_before_verification(
    payment_harness, priced_engines
):
    """An undecodable payment artifact fails header validation, so the request
    never reaches the facilitator at all — neither verify nor settle."""
    response = payment_harness.client.get(
        _VALID_PRICES_QUERY, headers=malformed_x402_headers()
    )

    assert response.status_code == 402
    _assert_no_settlement(payment_harness, verify_expected=0)


def test_10b_facilitator_rejected_proof_never_settles(payment_harness, priced_engines):
    payment_harness.facilitator.verify_valid = False

    response = payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    assert response.status_code == 402
    assert response.json()["error"] == "payment_verification_failed"
    _assert_no_settlement(payment_harness, verify_expected=1)


def test_11_replayed_reference_never_settles(payment_harness, priced_engines):
    payment_harness.mark_reference_used("already-spent")

    response = payment_harness.client.get(
        _VALID_PRICES_QUERY, headers=x402_headers(reference="already-spent")
    )

    assert response.status_code == 402
    assert response.json()["error"] == "replay_detected"
    _assert_no_settlement(payment_harness)


# ===========================================================================
# 12-13, 20 — the happy paths must keep working, exactly once
# ===========================================================================

def test_12_valid_paid_get_settles_exactly_once(payment_harness, priced_engines):
    response = payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    assert response.status_code == 200
    assert response.json()["symbol_exchange"] == "IBM-N"
    assert payment_harness.verify_count == 1
    assert payment_harness.settle_count == 1
    assert "payment-response" in response.headers


def test_13_valid_paid_post_settles_exactly_once(payment_harness, monkeypatch):
    monkeypatch.setattr(
        decision_router,
        "get_engine",
        lambda: sequence_engine([[], []]),
    )

    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol",
        headers=x402_headers(),
        json={"symbol_exchange": "IBM-N"},
    )

    # No weekdates in the stub, so the endpoint reaches its data-dependent 503.
    # That is a Class 2 failure: it required paid execution to discover, so a
    # single settlement is correct.
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "no_signal_data"
    assert payment_harness.settle_count == 1


def test_20_gate_is_one_shot(payment_harness, priced_engines):
    """A single request must produce exactly one settlement, never two."""
    response = payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    assert response.status_code == 200
    assert payment_harness.settle_count == 1, (
        "the deferred gate must cache its result; a second invocation must "
        "never create a second settlement"
    )


# ===========================================================================
# 14-15 — subscription regression
# ===========================================================================

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


def test_14_subscription_valid_request_never_settles(subscription_client, priced_engines):
    response = subscription_client.client.get(
        _VALID_PRICES_QUERY, headers={"X-API-Key": "test-key"}
    )

    assert response.status_code == 200
    assert subscription_client.settle_count == 0
    row = subscription_client.logs.only_economics_row()
    assert row["payment_rail"] == "subscription"
    assert row["payment_required"] == 0
    assert row["unit_price_usd"] == SENTINEL_UNIT_PRICE_USD
    assert row["stc_cost"] == SENTINEL_STC_COST
    assert row["billed_amount_usd"] == 0, (
        "a quota-backed caller is never charged per request"
    )


def test_15_subscription_invalid_input_never_settles(subscription_client, priced_engines):
    response = subscription_client.client.get(
        "/v1/prices/history?symbol_exchange=IBM", headers={"X-API-Key": "test-key"}
    )

    assert response.status_code == 400
    assert subscription_client.settle_count == 0


# ===========================================================================
# 16-17 — MPP regression
# ===========================================================================

def test_16_valid_mpp_request_authorizes_and_captures(payment_harness, priced_engines):
    response = payment_harness.client.get(_VALID_PRICES_QUERY, headers=mpp_headers())

    assert response.status_code == 200
    assert payment_harness.mpp.authorize_count == 1
    assert payment_harness.mpp.capture_count == 1, (
        "capture_mpp_payment must not be silently lost when enforcement moves "
        "off the enclosing local"
    )
    assert payment_harness.mpp.void_count == 0
    assert payment_harness.settle_count == 0
    assert payment_harness.logs.only_economics_row()["payment_status"] == "captured"


def test_17_invalid_mpp_request_never_captures(payment_harness, priced_engines):
    """
    No MPP control-plane traffic at all for a structurally invalid request.

    This case previously pinned a compensating behaviour: MPP authorized, the
    endpoint then rejected the input, and the finaliser voided the
    authorization.  That was economically safe but still opened and closed a
    reservation against a session for a request that could never be served.

    Semantic validation now runs before the gate, so the correct state is no
    control-plane round trip in either direction.  All three counts are still
    asserted together — a later change that reintroduced authorize-then-void
    would be a regression, not a return to a tolerable equilibrium — and the
    obsolete authorize=1 / void=1 expectation is deliberately not retained
    anywhere in this suite.
    """
    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM", headers=mpp_headers()
    )

    assert response.status_code == 400
    assert payment_harness.mpp.capture_count == 0, (
        "economic capture must never occur for a request that was never servable"
    )
    assert payment_harness.mpp.authorize_count == 0, (
        "the control plane must not be asked to reserve funds for a request "
        "rejected before the payment gate"
    )
    assert payment_harness.mpp.void_count == 0, (
        "nothing was authorized, so there is nothing to compensate; a void here "
        "would mean authorize was reached after all"
    )


def test_17b_invalid_mpp_request_never_authorizes(payment_harness, priced_engines):
    """
    The same claim as test_17, stated as the invariant rather than as counts.

    Kept separate because it is the case that originally carried the strict
    xfail: it is the one that had to flip when validation moved ahead of MPP
    authorization, and it stays as the named regression guard for that ordering.
    """
    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM", headers=mpp_headers()
    )

    assert response.status_code == 400
    assert payment_harness.mpp.authorize_count == 0
    assert payment_harness.mpp.void_count == 0


# ===========================================================================
# 18-19 — route and method misses
# ===========================================================================

def test_18_route_miss_under_paid_prefix_does_not_settle(payment_harness):
    """`/v1/stim` is a prefix enforcement scope, so a typo settles today and
    then 404s for a route that does not exist."""
    response = payment_harness.client.get("/v1/stim/latst", headers=x402_headers())

    assert response.status_code == 404
    _assert_no_settlement(payment_harness)


def test_19_method_miss_does_not_settle(payment_harness):
    response = payment_harness.client.post(
        "/v1/prices/history", headers=x402_headers(), json={}
    )

    assert response.status_code in {401, 404, 405}
    _assert_no_settlement(payment_harness)


# ===========================================================================
# 23-24 — accounting: billed_amount_usd is what was collected
# ===========================================================================

def test_23a_challenge_records_zero_billed_amount(payment_harness, priced_engines):
    payment_harness.client.get(_VALID_PRICES_QUERY, headers=unpaid_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "pending"
    assert row["unit_price_usd"] == SENTINEL_UNIT_PRICE_USD, (
        "the quoted price is still recorded; it is a price, not a claim of "
        "collection"
    )
    assert row["stc_cost"] == SENTINEL_STC_COST, (
        "the STC analytical value is still recorded regardless of collection"
    )
    assert row["billed_amount_usd"] == 0, (
        "nothing was collected on a challenge; billed_amount_usd must be zero"
    )


def test_23b_verification_failure_records_zero_billed_amount(
    payment_harness, priced_engines
):
    payment_harness.facilitator.verify_valid = False
    payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["billed_amount_usd"] == 0


def test_23c_settlement_failure_records_zero_billed_amount(
    payment_harness, priced_engines
):
    payment_harness.facilitator.settle_valid = False
    payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] in {"failed", "failed_validation"}
    assert row["billed_amount_usd"] == 0


def test_23d_replay_rejection_records_zero_billed_amount(
    payment_harness, priced_engines
):
    payment_harness.mark_reference_used("already-spent")
    payment_harness.client.get(
        _VALID_PRICES_QUERY, headers=x402_headers(reference="already-spent")
    )

    row = payment_harness.logs.only_economics_row()
    assert row["billed_amount_usd"] == 0


def test_23e_pre_gate_rejection_records_zero_billed_amount(
    payment_harness, priced_engines
):
    payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM", headers=x402_headers()
    )

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "pending"
    assert row["billed_amount_usd"] == 0


def test_24_settled_request_records_the_collected_amount(payment_harness, priced_engines):
    """
    The billed amount is what the rail collected, not what the catalogue quoted.

    On this path the two coincide by definition — an `exact` scheme artifact
    settles the quoted requirement — so the assertion is that billed tracks the
    settled artifact, and that it is emphatically not the STC cost, which is a
    different number and a different concept.
    """
    payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "settled"
    assert row["unit_price_usd"] == SENTINEL_UNIT_PRICE_USD
    assert row["stc_cost"] == SENTINEL_STC_COST
    assert row["billed_amount_usd"] == SENTINEL_UNIT_PRICE_USD, (
        "billed must equal the amount actually settled by the artifact"
    )
    assert row["billed_amount_usd"] != SENTINEL_STC_COST, (
        "the STC analytical cost must never become the collected amount"
    )


def test_24b_economics_fields_are_not_swapped(payment_harness, priced_engines):
    """
    A dedicated positive control for the sentinel scheme itself.

    unit price and STC cost are distinct values, so a mapping swap between them
    is visible; and the billed amount must follow the collection rather than the
    analytical measure.
    """
    payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["unit_price_usd"] == SENTINEL_UNIT_PRICE_USD
    assert row["unit_price_usd"] != SENTINEL_STC_COST
    assert row["stc_cost"] == SENTINEL_STC_COST
    assert row["stc_cost"] != SENTINEL_UNIT_PRICE_USD
    assert row["billed_amount_usd"] != SENTINEL_STC_COST


def test_24c_billed_amount_is_read_from_enforcement_not_price_lookup(
    payment_harness, priced_engines, monkeypatch
):
    """
    Provenance test: `billed_amount_usd` is sourced from the enforcement result.

    NOT a sanctioned-underpayment case.  Real x402 enforcement cannot reach
    `outcome="proceed"` with less than the quoted amount — an artifact below the
    requirement is rejected before the facilitator by
    `x402_insufficient_amount_detail`, whatever `VALIDATE_AGENT_PAY_HEADERS`
    says, which is what `test_31_*` asserts.

    `enforce_payment_rail` is therefore stubbed wholesale, deliberately
    bypassing that validation, purely to make the two candidate sources of the
    billed amount produce different numbers.  The quoted price stays at the
    0.15 sentinel while the stub reports a settled native amount of 0.09.  A
    billed amount read from the price catalogue would still say 0.15; one read
    from the settlement says 0.09.  The 0.09 is a probe value, not a payment
    the system would ever accept.
    """
    import middleware.metering as metering_module
    from payments.enforcement import PaymentEnforcementResult

    monkeypatch.setattr(
        metering_module,
        "enforce_payment_rail",
        lambda **_kw: PaymentEnforcementResult(
            outcome="proceed",
            payment_reference="x402-partial-ref",
            payment_network="eip155:8453",
            payment_token="0xtoken",
            # 0.09 USDC at 6 decimals — deliberately not the quoted 0.15, so the
            # two possible provenances of billed_amount_usd are distinguishable.
            payment_amount_native=Decimal("90000"),
            payment_response={"success": True, "transaction": "0xdeadbeef"},
        ),
    )

    payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "settled"
    assert row["unit_price_usd"] == SENTINEL_UNIT_PRICE_USD
    assert row["billed_amount_usd"] == Decimal("0.09"), (
        "billed must come from the settled payment amount, not the quoted price"
    )


# ===========================================================================
# 25 — the permission the invariant must NOT over-correct away
# ===========================================================================

def test_25_data_dependent_not_found_still_settles(payment_harness, monkeypatch):
    """
    A structurally valid, fully paid request whose symbol does not exist in the
    database settles exactly once and returns its existing post-execution 404.

    The caller consumed the paid lookup that produced that answer.  This case is
    the guardrail against re-reading the invariant as "no settled request may
    return 4xx".
    """
    monkeypatch.setattr(prices_router, "get_engine", lambda: rows_engine([]))

    response = payment_harness.client.get(
        "/v1/prices/latest?symbol_exchange=ZZZZ-N", headers=x402_headers()
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "price_not_found"
    assert payment_harness.settle_count == 1, (
        "data-dependent failures discovered by paid execution remain chargeable"
    )
    assert payment_harness.logs.only_economics_row()["payment_status"] == "settled"


# ===========================================================================
# Optional coverage
#
# Not required for approval of this PR.  Both cases are three lines and depend
# only on the existing harness, and both are relevant to PR2 — the first because
# a standard x402 client sends no Stock Trends payment header at all, the second
# because amount validation happens before enforcement and must stay there.
# Isolated here so they can be dropped in one edit if review prefers.
# ===========================================================================

def test_opt_standard_x402_client_without_stocktrends_method_header(
    payment_harness, priced_engines
):
    """A conformant x402 client sends only `X-Payment`; rail resolution must
    still reach x402 rather than depending on the private header."""
    response = payment_harness.client.get(
        _VALID_PRICES_QUERY, headers=x402_headers(declare_method=False)
    )

    assert response.status_code == 200
    assert payment_harness.settle_count == 1
    assert payment_harness.logs.only_economics_row()["payment_rail"] == "x402"


def test_opt_underpaid_proof_never_settles(payment_harness, priced_engines):
    """An artifact presenting less than the quoted price is rejected during
    header validation, ahead of the facilitator."""
    response = payment_harness.client.get(
        _VALID_PRICES_QUERY, headers=x402_headers(amount="1")
    )

    assert response.status_code == 402
    assert response.json()["error"] == "insufficient_payment_amount"
    _assert_no_settlement(payment_harness)


# ===========================================================================
# 26-27 — the execution boundary is load-bearing, not advisory
#
# Startup verification makes an unwrapped route unreachable in this
# application.  These cases construct one anyway, because the middleware must
# still fail closed for any surface it did not verify itself.
# ===========================================================================

PAID_PAYLOAD_MARKER = "paid-payload-that-must-never-reach-a-client"

# `/v1/stim/*` is a prefix enforcement scope, so a route registered here is
# payment-governed by policy without needing a bespoke registration.
_UNWRAPPED_PROBE_PATH = "/stim/_boundary_probe"


@contextmanager
def temporary_v1_route(path: str, *, wrap: bool):
    """
    Register a route on the shared v1 app for one test, then withdraw it.

    `wrap=False` reproduces the reviewer's scenario: a payment-governed route
    that reached the surface after the boundary was installed.
    """
    import main
    from api.routing import install_payment_execution_boundary

    original_routes = list(main.v1.routes)
    original_schema = main.v1.openapi_schema
    try:
        @main.v1.get(path)
        def probe_endpoint():
            return {"marker": PAID_PAYLOAD_MARKER}

        if wrap:
            install_payment_execution_boundary(main.v1)
        yield
    finally:
        main.v1.router.routes[:] = original_routes
        # A schema generated while the probe existed must not outlive it.
        main.v1.openapi_schema = original_schema


def test_26_unwrapped_paid_route_fails_closed_and_never_leaks_the_payload(
    payment_harness, caplog
):
    """
    An endpoint that executed without consulting the payment gate must not have
    its result delivered.

    Payment is deliberately NOT invoked to repair this: enforcement after the
    work is done would charge for a request the caller never agreed to pay, and
    could not be undone.  The only safe answer is to refuse the result.
    """
    import logging

    with temporary_v1_route(_UNWRAPPED_PROBE_PATH, wrap=False):
        with caplog.at_level(logging.CRITICAL, logger="stocktrends_api.metering"):
            response = payment_harness.client.get(
                f"/v1{_UNWRAPPED_PROBE_PATH}", headers=x402_headers()
            )

    # The client is told the request failed, and learns nothing of the payload.
    assert response.status_code == 500
    assert response.json()["error"] == "payment_execution_boundary_not_consulted"
    assert PAID_PAYLOAD_MARKER not in response.text, (
        "the paid payload leaked to a caller that never paid for it"
    )

    # No rail was contacted — not before the fact, and not retroactively.
    assert payment_harness.verify_count == 0
    assert payment_harness.settle_count == 0
    assert payment_harness.mpp.authorize_count == 0
    assert payment_harness.mpp.capture_count == 0
    assert payment_harness.mpp.void_count == 0

    # The economics row records an uncollected request.
    row = payment_harness.logs.only_economics_row()
    assert row["billed_amount_usd"] == 0

    event = payment_harness.logs.only_event_row()
    assert event["success"] == 0
    assert event["error_code"] == "payment_execution_boundary_not_consulted"

    assert any(
        record.levelno >= logging.CRITICAL for record in caplog.records
    ), "the invariant breach must be logged at CRITICAL"


def test_26b_wrapped_probe_route_behaves_normally(payment_harness):
    """
    Control for test_26.  The same route, wrapped, takes the ordinary path — so
    the 500 above is attributable to the missing boundary and nothing else.
    """
    with temporary_v1_route(_UNWRAPPED_PROBE_PATH, wrap=True):
        response = payment_harness.client.get(
            f"/v1{_UNWRAPPED_PROBE_PATH}", headers=x402_headers()
        )

    assert response.status_code == 200
    assert response.json()["marker"] == PAID_PAYLOAD_MARKER
    assert payment_harness.settle_count == 1
    assert payment_harness.logs.only_economics_row()["payment_status"] == "settled"


def test_27_paid_route_without_a_request_parameter_still_consults_the_gate(
    payment_harness
):
    """
    The injected-request path, end to end, on a payment-governed route.

    `probe_endpoint` declares no `Request`, so the wrapper can only reach
    `request.state` through the parameter name the installer injected.  A 402
    here proves that injection worked: the gate was consulted for an endpoint
    that never asked for a request of its own.
    """
    with temporary_v1_route(_UNWRAPPED_PROBE_PATH, wrap=True):
        response = payment_harness.client.get(
            f"/v1{_UNWRAPPED_PROBE_PATH}", headers=unpaid_headers()
        )

    assert response.status_code == 402
    assert response.json()["error"] == "payment_required"
    assert PAID_PAYLOAD_MARKER not in response.text
    _assert_no_settlement(payment_harness)


# ===========================================================================
# 28 — MPP charges one amount, not two
# ===========================================================================

def test_28_mpp_authorizes_and_captures_the_quoted_charge(payment_harness, priced_engines):
    """
    Authorization and capture are two legs of one payment and must agree.

    Production hid a disagreement here because the quoted price and the STC cost
    are the same number for every current rule.  The harness prices them apart,
    so authorizing the quote and capturing the STC cost is now visible — and
    billed must follow what was captured, not either catalogue value by
    coincidence.
    """
    response = payment_harness.client.get(_VALID_PRICES_QUERY, headers=mpp_headers())

    assert response.status_code == 200
    assert payment_harness.mpp.authorize_count == 1
    assert payment_harness.mpp.capture_count == 1

    authorized = payment_harness.mpp.authorize_calls[0]["requested_stc"]
    captured = payment_harness.mpp.capture_calls[0]["captured_stc"]

    assert authorized == SENTINEL_UNIT_PRICE_USD, (
        f"MPP authorized {authorized}, expected the quoted charge "
        f"{SENTINEL_UNIT_PRICE_USD}"
    )
    assert captured == SENTINEL_UNIT_PRICE_USD, (
        f"MPP captured {captured}, expected the authorized charge "
        f"{SENTINEL_UNIT_PRICE_USD}; capturing the STC cost "
        f"({SENTINEL_STC_COST}) would settle a different amount than was reserved"
    )
    assert captured == authorized, "capture must settle exactly what was authorized"
    assert captured != SENTINEL_STC_COST

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "captured"
    assert row["billed_amount_usd"] == SENTINEL_UNIT_PRICE_USD
    assert row["unit_price_usd"] == SENTINEL_UNIT_PRICE_USD
    assert row["stc_cost"] == SENTINEL_STC_COST, (
        "the STC analytical cost is recorded independently and is not the charge"
    )


def test_28b_mpp_capture_failure_collects_nothing(payment_harness, priced_engines):
    """A capture the control plane refused collected no money."""
    payment_harness.mpp.capture_success = False

    payment_harness.client.get(_VALID_PRICES_QUERY, headers=mpp_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "capture_failed"
    assert row["billed_amount_usd"] == 0


# ===========================================================================
# 29 — rejection paths keep their payment context in api_request_logs
# ===========================================================================

@pytest.mark.parametrize(
    ("case", "headers_factory", "setup"),
    [
        ("unpaid challenge", unpaid_headers, None),
        ("malformed artifact", malformed_x402_headers, None),
        ("underpaid artifact", lambda: x402_headers(amount="1"), None),
        (
            "replay",
            lambda: x402_headers(reference="already-spent"),
            lambda h: h.mark_reference_used("already-spent"),
        ),
        (
            "verification failure",
            x402_headers,
            lambda h: setattr(h.facilitator, "verify_valid", False),
        ),
        (
            "settlement failure",
            x402_headers,
            lambda h: setattr(h.facilitator, "settle_valid", False),
        ),
    ],
)
def test_29_rejections_record_payment_context_in_the_request_event(
    payment_harness, priced_engines, case, headers_factory, setup
):
    """
    A standard x402 client sends only `X-Payment`, so the network and token are
    known from enforcement rather than from any inbound Stock Trends header.
    Both log destinations must keep them: fixing only the economics row would
    leave api_request_logs blind on exactly the requests operators investigate.
    """
    if setup is not None:
        setup(payment_harness)

    response = payment_harness.client.get(_VALID_PRICES_QUERY, headers=headers_factory())
    assert response.status_code == 402, case

    event = payment_harness.logs.only_event_row()
    assert event["payment_network"], f"{case}: request event lost payment_network"
    assert event["payment_token"], f"{case}: request event lost payment_token"

    econ = payment_harness.logs.only_economics_row()
    assert event["payment_network"] == econ["payment_network"], case
    assert event["payment_token"] == econ["payment_token"], case
    assert econ["billed_amount_usd"] == 0, case


# ===========================================================================
# 30 — the gate's outcome is terminal in both directions
# ===========================================================================

def test_30_gate_caches_and_reraises_an_enforcement_failure():
    """
    A raising enforcement attempt must never decay into "proceed".

    Marking the gate invoked before the call returns would leave
    `invoked=True, response=None` after a crash — and `None` is the signal that
    means the endpoint may execute.  A caller that retried would then serve paid
    work for free.
    """
    from middleware.metering import DeferredPaymentGate

    attempts = []
    boom = RuntimeError("facilitator exploded")

    def failing_enforcement():
        attempts.append(1)
        raise boom

    gate = DeferredPaymentGate(failing_enforcement)

    with pytest.raises(RuntimeError) as first:
        gate()
    with pytest.raises(RuntimeError) as second:
        gate()

    assert len(attempts) == 1, (
        f"enforcement ran {len(attempts)} times; a failed attempt must not be retried"
    )
    assert first.value is boom
    assert second.value is boom, "the second call must re-raise the cached failure"
    assert gate.invoked and gate.failed


def test_30b_gate_caches_a_normal_outcome_without_re_enforcing():
    """The success side of the same contract."""
    from starlette.responses import JSONResponse

    from middleware.metering import DeferredPaymentGate

    attempts = []
    rejection = JSONResponse(status_code=402, content={"error": "payment_required"})

    def enforcement():
        attempts.append(1)
        return rejection

    gate = DeferredPaymentGate(enforcement)

    assert gate() is rejection
    assert gate() is rejection
    assert len(attempts) == 1
    assert not gate.failed


def test_30c_gate_proceed_result_is_cached_as_proceed():
    from middleware.metering import DeferredPaymentGate

    attempts = []
    gate = DeferredPaymentGate(lambda: attempts.append(1))

    assert gate() is None
    assert gate() is None
    assert len(attempts) == 1


# ===========================================================================
# 31 — the minimum charge is not switchable
#
# `VALIDATE_AGENT_PAY_HEADERS` used to be the only thing comparing the presented
# amount to the quoted price.  With it off, an underpaid artifact went straight
# to the facilitator and settled for less than the quote.  Production runs with
# validation on, but an economic minimum must not depend on an optional
# validation flag, so the enforcement path applies the same shared rule itself.
#
# The matrix below is the point: the flag changes nothing about the outcome.
# ===========================================================================

@pytest.fixture
def validation_flag_off(payment_harness, monkeypatch):
    """The hazardous configuration: enforcement on, optional validation off."""
    import middleware.metering as metering_module

    monkeypatch.setattr(metering_module, "VALIDATE_AGENT_PAY_HEADERS", False)
    return payment_harness


def _assert_underpayment_rejected(harness, response) -> None:
    assert response.status_code == 402
    assert response.json()["error"] == "insufficient_payment_amount"
    assert harness.verify_count == 0, (
        "an underpaid artifact reached the facilitator's verify endpoint"
    )
    assert harness.settle_count == 0, (
        "an underpaid artifact was settled"
    )
    assert harness.logs.only_economics_row()["billed_amount_usd"] == 0


def test_31_underpaid_artifact_rejected_with_validation_flag_on(
    payment_harness, priced_engines
):
    """The configuration production runs. Rejected before the facilitator."""
    response = payment_harness.client.get(
        _VALID_PRICES_QUERY, headers=x402_headers(amount="1")
    )
    _assert_underpayment_rejected(payment_harness, response)


def test_31b_underpaid_artifact_rejected_with_validation_flag_off(
    validation_flag_off, priced_engines
):
    """
    The hazard, closed.

    Identical assertions to test_31 with `VALIDATE_AGENT_PAY_HEADERS=False`.
    Before this change the same request verified and settled 0.000001 USDC
    against a 0.15 quote; enforcement now applies the shared amount rule itself,
    so the flag governs optional validation behaviour and nothing economic.
    """
    response = validation_flag_off.client.get(
        _VALID_PRICES_QUERY, headers=x402_headers(amount="1")
    )
    _assert_underpayment_rejected(validation_flag_off, response)


def test_31c_exact_amount_settles_normally_with_validation_flag_off(
    validation_flag_off, priced_engines
):
    """
    Positive control.  The backstop must reject underpayment, not payment.

    Without this, test_31b would pass just as well if the new check rejected
    every artifact it saw.
    """
    response = validation_flag_off.client.get(
        _VALID_PRICES_QUERY, headers=x402_headers()
    )

    assert response.status_code == 200
    assert validation_flag_off.verify_count == 1
    assert validation_flag_off.settle_count == 1
    assert validation_flag_off.logs.only_economics_row()["payment_status"] == "settled"


def test_31d_overpaid_artifact_still_accepted_with_validation_flag_off(
    validation_flag_off, priced_engines
):
    """
    An amount above the requirement keeps its existing accepted behaviour.

    The rule is a minimum, not an equality: x402 doctrine does not treat paying
    more than asked as a protocol error, and this PR does not change facilitator
    protocol semantics beyond making the minimum non-optional.
    """
    response = validation_flag_off.client.get(
        _VALID_PRICES_QUERY, headers=x402_headers(amount="900000")
    )

    assert response.status_code == 200
    assert validation_flag_off.settle_count == 1


def test_31e_malformed_artifact_keeps_its_existing_rejection(
    payment_harness, priced_engines
):
    """
    An undecodable artifact keeps its existing pre-facilitator rejection.

    Scope note.  This PR made the *amount* minimum non-optional; it deliberately
    did not change which layer decides anything else.  Whether an artifact
    decodes at all remains validation-governed, exactly as before: with
    `VALIDATE_AGENT_PAY_HEADERS` on it is refused here, and with it off the
    payload is passed to the facilitator, which is the authority on whether a
    payload actually pays and rejects a garbage one.

    That asymmetry is intentional rather than an oversight.  Underpayment is
    decidable locally against a price we quoted, so leaving it to a flag was a
    real economic hazard.  Decodability is the facilitator's judgement, and
    pre-empting it would change protocol semantics this PR is not chartered to
    touch.  The new amount rule therefore abstains on an artifact whose amount
    cannot be read — an unreadable amount is not evidence of underpayment.
    """
    response = payment_harness.client.get(
        _VALID_PRICES_QUERY, headers=malformed_x402_headers()
    )

    assert response.status_code == 402
    _assert_no_settlement(payment_harness, verify_expected=0)


def test_31f_amount_rule_has_a_single_definition():
    """
    Both call sites resolve the same helper.

    Two independent amount comparisons could disagree about the boundary — one
    strict, one inclusive — and the disagreement would only ever be visible as
    money.  Asserted structurally so a re-inlined copy is caught.
    """
    import inspect

    import payments.enforcement as enforcement_module
    import payments.x402 as x402_module

    for source, where in (
        (inspect.getsource(x402_module.validate_x402_payment), "validate_x402_payment"),
        (inspect.getsource(enforcement_module.enforce_x402_payment), "enforce_x402_payment"),
    ):
        assert "x402_insufficient_amount_detail(" in source, (
            f"{where} no longer uses the shared amount-sufficiency helper; the "
            "minimum-charge rule now has more than one definition"
        )


def test_31g_amount_rule_boundary_is_inclusive():
    """The quoted amount exactly is payment, not underpayment."""
    from payments.x402 import x402_insufficient_amount_detail

    quote = Decimal("0.15")

    assert x402_insufficient_amount_detail(Decimal("150000"), quote) is None
    assert x402_insufficient_amount_detail(Decimal("150001"), quote) is None
    assert x402_insufficient_amount_detail(Decimal("149999"), quote) is not None
    assert x402_insufficient_amount_detail(None, quote) is None, (
        "an unreadable amount is not evidence of underpayment"
    )


# ===========================================================================
# 32 — x402_settled_amount_usd fallback is explicit in all three cases
# ===========================================================================

def test_32_settled_amount_conversions_and_fallbacks():
    """
    None and unexpected types both fall back to the quote, never to zero.

    Enforcement types this value `Decimal | None`.  The third case is a contract
    violation, and the tempting handling — coercing through `safe_decimal` —
    would record a settled payment as having collected 0, understating revenue
    on a request that did move money.
    """
    from middleware.metering import x402_settled_amount_usd

    quote = Decimal("0.15")

    # Expected Decimal: converted from atomic units.
    assert x402_settled_amount_usd(Decimal("150000"), quote) == Decimal("0.15")
    assert x402_settled_amount_usd(Decimal("90000"), quote) == Decimal("0.09")

    # None: settlement succeeded, so the quote was satisfied.
    assert x402_settled_amount_usd(None, quote) == quote

    # Unexpected types: the quote, emphatically not zero.
    for unexpected in ("not-a-number", object(), [], {}, float("nan")):
        assert x402_settled_amount_usd(unexpected, quote) == quote, (
            f"{unexpected!r} must fall back to the quoted price"
        )
        assert x402_settled_amount_usd(unexpected, quote) != Decimal("0")
