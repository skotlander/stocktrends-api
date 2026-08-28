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
`pytest.mark.xfail(strict=True)` marks the cases the defect currently breaks.
Strict mode means PR 2 and PR 3 cannot land silently: the moment behaviour is
fixed, the xfail becomes an unexpected pass and the suite goes red until the
marker is removed.  That is the intended handshake between the PRs.
"""

from __future__ import annotations

import pytest

import routers.decision as decision_router
import routers.portfolio as portfolio_router
import routers.prices as prices_router
import routers.screener as screener_router
import routers.stim as stim_router
from support.payment_harness import (
    SENTINEL_BILLED_AMOUNT_USD,
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

PRE_GATE = "PR2/PR3: deterministic input failure still settles before validation"
BILLED = "PR2: billed_amount_usd still carries list price on non-collected states"

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

@pytest.mark.xfail(strict=True, reason=PRE_GATE)
def test_01_malformed_symbol_exchange_does_not_settle(payment_harness, monkeypatch):
    """
    The representative case, asserted on all four axes at once.

    Non-execution of the paid service is proved by the query counter rather than
    inferred from the status code: a 400 is equally consistent with "the endpoint
    never ran" and "the endpoint ran, queried, and then rejected the input".

    Assertion order is deliberate.  The query-count assertion passes today and
    must therefore be reached and exercised, so it is placed ahead of the
    facilitator assertions that raise the expected failure.  Putting the
    facilitator checks first would short-circuit the test at the xfail and leave
    the endpoint-nonexecution measurement permanently unevaluated.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM", headers=x402_headers()
    )

    # 1. The expected deterministic client-input error.
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_symbol_exchange"

    # 2. The paid service did not execute.  Passes today; actively exercised.
    assert len(queries) == 0, (
        f"the paid service executed {len(queries)} quer(ies) for a request that "
        "must be rejected before payment"
    )

    # 3-4. No money moved.  These are what fail today.
    assert payment_harness.verify_count == 0, "facilitator verify must not run"
    assert payment_harness.settle_count == 0, "facilitator settle must not run"


@pytest.mark.xfail(strict=True, reason=PRE_GATE)
def test_02_constraint_violation_does_not_settle(payment_harness, priced_engines):
    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM-N&limit=0", headers=x402_headers()
    )

    assert response.status_code == 422
    _assert_no_settlement(payment_harness)


@pytest.mark.xfail(strict=True, reason=PRE_GATE)
def test_03_invalid_enum_does_not_settle(payment_harness, priced_engines):
    response = payment_harness.client.get(
        "/v1/agent/screener/top?sort=bogus", headers=x402_headers()
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_sort"
    _assert_no_settlement(payment_harness)


@pytest.mark.xfail(strict=True, reason=PRE_GATE)
def test_04_invalid_exchange_domain_does_not_settle(payment_harness, priced_engines):
    response = payment_harness.client.get(
        "/v1/stim/latest?symbol_exchange=IBM-Z", headers=x402_headers()
    )

    assert response.status_code == 400
    _assert_no_settlement(payment_harness)


# ===========================================================================
# 5-7 — invalid POST bodies presented with a valid payment proof
# ===========================================================================

@pytest.mark.xfail(strict=True, reason=PRE_GATE)
def test_05_malformed_json_body_does_not_settle(payment_harness):
    headers = x402_headers()
    headers["Content-Type"] = "application/json"

    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol", headers=headers, content=b"{"
    )

    assert response.status_code == 422
    _assert_no_settlement(payment_harness)


@pytest.mark.xfail(strict=True, reason=PRE_GATE)
def test_06_schema_invalid_body_does_not_settle(payment_harness):
    response = payment_harness.client.post(
        "/v1/portfolio/construct", headers=x402_headers(), json={"count": 99}
    )

    assert response.status_code == 422
    _assert_no_settlement(payment_harness)


@pytest.mark.xfail(strict=True, reason=PRE_GATE)
def test_07_semantic_invalid_body_does_not_settle(payment_harness):
    """An empty body is schema-valid but semantically incomplete — today a 422
    raised from inside the endpoint, after settlement."""
    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol", headers=x402_headers(), json={}
    )

    assert response.status_code == 422
    _assert_no_settlement(payment_harness)


@pytest.mark.xfail(strict=True, reason=PRE_GATE)
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

@pytest.mark.xfail(strict=True, reason=PRE_GATE)
def test_08_invalid_unpaid_request_returns_input_error_not_challenge(
    payment_harness, priced_engines
):
    """
    Contract-visible and intentional: an unpaid request that could never have
    been served returns the deterministic client-input error rather than a
    payment challenge.  Pricing context headers stay present so an agent can
    still discover the price after correcting its request.
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
    Pins today's compensating behaviour for a structurally invalid MPP request.

    MPP is already economically safe on invalid input, but only because the
    finaliser compensates: it authorizes, the endpoint then rejects the input,
    and the authorization is voided.  All three counts are asserted so that PR2
    cannot lose the compensation while moving the gate.

    NOTE FOR PR2 — this test is expected to change.  Once validation runs before
    MPP authorization the target state is authorize 0 / capture 0 / void 0, which
    is what test_17b asserts.  When test_17b stops xfailing, the authorize and
    void assertions here must be updated to 0.  A failure here after PR2 is that
    handover, not a regression.
    """
    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM", headers=mpp_headers()
    )

    assert response.status_code == 400
    assert payment_harness.mpp.capture_count == 0, (
        "economic capture must never occur for a request that was never servable"
    )
    assert payment_harness.mpp.authorize_count == 1, (
        "current behaviour: the control plane is asked to authorize before the "
        "endpoint rejects the input"
    )
    assert payment_harness.mpp.void_count == 1, (
        "current behaviour: the authorization is compensated by a void once the "
        "endpoint returns 4xx. PR2 must not drop this until authorize is no "
        "longer reached at all"
    )


@pytest.mark.xfail(strict=True, reason=PRE_GATE)
def test_17b_invalid_mpp_request_never_authorizes(payment_harness, priced_engines):
    """
    Stronger than 17.  MPP is already economically safe on invalid input because
    the finalizer voids the authorization, but the control-plane round trip is
    still made for a request that could never be served.
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

@pytest.mark.xfail(strict=True, reason=PRE_GATE)
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

@pytest.mark.xfail(strict=True, reason=BILLED)
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


@pytest.mark.xfail(strict=True, reason=BILLED)
def test_23b_verification_failure_records_zero_billed_amount(
    payment_harness, priced_engines
):
    payment_harness.facilitator.verify_valid = False
    payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["billed_amount_usd"] == 0


@pytest.mark.xfail(strict=True, reason=BILLED)
def test_23c_settlement_failure_records_zero_billed_amount(
    payment_harness, priced_engines
):
    payment_harness.facilitator.settle_valid = False
    payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] in {"failed", "failed_validation"}
    assert row["billed_amount_usd"] == 0


@pytest.mark.xfail(strict=True, reason=BILLED)
def test_23d_replay_rejection_records_zero_billed_amount(
    payment_harness, priced_engines
):
    payment_harness.mark_reference_used("already-spent")
    payment_harness.client.get(
        _VALID_PRICES_QUERY, headers=x402_headers(reference="already-spent")
    )

    row = payment_harness.logs.only_economics_row()
    assert row["billed_amount_usd"] == 0


@pytest.mark.xfail(strict=True, reason=PRE_GATE)
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
    Each field carries its own sentinel.

    Because the three values are pairwise distinct, this fails if PR2 wires the
    quoted price or the STC cost into the billed slot — a swap that would be
    invisible if all three were the same number.
    """
    payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "settled"
    assert row["unit_price_usd"] == SENTINEL_UNIT_PRICE_USD
    assert row["stc_cost"] == SENTINEL_STC_COST
    assert row["billed_amount_usd"] == SENTINEL_BILLED_AMOUNT_USD


def test_24b_economics_fields_are_not_swapped(payment_harness, priced_engines):
    """
    A dedicated positive control for the sentinel scheme itself.

    Asserting each field is *not* equal to the other two catches a swap even if
    a future edit changes what the correct value should be.
    """
    payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["unit_price_usd"] not in (SENTINEL_BILLED_AMOUNT_USD, SENTINEL_STC_COST)
    assert row["billed_amount_usd"] not in (SENTINEL_UNIT_PRICE_USD, SENTINEL_STC_COST)
    assert row["stc_cost"] not in (SENTINEL_UNIT_PRICE_USD, SENTINEL_BILLED_AMOUNT_USD)


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
