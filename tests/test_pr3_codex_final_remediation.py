"""
Final PR3 remediation — the two blockers from the second Codex review.

BLOCKER 1 (P1).  MPP capture is called from the request finaliser *after* the
downstream response is produced, because `is_billable` must reflect whether the
rail actually collected.  The successful-response capture branch did not guard
exceptions, so a raising capture escaped the finaliser: the successful paid
response was destroyed, and neither the `api_request_logs` row nor the
`api_request_economics` row was ever written.  A request that authorized against
a live session left no local record of what happened to it.

Codex reached it through the real response parser: HTTP 200 with a non-object
JSON body, which `capture_mpp_payment` read with `.get()`.  The void branch had
always been guarded; capture had not.

BLOCKER 2 (P2).  `has_payment_signature` tested raw header truthiness while
`extract_payment_signature` normalized with `.strip()` and required a non-empty
result.  A whitespace-only `X-Payment` was therefore "payment presented" to the
early-challenge guard and "no artifact" to the facilitator path — two
definitions of the same fact.  The behavioural consequence was the exact failure
PR3 exists to remove: a bare canonical probe carrying a blank proof header
skipped the challenge and received `400 missing_required_param`, while no
consumable artifact existed anywhere in the request.

As elsewhere in this repository, status codes are never the whole assertion:
collection is measured against the facilitator and MPP spies, and endpoint
execution against a query counter.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from support.payment_harness import (
    AGENT_HEADERS,
    SENTINEL_UNIT_PRICE_USD,
    counting_engine,
    mpp_headers,
    rows_engine,
    x402_headers,
)

import payments.mpp_client as mpp_client_module
import routers.prices as prices_router

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
    monkeypatch.setattr(prices_router, "get_engine", lambda: rows_engine([_PRICE_ROW]))


# ===========================================================================
# BLOCKER 1 — a capture exception must not erase the response or the records
# ===========================================================================

@pytest.fixture
def raising_capture(payment_harness, monkeypatch):
    """
    Capture that raises, counted.

    Patched onto `payments.mpp_client` after the harness spy, because that is
    the binding the finaliser resolves — it imports `capture_mpp_payment` inside
    the function precisely so this substitution is observed.  The raised type
    mirrors what the real parser produced: `.get()` on a non-object body.
    """
    attempts: list[int] = []

    def _raising(**_kwargs):
        attempts.append(1)
        raise AttributeError("'str' object has no attribute 'get'")

    monkeypatch.setattr(mpp_client_module, "capture_mpp_payment", _raising)
    return attempts


def test_01_capture_exception_preserves_the_response_and_both_records(
    payment_harness, priced_engines, raising_capture
):
    """
    Requirement A.

    The endpoint already succeeded and the caller is entitled to that answer; a
    control-plane problem discovered afterwards cannot retract it.  What the
    failure must change is the accounting, not the response.
    """
    response = payment_harness.client.get(_VALID, headers=mpp_headers())

    # The paid answer survives, intact.
    assert response.status_code == 200, (
        "a raising capture destroyed a successful paid response"
    )
    assert response.json()["symbol_exchange"] == "IBM-N"

    # Capture was attempted exactly once, and nothing else was contacted.
    assert payment_harness.mpp.authorize_count == 1
    assert len(raising_capture) == 1, (
        f"capture was attempted {len(raising_capture)} times; it must run once"
    )
    assert payment_harness.verify_count == 0, "x402 verify ran on an MPP request"
    assert payment_harness.settle_count == 0, "x402 settle ran on an MPP request"

    # Observability survives: exactly one of each row, no duplicates.
    event = payment_harness.logs.only_event_row()
    row = payment_harness.logs.only_economics_row()

    assert row["payment_status"] == "capture_failed", (
        "an unresolved capture was recorded as something other than a failure"
    )
    assert row["billed_amount_usd"] == 0, (
        "collection was claimed for a capture whose outcome is unknown"
    )
    assert event["is_billable"] == 0, (
        "the request-event row claims billable usage for an uncollected request"
    )
    assert event["status_code"] == 200
    assert event["success"] == 1, (
        "the endpoint did succeed; the capture failure is an accounting fact, "
        "not a service failure"
    )


def test_01b_capture_exception_is_logged_operationally(
    payment_harness, priced_engines, raising_capture, caplog
):
    """
    A swallowed exception that left no trace would be worse than the crash.

    The failure must reach the operator log with the identifiers needed to
    reconcile the reservation.
    """
    import logging

    with caplog.at_level(logging.ERROR, logger="stocktrends_api.metering"):
        response = payment_harness.client.get(_VALID, headers=mpp_headers())

    assert response.status_code == 200

    records = [
        record for record in caplog.records
        if "mpp capture raised" in record.getMessage()
    ]
    assert records, "a raising capture was swallowed without an operator log line"

    message = records[0].getMessage()
    assert "mpp-acceptance-ref" in message, (
        "the capture failure log carries no payment reference to reconcile with"
    )
    assert records[0].exc_info is not None, (
        "the exception traceback was not attached to the log record"
    )


def test_02_ordinary_capture_failure_is_unchanged(payment_harness, priced_engines):
    """
    Requirement B: the already-correct path stays correct.

    A control plane that answers cleanly with a refusal produces the same
    accounting as one that raises — the difference is in how it was learned, not
    in what is owed.
    """
    payment_harness.mpp.capture_success = False

    response = payment_harness.client.get(_VALID, headers=mpp_headers())

    assert response.status_code == 200
    assert payment_harness.mpp.authorize_count == 1
    assert payment_harness.mpp.capture_count == 1
    assert payment_harness.mpp.void_count == 0

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "capture_failed"
    assert row["billed_amount_usd"] == 0
    assert payment_harness.logs.only_event_row()["is_billable"] == 0


def test_03_successful_capture_positive_control(payment_harness, priced_engines):
    """
    Requirement C.

    Without this, every assertion above would pass just as well if capture had
    been disabled outright.
    """
    response = payment_harness.client.get(_VALID, headers=mpp_headers())

    assert response.status_code == 200
    assert payment_harness.mpp.authorize_count == 1
    assert payment_harness.mpp.capture_count == 1
    assert payment_harness.mpp.void_count == 0

    row = payment_harness.logs.only_economics_row()
    assert row["payment_status"] == "captured"
    assert row["billed_amount_usd"] == SENTINEL_UNIT_PRICE_USD
    assert payment_harness.logs.only_event_row()["is_billable"] == 1


def test_04_only_a_confirmed_capture_claims_collection(
    payment_harness, priced_engines, monkeypatch
):
    """
    Collection follows the capture, never the authorization.

    Authorization succeeding is a reservation, not a payment.  This is asserted
    across all three capture outcomes at once so a future edit that sets the
    collected amount before the result is known fails here.
    """
    outcomes = {}

    payment_harness.client.get(_VALID, headers=mpp_headers())
    outcomes["captured"] = payment_harness.logs.only_economics_row()["billed_amount_usd"]

    payment_harness.logs.economics.clear()
    payment_harness.logs.events.clear()
    payment_harness.mpp.capture_success = False
    payment_harness.client.get(_VALID, headers=mpp_headers())
    outcomes["refused"] = payment_harness.logs.only_economics_row()["billed_amount_usd"]

    payment_harness.logs.economics.clear()
    payment_harness.logs.events.clear()
    monkeypatch.setattr(
        mpp_client_module,
        "capture_mpp_payment",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("control plane exploded")),
    )
    payment_harness.client.get(_VALID, headers=mpp_headers())
    outcomes["raised"] = payment_harness.logs.only_economics_row()["billed_amount_usd"]

    assert outcomes["captured"] == SENTINEL_UNIT_PRICE_USD
    assert outcomes["refused"] == 0, "a refused capture claimed collection"
    assert outcomes["raised"] == 0, "an unresolved capture claimed collection"


def test_05_capture_guard_mirrors_the_void_guard():
    """
    Structural: both control-plane calls in the finaliser are exception-safe.

    The void branch was guarded from the start and capture was not; the two sit
    in the same `if/else` and carry the same obligation not to disturb a
    response that has already been produced.
    """
    import ast
    import inspect
    import textwrap

    import middleware.metering as metering_module

    source = textwrap.dedent(
        inspect.getsource(metering_module.MeteringMiddleware.dispatch)
    )
    tree = ast.parse(source)

    def _calls_within(node) -> set[str]:
        names = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                names.add(inner.func.id)
        return names

    guarded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and node.handlers:
            for statement in node.body:
                guarded |= _calls_within(statement)

    for control_plane_call in ("capture_mpp_payment", "void_mpp_authorization"):
        assert control_plane_call in guarded, (
            f"{control_plane_call} is called outside an exception guard in the "
            "request finaliser; a raise there destroys the already-produced "
            "response and both log rows"
        )

    assert "mpp capture raised after successful response" in source


# ===========================================================================
# BLOCKER 1D/E — the control-plane client never assumes an object body
# ===========================================================================

_NON_OBJECT_BODIES = ["unexpected", [], 123, True, None]


@pytest.fixture
def fake_control_plane(monkeypatch):
    """Drive the real parsers with a controllable transport result."""

    def _configure(status: int, data):
        monkeypatch.setattr(
            mpp_client_module,
            "_mpp_post",
            lambda _endpoint, _payload: (status, data, "raw-body"),
        )

    return _configure


def _capture():
    return mpp_client_module.capture_mpp_payment(
        channel_id="chan",
        payment_reference="ref",
        captured_stc=Decimal("0.15"),
        pricing_rule_id="prices_history_paid",
        request_id="req",
    )


def _authorize():
    return mpp_client_module.authorize_mpp_payment(
        channel_id="chan",
        payment_reference="ref",
        requested_stc=Decimal("0.15"),
        pricing_rule_id="prices_history_paid",
        path=_BARE,
        request_id="req",
    )


def _void():
    return mpp_client_module.void_mpp_authorization(
        payment_reference="ref",
        request_id="req",
    )


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES, ids=lambda b: repr(b))
def test_06_capture_returns_structured_failure_for_a_non_object_body(
    fake_control_plane, body
):
    """
    Requirement D, against the real parser.

    JSON permits a string, number, list, boolean or null at the top level.  None
    of them is the authorization object capture expects, and reading fields off
    one raised — inside the request finaliser.  The outcome is unknown, so it is
    a structured failure; silently coercing it into success would claim a
    capture that may never have happened.
    """
    fake_control_plane(200, body)

    result = _capture()

    assert result.success is False, f"a {type(body).__name__} body was read as success"
    assert result.error_code == mpp_client_module.INVALID_CONTROL_PLANE_RESPONSE_ERROR
    assert result.error_detail
    assert type(body).__name__ in result.error_detail, (
        "the failure detail does not say what shape was actually received"
    )


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES, ids=lambda b: repr(b))
@pytest.mark.parametrize(
    ("label", "call"),
    [("authorize", _authorize), ("void", _void)],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_07_authorize_and_void_share_the_hardening(
    fake_control_plane, body, label, call
):
    """
    Requirement E: the same `.get()`-on-a-non-object defect existed in both.

    Verified present before the fix in authorize and void as well as capture, on
    both the 2xx and the >=400 path.  The smallest safe helper covers all three,
    so all three use it; nothing else about MPP is redesigned.
    """
    fake_control_plane(200, body)

    result = call()

    assert result.success is False, label
    assert result.error_code == mpp_client_module.INVALID_CONTROL_PLANE_RESPONSE_ERROR, label


@pytest.mark.parametrize("body", ["unexpected", 123, True], ids=lambda b: repr(b))
@pytest.mark.parametrize(
    ("label", "call", "expected_code"),
    [
        ("capture", _capture, "capture_failed"),
        ("authorize", _authorize, "authorization_failed"),
        ("void", _void, "void_failed"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_08_error_responses_with_a_non_object_body_do_not_raise(
    fake_control_plane, body, label, call, expected_code
):
    """
    The error path read the same fields off the same body, and raised too.

    A 5xx whose body is not an object must still produce the operation's normal
    failure code rather than an exception.
    """
    fake_control_plane(500, body)

    result = call()

    assert result.success is False, label
    assert result.error_code == expected_code, label


def test_09_a_real_object_body_still_parses_normally(fake_control_plane):
    """Positive control: hardening rejects bad shapes, not good ones."""
    fake_control_plane(200, {"status": "captured", "captured_at": "2026-09-05T00:00:00Z"})
    assert _capture().success is True

    fake_control_plane(200, {"status": "pending", "id": "auth-1"})
    assert _authorize().success is True

    fake_control_plane(200, {"status": "voided"})
    assert _void().success is True

    # An object with no useful fields remains the pre-existing "unexpected
    # status" failure, not the new invalid-shape one.
    fake_control_plane(200, {})
    empty = _capture()
    assert empty.success is False
    assert empty.error_code == "capture_failed"


# ===========================================================================
# BLOCKER 2 — one normalized definition of an x402 artifact
# ===========================================================================

#: Whitespace that the ASCII-only HTTP header encoding actually transmits.
_TRANSMITTABLE_BLANKS = [
    ("absent", None),
    ("empty", ""),
    ("spaces", "   "),
    ("tab", "\t"),
    ("mixed OWS", " \t "),
    ("folded", "\n"),
]

_PROOF_CARRIERS = ["X-Payment", "PAYMENT-SIGNATURE", "x-payment", "payment-signature"]


@pytest.mark.parametrize("carrier", _PROOF_CARRIERS)
@pytest.mark.parametrize(
    ("label", "value"), _TRANSMITTABLE_BLANKS, ids=[case[0] for case in _TRANSMITTABLE_BLANKS]
)
def test_10_a_blank_proof_carrier_never_suppresses_the_challenge(
    payment_harness, monkeypatch, carrier, label, value
):
    """
    The reproduction, inverted, through the real stack.

    A carrier that normalizes to nothing is not an artifact.  The caller holds
    no payment, so the bare canonical URL must answer with the payment contract
    — the same answer it gives when the header is absent entirely.

    Header casing is varied because `_get_header` is the only thing making the
    lookup case-insensitive, and a regression there would be invisible to a
    test that only ever sent canonical casing.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    headers = dict(AGENT_HEADERS)
    if value is not None:
        headers[carrier] = value

    response = payment_harness.client.get(_BARE, headers=headers)

    context = f"{carrier}/{label}"
    assert response.status_code == 402, (
        f"{context}: a blank proof carrier suppressed the challenge and the "
        "caller received an application error while holding no artifact"
    )
    assert response.json()["error"] == "payment_required", context
    assert "payment-required" in response.headers, context
    assert response.json()["payment_required"]["x402Version"] == 2, context
    assert len(queries) == 0, f"{context}: the paid endpoint executed"
    assert payment_harness.verify_count == 0, context
    assert payment_harness.settle_count == 0, context
    assert payment_harness.mpp.authorize_count == 0, context
    assert payment_harness.mpp.capture_count == 0, context
    assert payment_harness.mpp.void_count == 0, context


def test_10b_non_ascii_whitespace_normalizes_away_at_the_predicate(payment_harness):
    """
    Non-ASCII whitespace, where it is representable.

    The test client encodes header values as ASCII and rejects `\\u00a0` and
    `\\u2003` before the application sees them, so this cannot be asserted over
    HTTP here.  The normalization contract is pinned at the predicate instead:
    `str.strip()` removes every character Python considers whitespace, so a
    proxy or client that does transmit one produces no artifact.
    """
    from payments.challenge import presents_x402_payment_proof
    from payments.x402 import extract_payment_signature, has_payment_signature

    for value in (" ", " ", "　", "  \t "):
        headers = {"X-Payment": value}
        assert extract_payment_signature(headers) is None, repr(value)
        assert has_payment_signature(headers) is False, repr(value)
        assert presents_x402_payment_proof(headers) is False, repr(value)


@pytest.mark.parametrize("carrier", ["X-Payment", "PAYMENT-SIGNATURE"])
@pytest.mark.parametrize(
    ("label", "url", "expected"),
    [
        ("bare", _BARE, 400),
        ("semantic invalid", _SEMANTIC_INVALID, 400),
        ("structural invalid", _STRUCTURAL_INVALID, 422),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_11_a_nonblank_malformed_artifact_is_payment_bearing(
    payment_harness, monkeypatch, carrier, label, url, expected
):
    """
    Requirement 7, and the distinction that matters.

    "Unextractable" and "malformed but present" are different states.  A
    non-blank value IS an artifact: the caller is attempting to pay, so the
    request takes the validated path and an incomplete one gets its input error
    — settling nothing.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    headers = dict(AGENT_HEADERS)
    headers[carrier] = "  not-a-valid-artifact  "

    response = payment_harness.client.get(url, headers=headers)

    context = f"{carrier}/{label}"
    assert response.status_code == expected, context
    assert len(queries) == 0, f"{context}: the paid endpoint executed"
    assert payment_harness.verify_count == 0, context
    assert payment_harness.settle_count == 0, context
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0, context


@pytest.mark.parametrize("carrier", ["X-Payment", "PAYMENT-SIGNATURE"])
def test_12_a_nonblank_malformed_artifact_with_valid_input_is_rejected_as_payment(
    payment_harness, monkeypatch, carrier
):
    """
    Requirement 8.

    With nothing wrong with the request itself, the artifact is judged — and
    found invalid.  That is a payment rejection, not a challenge, and it still
    reaches neither the facilitator nor the endpoint.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    headers = dict(AGENT_HEADERS)
    headers[carrier] = "not-a-valid-artifact"

    response = payment_harness.client.get(_VALID, headers=headers)

    assert response.status_code == 402, carrier
    assert response.json()["error"] != "payment_required", (
        f"{carrier}: a present-but-malformed artifact was answered with a "
        "challenge; it is a rejected payment, not an absent one"
    )
    assert len(queries) == 0, f"{carrier}: the paid endpoint executed"
    assert payment_harness.verify_count == 0, carrier
    assert payment_harness.settle_count == 0, carrier
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0


def test_13_a_valid_artifact_still_settles_exactly_once(payment_harness, priced_engines):
    """Requirement 9: existing valid x402 behaviour is untouched."""
    response = payment_harness.client.get(_VALID, headers=x402_headers())

    assert response.status_code == 200
    assert payment_harness.verify_count == 1
    assert payment_harness.settle_count == 1
    assert "payment-response" in response.headers
    assert payment_harness.logs.only_economics_row()["payment_status"] == "settled"


def test_14_a_blank_carrier_does_not_hide_a_real_one(payment_harness, priced_engines):
    """
    Carrier order must not let a blank header mask a genuine artifact.

    `extract_payment_signature` walks `X402_PROOF_HEADERS` in order; a blank one
    has to keep the search going rather than terminate it, or a client that sent
    both would be treated as unpaid.
    """
    from payments.x402 import extract_payment_signature
    from payments.x402_contract import X402_PROOF_HEADERS

    blank, real = X402_PROOF_HEADERS[0], X402_PROOF_HEADERS[1]
    headers = {blank: "   ", real: "a-real-artifact"}

    assert extract_payment_signature(headers) == "a-real-artifact"

    request_headers = dict(AGENT_HEADERS)
    request_headers[blank] = "   "
    request_headers[real] = "a-real-artifact"

    response = payment_harness.client.get(_BARE, headers=request_headers)
    assert response.status_code == 400, (
        "a blank carrier masked a real artifact; the request was treated as "
        "unpaid and challenged instead of validated"
    )


# ---------------------------------------------------------------------------
# The four predicates and enforcement are one definition
# ---------------------------------------------------------------------------

def _carrier_cases() -> list[tuple[str, dict]]:
    from payments.x402_contract import X402_PROOF_HEADERS

    cases: list[tuple[str, dict]] = [
        ("nothing", {}),
        ("agent only", {"X-StockTrends-Agent-Id": "agent"}),
        ("rail hint", {"Authorization": "x402 something"}),
        ("rail declaration", {"X-StockTrends-Payment-Method": "x402"}),
        ("metadata only", {"X-StockTrends-Payment-Reference": "ref"}),
    ]
    for header in X402_PROOF_HEADERS:
        for label, value in (
            ("empty", ""),
            ("spaces", "   "),
            ("tab", "\t"),
            ("newline", "\n"),
            ("nbsp", " "),
            ("em space", " "),
            ("artifact", "artifact"),
            ("padded artifact", "  artifact  "),
            ("malformed artifact", "not-a-payment"),
        ):
            cases.append((f"{header}={label}", {header: value}))
            cases.append((f"{header.lower()}={label}", {header.lower(): value}))
    cases.append(
        (
            "blank first carrier plus real second",
            {X402_PROOF_HEADERS[0]: "  ", X402_PROOF_HEADERS[1]: "artifact"},
        )
    )
    return cases


@pytest.mark.parametrize(
    ("label", "headers"), _carrier_cases(), ids=[case[0] for case in _carrier_cases()]
)
def test_15_presence_and_extraction_are_one_definition(label, headers):
    """
    The invariant, over every representative carrier and value.

    Extraction is part of the assertion, not merely compared against another
    boolean: both booleans could share the same defect, and in the reported
    version they did not agree with the extractor at all.

    Payment proof is present IFF a canonical carrier holds a non-empty artifact
    *after the same normalization used for extraction*.
    """
    from payments.challenge import presents_x402_payment_proof
    from payments.x402 import (
        extract_payment_signature,
        has_payment_signature,
        has_x402_payment_proof,
    )

    extracted = extract_payment_signature(headers)
    expected = extracted is not None

    assert has_payment_signature(headers) is expected, label
    assert has_x402_payment_proof(headers) is expected, label
    assert presents_x402_payment_proof(headers) is expected, label

    if extracted is not None:
        assert extracted == extracted.strip(), (
            f"{label}: the extracted artifact was not normalized"
        )
        assert extracted, f"{label}: an empty artifact was extracted"


@pytest.mark.parametrize(
    ("label", "headers"), _carrier_cases(), ids=[case[0] for case in _carrier_cases()]
)
def test_16_enforcement_agrees_with_the_early_guard(label, headers):
    """
    The same invariant, expressed as behaviour rather than as predicates.

    `enforce_x402_payment` takes its no-proof branch — the challenge — exactly
    when the early guard says the caller is unpaid.  Asserted against the
    enforcement function itself so the claim survives a change in how it decides.
    """
    from payments.challenge import presents_x402_payment_proof
    from payments.enforcement import enforce_x402_payment

    result = enforce_x402_payment(
        headers=headers,
        path=_BARE,
        method="GET",
        amount_usd=Decimal("0.15"),
        validation_valid=True,
        validation_error=None,
        validation_detail=None,
        validated_payment_reference=None,
        validated_payment_network=None,
        validated_payment_token=None,
        validated_payment_amount_native=None,
        replay_checker=lambda _reference: False,
    )

    enforcement_says_unpaid = result.outcome == "challenge"
    guard_says_paid = presents_x402_payment_proof(headers)

    assert enforcement_says_unpaid is (not guard_says_paid), (
        f"{label}: the early-challenge guard says paid={guard_says_paid} while "
        f"enforcement took the {result.outcome!r} branch; one request would be "
        "challenged and settled under two different definitions of payment"
    )


def test_17_presence_is_not_defined_by_raw_header_truthiness():
    """
    Structural: `has_payment_signature` derives from the extractor.

    Re-deriving it from raw header truthiness is what created the second
    definition, and the two only diverge on values a test that used realistic
    artifacts would never produce.
    """
    import inspect

    import payments.x402 as x402_module

    source = inspect.getsource(x402_module.has_payment_signature)
    body = source.split('"""')[-1]

    assert "extract_payment_signature(" in body, (
        "has_payment_signature no longer derives from the extractor; presence "
        "and extractability can drift apart again"
    )
    assert "X402_PROOF_HEADERS" not in body, (
        "has_payment_signature walks the carrier list itself instead of "
        "delegating; that is a second normalization"
    )
