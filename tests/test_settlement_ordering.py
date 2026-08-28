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
    UNIT_PRICE_USD,
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
def test_01_malformed_symbol_exchange_does_not_settle(payment_harness, priced_engines):
    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM", headers=x402_headers()
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_symbol_exchange"
    _assert_no_settlement(payment_harness)


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

def test_10_invalid_proof_verifies_but_never_settles(payment_harness, priced_engines):
    response = payment_harness.client.get(
        _VALID_PRICES_QUERY, headers=malformed_x402_headers()
    )

    assert response.status_code == 402
    # The artifact is rejected during header validation, ahead of the facilitator.
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
    """Economic capture must never occur for a request that was never servable."""
    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM", headers=mpp_headers()
    )

    assert response.status_code == 400
    assert payment_harness.mpp.capture_count == 0


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
    assert row["unit_price_usd"] == UNIT_PRICE_USD, "quoted price is still recorded"
    assert row["stc_cost"] == UNIT_PRICE_USD, "STC analytics value is still recorded"
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
    payment_harness.client.get(_VALID_PRICES_QUERY, headers=x402_headers())

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "settled"
    assert row["unit_price_usd"] == UNIT_PRICE_USD
    assert row["stc_cost"] == UNIT_PRICE_USD
    assert row["billed_amount_usd"] == UNIT_PRICE_USD


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
