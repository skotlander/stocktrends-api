"""
Measurement harness for the settlement-ordering acceptance suite.

The governing invariant under test:

    No deterministic client-input failure or route-miss condition knowable
    before paid service execution may cause x402 settlement.

Status codes cannot prove that invariant — a 400 says nothing about whether
money moved.  Every assertion in the acceptance suite is therefore made against
the facilitator functions themselves.

Where the spies attach, and why
-------------------------------
`payments/enforcement.py` binds the facilitator functions at import time::

    from payments.x402 import settle_with_facilitator, verify_with_facilitator

so patching `payments.x402.settle_with_facilitator` would leave the name that
`enforce_x402_payment` actually calls untouched.  The spies therefore attach to
`payments.enforcement`, which is the binding the enforcement path resolves.
`assert_facilitator_bindings_intact()` guards that reasoning: it fails if the
import style in `payments/enforcement.py` ever changes so that the spy would
silently stop observing real calls.

Deliberately NOT spied
----------------------
`enforce_payment_rail` is left alone.  The existing suite replaces it wholesale,
which is fine for accounting tests but would mask the exact ordering this suite
exists to prove.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import main
import middleware.api_key as api_key_module
import middleware.metering as metering_module
import payments.enforcement as enforcement_module
import payments.mpp_client as mpp_client_module
import pricing.classifier as classifier_module
from payments.mpp_client import MppControlPlaneResult
from payments.x402 import X402ValidationResult

# ---------------------------------------------------------------------------
# Canonical test economics
#
# The three economics fields carry DELIBERATELY DISTINCT sentinel values.
# `resolve_economic_amounts` returns them in the order
# (unit_price_usd, billed_amount_usd, stc_cost), and in production all three
# happen to be the same number for a paid rule — which would let an accidental
# field swap pass every assertion unnoticed.  PR2 changes how the billed amount
# is derived, so distinguishing the three is what makes that change reviewable.
#
#   A  quoted / list unit price   -> unit_price_usd
#   B  initial billed value       -> billed_amount_usd (zeroed when uncollected)
#   C  STC analytical cost        -> stc_cost
#
# A must stay consistent with UNIT_PRICE_ATOMIC: it is the amount x402 payment
# validation requires, and the MPP minimum-amount check compares against it.
# B and C are free values chosen only to be visibly different from A and
# from each other.
# ---------------------------------------------------------------------------

SENTINEL_UNIT_PRICE_USD = Decimal("0.15")     # A
SENTINEL_BILLED_AMOUNT_USD = Decimal("0.21")  # B
SENTINEL_STC_COST = Decimal("0.37")           # C

# Retained under its original name because A is also the real quoted price the
# payment artifact must satisfy, not merely a sentinel.
UNIT_PRICE_USD = SENTINEL_UNIT_PRICE_USD
UNIT_PRICE_ATOMIC = "150000"          # 0.15 USDC at 6 decimals

assert len({SENTINEL_UNIT_PRICE_USD, SENTINEL_BILLED_AMOUNT_USD, SENTINEL_STC_COST}) == 3, (
    "economics sentinels must be pairwise distinct or field swaps go undetected"
)
PAYMENT_TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAYMENT_NETWORK = "eip155:8453"
SETTLEMENT_TX = "0x3ed456f52d6a6c330534e7544b4bb6d1c14af770ee08097ef3b012b4d27c3189"

AGENT_HEADERS = {
    "X-StockTrends-Agent-Id": "acceptance-suite-agent",
    "X-StockTrends-Agent-Vendor": "stocktrends-tests",
}


# ---------------------------------------------------------------------------
# Request header builders
# ---------------------------------------------------------------------------

def x402_payment_payload(
    *,
    amount: str = UNIT_PRICE_ATOMIC,
    reference: str = "x402-acceptance-ref",
) -> dict[str, Any]:
    """A canonical x402 V2 `exact` payload with a stable payment identifier."""
    return {
        "x402Version": 2,
        "scheme": "exact",
        "network": PAYMENT_NETWORK,
        "asset": PAYMENT_TOKEN,
        "paymentIdentifier": reference,
        "payload": {
            "authorization": {
                "from": "0xbuyer",
                "to": "0xseller",
                "value": amount,
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0xnonce",
            },
            "signature": "0xsig",
        },
    }


def x402_headers(
    *,
    amount: str = UNIT_PRICE_ATOMIC,
    reference: str = "x402-acceptance-ref",
    declare_method: bool = True,
) -> dict[str, str]:
    """Headers for a well-formed x402 payment attempt."""
    headers = dict(AGENT_HEADERS)
    headers["X-Payment"] = json.dumps(
        x402_payment_payload(amount=amount, reference=reference),
        separators=(",", ":"),
    )
    if declare_method:
        headers["X-StockTrends-Payment-Method"] = "x402"
    return headers


def malformed_x402_headers() -> dict[str, str]:
    """Headers presenting a payment artifact that cannot be decoded."""
    headers = dict(AGENT_HEADERS)
    headers["X-Payment"] = "this-is-not-a-payment-payload"
    headers["X-StockTrends-Payment-Method"] = "x402"
    return headers


def unpaid_headers() -> dict[str, str]:
    """An identified agent presenting no payment artifact at all."""
    return dict(AGENT_HEADERS)


def mpp_headers(
    *,
    amount: str = "0.15",
    reference: str = "mpp-acceptance-ref",
    channel_id: str = "mpp-channel-1",
) -> dict[str, str]:
    headers = dict(AGENT_HEADERS)
    headers.update(
        {
            "X-StockTrends-Payment-Method": "mpp",
            "X-StockTrends-Payment-Network": "stocktrends-mpp",
            "X-StockTrends-Payment-Reference": reference,
            "X-StockTrends-Payment-Amount": amount,
            "X-StockTrends-Payment-Channel-Id": channel_id,
        }
    )
    return headers


# ---------------------------------------------------------------------------
# Spies
# ---------------------------------------------------------------------------

@dataclass
class FacilitatorSpy:
    """Records every facilitator verify/settle call made during a request."""

    verify_calls: list[dict[str, Any]] = field(default_factory=list)
    settle_calls: list[dict[str, Any]] = field(default_factory=list)
    verify_valid: bool = True
    settle_valid: bool = True

    @property
    def verify_count(self) -> int:
        return len(self.verify_calls)

    @property
    def settle_count(self) -> int:
        return len(self.settle_calls)

    def reset(self) -> None:
        self.verify_calls.clear()
        self.settle_calls.clear()


@dataclass
class MppSpy:
    """Records control-plane authorize / capture / void calls."""

    authorize_calls: list[dict[str, Any]] = field(default_factory=list)
    capture_calls: list[dict[str, Any]] = field(default_factory=list)
    void_calls: list[dict[str, Any]] = field(default_factory=list)
    authorize_success: bool = True
    capture_success: bool = True
    void_success: bool = True

    @property
    def authorize_count(self) -> int:
        return len(self.authorize_calls)

    @property
    def capture_count(self) -> int:
        return len(self.capture_calls)

    @property
    def void_count(self) -> int:
        return len(self.void_calls)


@dataclass
class LogSpy:
    """Captures the rows the metering layer would have written."""

    events: list[dict[str, Any]] = field(default_factory=list)
    economics: list[dict[str, Any]] = field(default_factory=list)

    def only_economics_row(self) -> dict[str, Any]:
        assert len(self.economics) == 1, (
            f"expected exactly one economics row, got {len(self.economics)}: "
            f"{[row.get('payment_status') for row in self.economics]}"
        )
        return self.economics[0]


@dataclass
class PaymentHarness:
    """Everything a settlement-ordering test needs, in one place."""

    client: TestClient
    facilitator: FacilitatorSpy
    mpp: MppSpy
    logs: LogSpy
    used_payment_references: set[str]

    @property
    def settle_count(self) -> int:
        return self.facilitator.settle_count

    @property
    def verify_count(self) -> int:
        return self.facilitator.verify_count

    def mark_reference_used(self, reference: str) -> None:
        """Make the replay checker treat `reference` as already spent."""
        self.used_payment_references.add(reference)


# ---------------------------------------------------------------------------
# Binding guard
# ---------------------------------------------------------------------------

def assert_facilitator_bindings_intact() -> None:
    """
    Fail loudly if the spy attach points stop being the ones actually invoked.

    `enforce_x402_payment` resolves `verify_with_facilitator` and
    `settle_with_facilitator` from `payments.enforcement`'s module globals.  If
    that ever changes to a qualified `x402.settle_with_facilitator(...)` call,
    the spies would keep recording zero calls while real settlements happened —
    the worst possible failure mode for this suite.
    """
    import inspect

    source = inspect.getsource(enforcement_module.enforce_x402_payment)
    assert "verify_with_facilitator(" in source, (
        "enforce_x402_payment no longer calls verify_with_facilitator by bare "
        "name; the FacilitatorSpy attach point is stale."
    )
    assert "settle_with_facilitator(" in source, (
        "enforce_x402_payment no longer calls settle_with_facilitator by bare "
        "name; the FacilitatorSpy attach point is stale."
    )
    for name in ("verify_with_facilitator", "settle_with_facilitator"):
        assert hasattr(enforcement_module, name), (
            f"payments.enforcement.{name} is missing; the spy cannot attach."
        )


MPP_CONTROL_PLANE_CALLS = (
    "authorize_mpp_payment",
    "capture_mpp_payment",
    "void_mpp_authorization",
)


def assert_mpp_bindings_intact() -> None:
    """
    Fail loudly if the MppSpy attach points stop being the ones actually invoked.

    The MPP control-plane functions are imported *inside* the functions that use
    them — `authorize_mpp_payment` inside `payments.mpp.enforce_mpp_payment`, and
    `capture_mpp_payment` / `void_mpp_authorization` inside the metering
    finaliser.  A function-local import resolves through `payments.mpp_client`'s
    module globals on every call, which is what makes the spy effective.

    Promoting any of them to a module-level `from payments.mpp_client import ...`
    in a calling module would bind the name at import time, and monkeypatching
    `payments.mpp_client` afterwards would no longer be observed.  The harness
    would then record zero authorize/capture/void calls while real ones
    happened — the same silent failure the facilitator guard exists to prevent.

    The check is structural rather than textual: it asserts where the names live,
    not how the import statements are formatted.
    """
    # Scope: these two modules are the complete set of known production callers
    # of the MPP control plane today -- `payments.mpp` for authorize, and the
    # `middleware.metering` finaliser for capture and void.  The enumeration is
    # deliberately explicit rather than a repository-wide scan.  A newly
    # introduced caller must be added here, or the guard will not cover it.
    import middleware.metering as _metering
    import payments.mpp as _mpp

    for name in MPP_CONTROL_PLANE_CALLS:
        assert hasattr(mpp_client_module, name), (
            f"payments.mpp_client.{name} is missing; the MppSpy cannot attach."
        )

    # No calling module may hold its own module-level binding of these names.
    for module in (_metering, _mpp):
        shadowed = [name for name in MPP_CONTROL_PLANE_CALLS if hasattr(module, name)]
        assert not shadowed, (
            f"{module.__name__} now binds {shadowed} at module level. "
            "Patching payments.mpp_client would no longer be observed and the "
            "MppSpy would silently record zero calls."
        )

    # And the names must still be referenced by the code paths under test, so a
    # rename or removal is caught rather than producing a vacuously green spy.
    import inspect

    authorize_source = inspect.getsource(_mpp.enforce_mpp_payment)
    assert "authorize_mpp_payment" in authorize_source, (
        "enforce_mpp_payment no longer references authorize_mpp_payment."
    )

    finaliser_source = inspect.getsource(_metering.MeteringMiddleware.dispatch)
    for name in ("capture_mpp_payment", "void_mpp_authorization"):
        assert name in finaliser_source, (
            f"MeteringMiddleware.dispatch no longer references {name}; the MPP "
            "capture/void finaliser may have moved and the spy is stale."
        )


# ---------------------------------------------------------------------------
# Engine stubs
# ---------------------------------------------------------------------------

def rows_engine(rows: list[dict[str, Any]]) -> MagicMock:
    """An engine whose every query returns the same row set."""
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    result.mappings.return_value.first.return_value = rows[0] if rows else None

    conn = MagicMock()
    conn.execute.return_value = result

    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    engine.connect.return_value.__exit__.return_value = False
    engine.begin.return_value.__enter__.return_value = conn
    engine.begin.return_value.__exit__.return_value = False
    return engine


def sequence_engine(row_sets: list[list[dict[str, Any]]]) -> MagicMock:
    """An engine returning each row set in turn, one per query."""

    def _result(rows: list[dict[str, Any]]) -> MagicMock:
        r = MagicMock()
        r.mappings.return_value.all.return_value = rows
        r.mappings.return_value.first.return_value = rows[0] if rows else None
        return r

    conn = MagicMock()
    conn.execute.side_effect = [_result(rows) for rows in row_sets]

    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    engine.connect.return_value.__exit__.return_value = False
    return engine


def counting_engine(rows: list[dict[str, Any]]) -> tuple[MagicMock, list[int]]:
    """
    An engine that records how many queries it received.

    Used to prove the endpoint body actually executed (or did not) rather than
    inferring it from the status code.
    """
    calls: list[int] = []
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    result.mappings.return_value.first.return_value = rows[0] if rows else None

    conn = MagicMock()

    def _execute(*_args, **_kwargs):
        calls.append(1)
        return result

    conn.execute.side_effect = _execute

    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    engine.connect.return_value.__exit__.return_value = False
    return engine, calls


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def facilitator_spy(monkeypatch) -> FacilitatorSpy:
    """Replace the facilitator calls with recording fakes."""
    assert_facilitator_bindings_intact()
    spy = FacilitatorSpy()

    def fake_verify(*, payment_signature, payment_requirements):
        spy.verify_calls.append(
            {
                "payment_signature": payment_signature,
                "payment_requirements": payment_requirements,
            }
        )
        if not spy.verify_valid:
            return X402ValidationResult(
                valid=False,
                error_code="payment_verification_failed",
                error_detail="Facilitator reported invalid payment payload.",
            )
        return X402ValidationResult(valid=True, payment_signature=payment_signature)

    def fake_settle(*, payment_signature, payment_requirements):
        spy.settle_calls.append(
            {
                "payment_signature": payment_signature,
                "payment_requirements": payment_requirements,
            }
        )
        if not spy.settle_valid:
            return X402ValidationResult(
                valid=False,
                error_code="payment_settlement_failed",
                error_detail="Facilitator did not confirm settlement.",
            )
        return X402ValidationResult(
            valid=True,
            payment_signature=payment_signature,
            settlement_response={"success": True, "transaction": SETTLEMENT_TX},
        )

    monkeypatch.setattr(enforcement_module, "verify_with_facilitator", fake_verify)
    monkeypatch.setattr(enforcement_module, "settle_with_facilitator", fake_settle)
    return spy


@pytest.fixture
def mpp_spy(monkeypatch) -> MppSpy:
    """Replace the MPP control-plane calls with recording fakes."""
    assert_mpp_bindings_intact()
    spy = MppSpy()

    def fake_authorize(**kwargs):
        spy.authorize_calls.append(dict(kwargs))
        if not spy.authorize_success:
            return MppControlPlaneResult(
                success=False,
                error_code="insufficient_session_balance",
                error_detail="Session balance too low.",
            )
        return MppControlPlaneResult(success=True, response_data={"authorized": True})

    def fake_capture(**kwargs):
        spy.capture_calls.append(dict(kwargs))
        return MppControlPlaneResult(success=spy.capture_success)

    def fake_void(**kwargs):
        spy.void_calls.append(dict(kwargs))
        return MppControlPlaneResult(success=spy.void_success)

    monkeypatch.setattr(mpp_client_module, "authorize_mpp_payment", fake_authorize)
    monkeypatch.setattr(mpp_client_module, "capture_mpp_payment", fake_capture)
    monkeypatch.setattr(mpp_client_module, "void_mpp_authorization", fake_void)
    return spy


@pytest.fixture
def log_spy(monkeypatch) -> LogSpy:
    """Capture request-event and economics rows instead of writing them."""
    spy = LogSpy()
    monkeypatch.setattr(
        metering_module,
        "log_api_request_event",
        lambda event: spy.events.append(dict(event)),
    )
    monkeypatch.setattr(
        metering_module,
        "log_api_request_economics",
        lambda econ: spy.economics.append(dict(econ)),
    )
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda **_kw: None)
    return spy


@pytest.fixture
def agent_pay_enabled(monkeypatch) -> None:
    """
    Turn on the agent-pay lane the way production runs it.

    These are module-level constants read from the environment at import, so
    the flags are patched on the modules rather than in os.environ.
    """
    monkeypatch.setattr(api_key_module, "_ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "ENFORCE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "VALIDATE_AGENT_PAY_HEADERS", True)
    monkeypatch.setattr(classifier_module, "ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(classifier_module, "ENFORCE_AGENT_PAY", True)


@pytest.fixture
def priced_at_unit_price(monkeypatch) -> None:
    """
    Resolve every pricing rule to the acceptance-suite sentinels.

    The three values are distinct on purpose — see the sentinel block at the top
    of this module.  Tests assert each economics field against its own sentinel,
    so a mapping swap between unit_price_usd, billed_amount_usd and stc_cost
    fails rather than passing silently.
    """
    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda *_a, **_kw: (
            SENTINEL_UNIT_PRICE_USD,
            SENTINEL_BILLED_AMOUNT_USD,
            SENTINEL_STC_COST,
        ),
    )


@pytest.fixture
def payment_harness(
    monkeypatch,
    agent_pay_enabled,
    priced_at_unit_price,
    facilitator_spy,
    mpp_spy,
    log_spy,
) -> PaymentHarness:
    """
    A TestClient over the real application, with every economic side effect
    replaced by a recording spy.

    The real `main.app` is used deliberately: middleware order, the `/v1` mount,
    routing, and the sub-application's exception handling are all part of what
    the invariant depends on, so none of them may be stubbed out.
    """
    used_references: set[str] = set()
    monkeypatch.setattr(
        metering_module,
        "is_payment_reference_used",
        lambda reference: bool(reference) and reference in used_references,
    )

    with TestClient(main.app) as client:
        yield PaymentHarness(
            client=client,
            facilitator=facilitator_spy,
            mpp=mpp_spy,
            logs=log_spy,
            used_payment_references=used_references,
        )


# ---------------------------------------------------------------------------
# Route-governance helpers (shared with the structural guards)
# ---------------------------------------------------------------------------

def v1_api_routes() -> list[Any]:
    """Every FastAPI APIRoute mounted under the v1 application."""
    from fastapi.routing import APIRoute

    return [route for route in main.v1.routes if isinstance(route, APIRoute)]


def v1_path(route: Any) -> str:
    """The externally addressable path of a route on the mounted v1 app."""
    return f"/v1{route.path}"


def payment_governed_routes() -> list[tuple[Any, str]]:
    """
    Routes the payment policy currently governs, as (route, method) pairs.

    Governance is read from the policy provider rather than from a list in the
    test, so a new paid endpoint is enrolled by the same act that makes it paid.
    """
    from payments.policy_provider import is_agent_pay_enforcement_path

    governed: list[tuple[Any, str]] = []
    for route in v1_api_routes():
        for method in sorted(route.methods or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            if is_agent_pay_enforcement_path(v1_path(route), method=method):
                governed.append((route, method))
    return governed
