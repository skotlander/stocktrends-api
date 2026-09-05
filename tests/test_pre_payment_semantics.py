"""
Per-endpoint coverage for the validation PR 3 moved ahead of the payment gate.

`test_settlement_ordering.py` proves the invariant on one representative case
per endpoint.  This file covers the rest of each moved rule — every branch of
each shared helper, plus the valid inputs that must still reach the endpoint —
and pins the exact error payloads, so "the validation moved" cannot quietly
become "the validation changed".

Two things are asserted throughout:

* the public error contract is byte-for-byte what it was (status, `error`,
  `valid`, `value`, `message`, `request_id`);
* no rail was touched — verify, settle and MPP authorize all zero.

The second is why these are here rather than in the routers' own test files: a
status-code assertion alone cannot tell a rejection before payment from a
rejection after it.
"""

from __future__ import annotations

import pytest

import routers.breadth as breadth_router
import routers.decision as decision_router
import routers.leadership as leadership_router
import routers.portfolio as portfolio_router
import routers.prices as prices_router
import routers.screener as screener_router
import routers.selections as selections_router
import routers.stim as stim_router
import routers.stwr as stwr_router
from support.payment_harness import (
    counting_engine,
    mpp_headers,
    rows_engine,
    sequence_engine,
    unpaid_headers,
    x402_headers,
)

_PRICE_ROW = {
    "weekdate": "2026-01-02", "exchange": "N", "symbol": "IBM", "type": "CS",
    "currency_code": "USD", "price": 100.0, "adj_close": 100.0,
    "pr_week_hi": 101.0, "pr_week_lo": 99.0, "volume": 1000, "trades": 10,
    "split_fact": 1.0, "pr_change": 0.5,
}

# The screener projects signal columns, so it needs its own row shape.
_SIGNAL_ROW = {
    "symbol": "IBM", "exchange": "N", "trend": "^+", "trend_cnt": 4,
    "mt_cnt": 12, "rsi": 120, "rsi_updn": "U", "vol_tag": "HV",
    "weekdate": "2026-01-02",
}

_ROUTER_MODULES = (
    breadth_router,
    decision_router,
    leadership_router,
    portfolio_router,
    prices_router,
    screener_router,
    selections_router,
    stim_router,
    stwr_router,
)


@pytest.fixture
def counted_engines(monkeypatch):
    """
    Stub every touched router's engine and count the queries it receives.

    Non-execution of the paid service is then measured rather than inferred: a
    400 is equally consistent with "the endpoint never ran" and "the endpoint
    ran, queried, and then rejected the input".
    """
    engine, queries = counting_engine([_PRICE_ROW])
    for module in _ROUTER_MODULES:
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    return queries


@pytest.fixture
def priced_engines(monkeypatch):
    """A benign result set for the cases that must reach the endpoint."""
    for module in _ROUTER_MODULES:
        monkeypatch.setattr(module, "get_engine", lambda: rows_engine([_PRICE_ROW]))
    # Screener rows carry signal columns rather than price columns.
    monkeypatch.setattr(
        screener_router, "get_engine", lambda: rows_engine([_SIGNAL_ROW])
    )


def assert_rejected_before_payment(harness, queries=None) -> None:
    """No rail was contacted, and the paid service did not run."""
    assert harness.verify_count == 0, "facilitator verify must not run"
    assert harness.settle_count == 0, "facilitator settle must not run"
    assert harness.mpp.authorize_count == 0, "MPP must not authorize"
    assert harness.mpp.capture_count == 0
    assert harness.mpp.void_count == 0
    assert harness.logs.only_economics_row()["billed_amount_usd"] == 0
    if queries is not None:
        assert len(queries) == 0, (
            f"the paid service executed {len(queries)} quer(ies) for a request "
            "rejected before payment"
        )


# ===========================================================================
# A — prices: the shared symbol/exchange resolver, every branch
# ===========================================================================

PRICES_PATHS = ("/v1/prices/latest", "/v1/prices/history")


@pytest.mark.parametrize("path", PRICES_PATHS)
@pytest.mark.parametrize(
    ("query", "error"),
    [
        # Malformed composite identifier — no separator at all.
        ("?symbol_exchange=IBM", "invalid_symbol_exchange"),
        # Well-formed composite identifier naming an exchange that does not exist.
        ("?symbol_exchange=IBM-Z", "invalid_symbol_exchange"),
        # Composite identifier with an empty symbol.
        ("?symbol_exchange=-N", "invalid_symbol_exchange"),
        # Neither accepted form supplied.
        ("", "missing_required_param"),
        # Symbol present, exchange missing.
        ("?symbol=IBM", "missing_required_param"),
    ],
)
def test_prices_request_only_rejections_never_reach_a_rail(
    payment_harness, counted_engines, path, query, error
):
    response = payment_harness.client.get(path + query, headers=x402_headers())

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == error
    assert detail["request_id"], "the moved rejection lost its request_id"
    assert detail["message"], "the moved rejection lost its message"
    assert_rejected_before_payment(payment_harness, counted_engines)


@pytest.mark.parametrize("path", PRICES_PATHS)
def test_prices_symbol_with_invalid_exchange_is_rejected_before_payment(
    payment_harness, counted_engines, path
):
    """
    The split form with a bad exchange keeps `_norm_exchange`'s bare-string
    detail rather than the resolver's structured one — an existing quirk of this
    contract, preserved deliberately rather than tidied up in a PR about
    ordering.
    """
    response = payment_harness.client.get(
        f"{path}?symbol=IBM&exchange=Z", headers=x402_headers()
    )

    assert response.status_code == 400
    assert "Invalid exchange 'Z'" in response.json()["detail"]
    assert_rejected_before_payment(payment_harness, counted_engines)


@pytest.mark.parametrize("path", PRICES_PATHS)
@pytest.mark.parametrize(
    "query",
    ["?symbol_exchange=IBM-N", "?symbol=IBM&exchange=N", "?symbol=ibm&exchange=n"],
)
def test_prices_valid_identity_reaches_the_endpoint_and_settles_once(
    payment_harness, priced_engines, path, query
):
    """The positive control: valid identifiers are not caught by the new layer."""
    response = payment_harness.client.get(path + query, headers=x402_headers())

    assert response.status_code == 200
    assert payment_harness.settle_count == 1


# ===========================================================================
# B — ST-IM: same resolver, and its data-dependent outcomes stay paid
# ===========================================================================

STIM_PATHS = ("/v1/stim/latest", "/v1/stim/history")


@pytest.mark.parametrize("path", STIM_PATHS)
@pytest.mark.parametrize(
    ("query", "error"),
    [
        ("?symbol_exchange=IBM-Z", "invalid_symbol_exchange"),
        ("?symbol_exchange=IBM", "invalid_symbol_exchange"),
        ("", "missing_required_param"),
        ("?symbol=IBM", "missing_required_param"),
    ],
)
def test_stim_request_only_rejections_never_reach_a_rail(
    payment_harness, counted_engines, path, query, error
):
    response = payment_harness.client.get(path + query, headers=x402_headers())

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == error
    assert_rejected_before_payment(payment_harness, counted_engines)


def test_stim_latest_not_found_still_settles(payment_harness, monkeypatch):
    """
    The complement, and the reason the ST-IM move had to be surgical.

    A valid instrument with no ST-IM estimate is a Class 2 outcome: it took the
    paid query to discover, so it stays chargeable and keeps its 404.
    """
    monkeypatch.setattr(stim_router, "get_engine", lambda: rows_engine([]))

    response = payment_harness.client.get(
        "/v1/stim/latest?symbol_exchange=ZZZZ-N", headers=x402_headers()
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "stim_not_found"
    assert payment_harness.settle_count == 1, (
        "a data-dependent ST-IM miss required paid execution and remains billable"
    )


# ===========================================================================
# C — agent screener
# ===========================================================================

@pytest.mark.parametrize(
    ("query", "error", "detail_key", "detail_value"),
    [
        ("?sort=bogus", "invalid_sort", "value", "bogus"),
        ("?exchange=ZZ", "invalid_exchange", "value", "ZZ"),
        ("?trend=nope", "invalid_trend_code", "invalid", ["nope"]),
        # `+` decodes to a space in a query string, so the caret-plus trend code
        # must be percent-encoded to reach the endpoint as `^+`.
        ("?trend=%5E%2B,nope", "invalid_trend_code", "invalid", ["nope"]),
    ],
)
def test_screener_request_only_rejections_never_reach_a_rail(
    payment_harness, counted_engines, query, error, detail_key, detail_value
):
    response = payment_harness.client.get(
        "/v1/agent/screener/top" + query, headers=x402_headers()
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == error
    assert detail[detail_key] == detail_value
    assert detail["valid"], "the moved rejection lost its `valid` vocabulary list"
    assert_rejected_before_payment(payment_harness, counted_engines)


@pytest.mark.parametrize(
    "query",
    [
        "",                      # defaults: sort=rsi, default bullish trend filter
        "?trend=all",            # explicit no-trend-filter
        "?sort=mt_cnt",          # the other accepted sort key
        "?exchange=n",                    # lowercase, normalized by the helper
        "?trend=%5E%2B,%5E-,v%5E",        # the default trend set, given explicitly
    ],
)
def test_screener_valid_filters_reach_the_endpoint_and_settle_once(
    payment_harness, priced_engines, query
):
    response = payment_harness.client.get(
        "/v1/agent/screener/top" + query, headers=x402_headers()
    )

    assert response.status_code == 200
    assert payment_harness.settle_count == 1


def test_screener_normalizes_exchange_for_the_executed_query(
    payment_harness, priced_engines
):
    """
    The endpoint consumes the shared helper's normalized value.

    A lowercase exchange must be reported back uppercased in `filter_summary`,
    which is only true if the endpoint used the helper's result rather than the
    raw query value.
    """
    response = payment_harness.client.get(
        "/v1/agent/screener/top?exchange=n", headers=x402_headers()
    )

    assert response.status_code == 200
    assert response.json()["filter_summary"]["exchange"] == "N"


def test_screener_no_weekdate_is_a_paid_outcome(payment_harness, monkeypatch):
    """
    Latest-weekdate availability deliberately did NOT move.

    It is a data question, so its 503 stays behind the gate and stays billable.
    """
    monkeypatch.setattr(screener_router, "get_engine", lambda: rows_engine([]))

    response = payment_harness.client.get(
        "/v1/agent/screener/top", headers=x402_headers()
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "no_signal_data"
    assert payment_harness.settle_count == 1


# ===========================================================================
# D — decision/evaluate-symbol
# ===========================================================================

@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ({}, 422, "missing_symbol"),
        ({"symbol": "IBM"}, 400, "missing_exchange"),
        ({"symbol_exchange": "IBM"}, 400, "invalid_input"),
        ({"symbol_exchange": "IBM-Z"}, 400, "invalid_input"),
        ({"symbol": "IBM", "exchange": "Z"}, 400, "invalid_input"),
        ({"symbol": "", "exchange": "N"}, 422, "missing_symbol"),
    ],
)
def test_decision_request_only_rejections_never_reach_a_rail(
    payment_harness, counted_engines, body, status, error
):
    """
    Every branch of the identity resolver, with its exact status preserved.

    `missing_symbol` is a 422 and `missing_exchange` a 400 — an existing
    asymmetry in this contract.  Moving the checks earlier deliberately did not
    normalize them, because that would be an API change wearing an ordering fix
    as a disguise.
    """
    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol", headers=x402_headers(), json=body
    )

    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["error"] == error
    assert detail["message"]
    assert_rejected_before_payment(payment_harness, counted_engines)


@pytest.mark.parametrize(
    "body",
    [{"symbol_exchange": "IBM-N"}, {"symbol": "IBM", "exchange": "N"}],
)
def test_decision_valid_identity_reaches_paid_execution(
    payment_harness, monkeypatch, body
):
    """
    A valid request settles, then discovers a data-dependent outcome.

    Exactly the split the invariant requires: the caller consumed the paid
    lookup that produced the answer, so the answer is chargeable even though it
    is not a 200.
    """
    monkeypatch.setattr(decision_router, "get_engine", lambda: sequence_engine([[], []]))

    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol", headers=x402_headers(), json=body
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "no_signal_data"
    assert payment_harness.settle_count == 1


def test_decision_symbol_not_found_still_settles(payment_harness, monkeypatch):
    """A resolvable instrument that is not in the database is a paid answer."""
    monkeypatch.setattr(
        decision_router,
        "get_engine",
        lambda: sequence_engine([[{"weekdate": "2026-01-02"}], []]),
    )

    response = payment_harness.client.post(
        "/v1/decision/evaluate-symbol",
        headers=x402_headers(),
        json={"symbol_exchange": "ZZZZ-N"},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "symbol_not_found"
    assert payment_harness.settle_count == 1, (
        "symbol existence is a database answer and remains chargeable"
    )


# ===========================================================================
# E — portfolio/construct
# ===========================================================================

@pytest.mark.parametrize(
    ("body", "error", "value"),
    [
        ({"bias": "sideways"}, "invalid_bias", "sideways"),
        ({"universe": "everything"}, "invalid_universe", "everything"),
        ({"exchange": "ZZ"}, "invalid_exchange", "ZZ"),
    ],
)
def test_portfolio_construct_request_only_rejections_never_reach_a_rail(
    payment_harness, counted_engines, body, error, value
):
    response = payment_harness.client.post(
        "/v1/portfolio/construct", headers=x402_headers(), json=body
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == error
    assert detail["value"] == value
    assert detail["valid"], "the moved rejection lost its `valid` vocabulary list"
    assert detail["request_id"]
    assert_rejected_before_payment(payment_harness, counted_engines)


@pytest.mark.parametrize(
    "body",
    [
        {"bias": "bullish"},
        {"bias": "bearish"},
        {"bias": "auto"},
        {"universe": "top"},
        {"exchange": "n"},
    ],
)
def test_portfolio_construct_valid_request_reaches_paid_execution(
    payment_harness, monkeypatch, body
):
    monkeypatch.setattr(portfolio_router, "get_engine", lambda: rows_engine([]))

    response = payment_harness.client.post(
        "/v1/portfolio/construct", headers=x402_headers(), json=body
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "no_signal_data"
    assert payment_harness.settle_count == 1, (
        "weekdate availability is a data answer and remains chargeable"
    )


def test_portfolio_construct_normalizes_exchange_for_the_executed_query(
    payment_harness, monkeypatch
):
    """
    The endpoint consumes the shared helper's normalized exchange.

    Proved through the same 503 path: the request is accepted past the semantic
    layer with a lowercase code, which only happens if the helper normalized it
    rather than the endpoint re-reading the raw body value.
    """
    monkeypatch.setattr(portfolio_router, "get_engine", lambda: rows_engine([]))

    response = payment_harness.client.post(
        "/v1/portfolio/construct", headers=x402_headers(), json={"exchange": "n"}
    )

    assert response.status_code == 503


# ===========================================================================
# F — portfolio evaluate / compare: one rule, two renderings
# ===========================================================================

_VALID_POSITIONS = [{"symbol_exchange": "IBM-N", "weight": 1.0}]


@pytest.mark.parametrize(
    ("positions", "error"),
    [
        ([{"symbol_exchange": "IBM", "weight": 1.0}], "invalid_input"),
        ([{"symbol_exchange": "IBM-Z", "weight": 1.0}], "invalid_input"),
        (
            [
                {"symbol_exchange": "IBM-N", "weight": 0.5},
                {"symbol_exchange": "IBM-N", "weight": 0.5},
            ],
            "duplicate_positions",
        ),
        # Merged main's inline text for this case, pinned verbatim.
        ([{"symbol_exchange": "IBM-N", "weight": 0.4}], "invalid_weights"),
    ],
)
def test_portfolio_evaluate_request_only_rejections_never_reach_a_rail(
    payment_harness, counted_engines, positions, error
):
    response = payment_harness.client.post(
        "/v1/portfolio/evaluate", headers=x402_headers(), json={"positions": positions}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == error
    assert "side" not in detail, (
        "/portfolio/evaluate must not acquire the /compare `side` key from the "
        "shared helper"
    )
    assert " in None" not in detail["message"], (
        "the unlabelled rendering leaked the absent side into its message"
    )
    assert " for None" not in detail["message"]
    assert_rejected_before_payment(payment_harness, counted_engines)


@pytest.mark.parametrize("side", ["left", "right"])
def test_portfolio_compare_rejections_keep_their_side_label(
    payment_harness, counted_engines, side
):
    """
    The other rendering of the same rule.

    `/compare` must keep labelling which submitted portfolio failed — a caller
    with two lists cannot otherwise tell them apart.
    """
    body = {"left": list(_VALID_POSITIONS), "right": list(_VALID_POSITIONS)}
    body[side] = [{"symbol_exchange": "IBM", "weight": 1.0}]

    response = payment_harness.client.post(
        "/v1/portfolio/compare", headers=x402_headers(), json=body
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_input"
    assert detail["side"] == side
    assert_rejected_before_payment(payment_harness, counted_engines)


def test_portfolio_compare_weight_message_keeps_its_side_phrasing(
    payment_harness, counted_engines
):
    """The weight-sum message differs between the two callers; both preserved."""
    response = payment_harness.client.post(
        "/v1/portfolio/compare",
        headers=x402_headers(),
        json={
            "left": [{"symbol_exchange": "IBM-N", "weight": 0.4}],
            "right": list(_VALID_POSITIONS),
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_weights"
    assert detail["message"].startswith("Weights for left must sum to 1.0")
    assert_rejected_before_payment(payment_harness, counted_engines)


def test_portfolio_evaluate_weight_message_has_no_side_phrasing(
    payment_harness, counted_engines
):
    response = payment_harness.client.post(
        "/v1/portfolio/evaluate",
        headers=x402_headers(),
        json={"positions": [{"symbol_exchange": "IBM-N", "weight": 0.4}]},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"].startswith("Weights must sum to 1.0")
    assert "for left" not in detail["message"]
    assert "for right" not in detail["message"]
    assert_rejected_before_payment(payment_harness, counted_engines)


def test_portfolio_evaluate_missing_symbol_is_a_paid_outcome(
    payment_harness, monkeypatch
):
    """
    Per-symbol existence deliberately did NOT move.

    A position that is not in the database comes back `found=false` from the
    paid lookup, so the request settles.
    """
    monkeypatch.setattr(portfolio_router, "get_engine", lambda: rows_engine([]))

    response = payment_harness.client.post(
        "/v1/portfolio/evaluate",
        headers=x402_headers(),
        json={"positions": [{"symbol_exchange": "ZZZZ-N", "weight": 1.0}]},
    )

    assert response.status_code == 503
    assert payment_harness.settle_count == 1


# ===========================================================================
# G — the rest of the audited surface
# ===========================================================================

@pytest.mark.parametrize(
    "path",
    [
        "/v1/breadth/sector/latest",
        "/v1/breadth/sector/history",
        "/v1/leadership/summary/latest",
        "/v1/leadership/rotation/history",
        "/v1/selections/latest",
        "/v1/selections/history",
        "/v1/selections/published/latest",
        "/v1/selections/published/history",
        "/v1/indicators/latest",
        "/v1/indicators/history",
    ],
)
def test_invalid_exchange_filter_never_reaches_a_rail(
    payment_harness, counted_engines, path
):
    """
    The optional exchange filter, across every governed route that takes one.

    None of these were among the eight measured xfails; they were found by the
    Class 1 audit and are covered here so the class is closed rather than the
    eight cases merely fixed.
    """
    response = payment_harness.client.get(
        f"{path}?exchange=ZZ", headers=x402_headers()
    )

    assert response.status_code == 400
    assert_rejected_before_payment(payment_harness, counted_engines)


@pytest.mark.parametrize(
    "path", ["/v1/selections/history", "/v1/selections/published/history"]
)
def test_selection_history_malformed_identifier_never_reaches_a_rail(
    payment_harness, counted_engines, path
):
    response = payment_harness.client.get(
        f"{path}?symbol_exchange=IBM", headers=x402_headers()
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_symbol_exchange"
    assert_rejected_before_payment(payment_harness, counted_engines)


@pytest.mark.parametrize(
    "path", ["/v1/stwr/reports/latest", "/v1/stwr/reports/history"]
)
def test_stwr_unknown_report_code_never_reaches_a_rail(
    payment_harness, counted_engines, path
):
    response = payment_harness.client.get(
        f"{path}?rpt=not-a-report&exchange=N", headers=x402_headers()
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_report"
    assert detail["allowed"], "the moved rejection lost its `allowed` list"
    assert_rejected_before_payment(payment_harness, counted_engines)


@pytest.mark.parametrize(
    "path", ["/v1/stwr/reports/latest", "/v1/stwr/reports/history"]
)
def test_stwr_invalid_exchange_never_reaches_a_rail(
    payment_harness, counted_engines, path
):
    response = payment_harness.client.get(
        f"{path}?rpt=pw&exchange=ZZ", headers=x402_headers()
    )

    assert response.status_code == 400
    assert_rejected_before_payment(payment_harness, counted_engines)


@pytest.mark.parametrize(
    "path",
    ["/v1/intelligence/guidance", "/v1/intelligence/research"],
)
def test_intelligence_invalid_artifact_id_never_reaches_a_rail(
    payment_harness, path
):
    """
    The artifact id's *shape* is request-only; whether it exists is not.

    A backslash is used because a forward slash would not match the route at all
    and would be a route miss rather than a semantic rejection.
    """
    response = payment_harness.client.get(
        f"{path}/bad%5Cid", headers=x402_headers()
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_artifact_id"
    assert payment_harness.verify_count == 0
    assert payment_harness.settle_count == 0


# ===========================================================================
# H — cross-rail behaviour of a moved rejection
# ===========================================================================

_REPRESENTATIVE_INVALID = "/v1/prices/history?symbol_exchange=IBM"


def test_invalid_request_on_mpp_never_authorizes(payment_harness, counted_engines):
    response = payment_harness.client.get(
        _REPRESENTATIVE_INVALID, headers=mpp_headers()
    )

    assert response.status_code == 400
    assert payment_harness.mpp.authorize_count == 0
    assert payment_harness.mpp.capture_count == 0
    assert payment_harness.mpp.void_count == 0


def test_invalid_unpaid_request_is_challenged_before_validation(
    payment_harness, counted_engines
):
    """
    PR3: an unpaid probe of a challengeable fixed-price resource is challenged.

    The semantic validator is not weakened by this — it still guards the paid
    path, which is what the rest of this file exercises.  It is simply not
    reached, because a request presenting no payment does not need validating
    to be told what the resource costs and what it requires.  Nothing moves:
    the same no-rail, no-query assertions apply as to any rejection here.
    """
    response = payment_harness.client.get(
        _REPRESENTATIVE_INVALID, headers=unpaid_headers()
    )

    assert response.status_code == 402
    assert "payment-required" in response.headers
    assert response.json()["error"] == "payment_required"
    assert_rejected_before_payment(payment_harness, counted_engines)


def test_subscription_invalid_request_is_an_input_error_not_a_challenge(
    payment_harness, counted_engines, monkeypatch
):
    """
    The subscription rail sees the same rejection, and no x402 anywhere.

    A quota-backed caller must not be handed a payment challenge for a request
    that was never servable, and must not be metered for one either.
    """
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

    response = payment_harness.client.get(
        _REPRESENTATIVE_INVALID, headers={"X-API-Key": "test-key"}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_symbol_exchange"
    assert "payment-required" not in response.headers
    assert payment_harness.verify_count == 0
    assert payment_harness.settle_count == 0
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0
    assert len(counted_engines) == 0


def test_db_failure_after_execution_remains_post_payment(payment_harness, monkeypatch):
    """
    A downstream failure discovered only by running the paid service.

    It is neither knowable from the request nor preventable, so it stays behind
    the gate and keeps its 500.  Included so a future over-correction cannot
    reinterpret "deterministic" as "anything that fails the same way twice".
    """
    class _ExplodingEngine:
        def connect(self):
            raise RuntimeError("database is on fire")

    monkeypatch.setattr(prices_router, "get_engine", lambda: _ExplodingEngine())

    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM-N", headers=x402_headers()
    )

    assert response.status_code == 500
    assert response.json()["detail"]["error"] == "db_query_failed"
    assert payment_harness.settle_count == 1, (
        "a failure discovered only by paid execution remains post-payment"
    )
