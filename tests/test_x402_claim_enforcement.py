"""Deterministic claim-enforcement regressions; no facilitator or DB is contacted."""

from __future__ import annotations

import json
from decimal import Decimal

import payments.enforcement as enforcement
from payments.x402 import X402ValidationResult
from payments.x402_claims import ClaimAcquireResult, ClaimRepositoryUnavailable


ASSET = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SELLER = "0x1111111111111111111111111111111111111111"
PAYER = "0x2222222222222222222222222222222222222222"
REQUIREMENT = {"scheme": "exact", "network": "eip155:8453", "amount": "150000", "asset": ASSET,
               "payTo": SELLER, "maxTimeoutSeconds": 300,
               "extra": {"assetTransferMethod": "eip3009"}}


def headers():
    return {"X-Payment": json.dumps({"x402Version": 2, "accepted": REQUIREMENT,
        "payload": {"signature": "0x" + "aa" * 65, "authorization": {
        "from": PAYER, "to": SELLER, "value": "150000", "validAfter": "0",
        "validBefore": "9999999999", "nonce": "0x" + "01" * 32}}})}


def call(monkeypatch, *, replay_checker=lambda _value: False, suspended=False):
    monkeypatch.setenv("X402_ATOMIC_CLAIMS_ENABLED", "true")
    if suspended:
        monkeypatch.setenv("X402_SETTLEMENT_SUSPENDED", "true")
    else:
        monkeypatch.delenv("X402_SETTLEMENT_SUSPENDED", raising=False)
    monkeypatch.setattr(enforcement, "build_x402_requirements", lambda **_kwargs: {"accepts": [REQUIREMENT]})
    return enforcement.enforce_x402_payment(
        headers=headers(), path="/v1/stim/latest", method="GET", amount_usd=Decimal("0.15"),
        validation_valid=True, validation_error=None, validation_detail=None,
        validated_payment_reference="legacy-reference", validated_payment_network="eip155:8453",
        validated_payment_token=ASSET, validated_payment_amount_native=Decimal("150000"),
        replay_checker=replay_checker, request_id="request-1",
    )


def _verify_ok(**kwargs):
    return X402ValidationResult(valid=True, payment_signature=kwargs["payment_signature"])


def _settle_ok(**kwargs):
    return X402ValidationResult(valid=True, payment_signature=kwargs["payment_signature"],
                                settlement_response={"success": True, "transaction": "0xtx"})


def test_claim_acquisition_runs_one_settlement_and_records_terminal_state(monkeypatch):
    calls = []
    monkeypatch.setattr(enforcement, "verify_with_facilitator", _verify_ok)
    monkeypatch.setattr(enforcement, "settle_with_facilitator", lambda **kwargs: calls.append(kwargs) or _settle_ok(**kwargs))
    monkeypatch.setattr(enforcement, "acquire_settling_claim", lambda **_kwargs: ClaimAcquireResult(True, "settling"))
    recorded = []
    monkeypatch.setattr(enforcement, "record_settled", lambda **kwargs: recorded.append(kwargs) or True)
    assert call(monkeypatch).outcome == "proceed"
    assert len(calls) == 1 and len(recorded) == 1


def test_existing_or_unavailable_claim_blocks_settlement(monkeypatch):
    monkeypatch.setattr(enforcement, "verify_with_facilitator", _verify_ok)
    settle_calls = []
    monkeypatch.setattr(enforcement, "settle_with_facilitator", lambda **kwargs: settle_calls.append(kwargs) or _settle_ok(**kwargs))
    monkeypatch.setattr(enforcement, "acquire_settling_claim", lambda **_kwargs: ClaimAcquireResult(False, "settling"))
    assert call(monkeypatch).outcome == "claim_exists"
    monkeypatch.setattr(enforcement, "acquire_settling_claim", lambda **_kwargs: (_ for _ in ()).throw(ClaimRepositoryUnavailable()))
    assert call(monkeypatch).outcome == "claim_unavailable"
    assert not settle_calls


def test_timeout_and_final_record_failure_are_uncertain_and_block_execution(monkeypatch):
    monkeypatch.setattr(enforcement, "verify_with_facilitator", _verify_ok)
    monkeypatch.setattr(enforcement, "acquire_settling_claim", lambda **_kwargs: ClaimAcquireResult(True, "settling"))
    monkeypatch.setattr(enforcement, "settle_with_facilitator", lambda **_kwargs: X402ValidationResult(valid=False, error_code="facilitator_settle_unreachable"))
    uncertain = []
    monkeypatch.setattr(enforcement, "record_uncertain", lambda **kwargs: uncertain.append(kwargs) or True)
    assert call(monkeypatch).outcome == "settlement_uncertain"
    assert uncertain
    monkeypatch.setattr(enforcement, "settle_with_facilitator", _settle_ok)
    monkeypatch.setattr(enforcement, "record_settled", lambda **_kwargs: False)
    assert call(monkeypatch).outcome == "settlement_uncertain"


def test_suspension_never_calls_settle(monkeypatch):
    monkeypatch.setenv("X402_SETTLEMENT_SUSPENDED", "true")
    monkeypatch.setattr(enforcement, "verify_with_facilitator", _verify_ok)
    monkeypatch.setattr(enforcement, "settle_with_facilitator", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("settle")))
    assert call(monkeypatch, suspended=True).outcome == "settlement_suspended"


def test_safe_reference_is_checked_after_legacy_reference(monkeypatch):
    monkeypatch.setenv("X402_ATOMIC_CLAIMS_ENABLED", "false")
    monkeypatch.delenv("X402_SETTLEMENT_SUSPENDED", raising=False)
    monkeypatch.setattr(enforcement, "build_x402_requirements", lambda **_kwargs: {"accepts": [REQUIREMENT]})
    monkeypatch.setattr(enforcement, "verify_with_facilitator", _verify_ok)
    monkeypatch.setattr(enforcement, "settle_with_facilitator", _settle_ok)
    checked = []
    kwargs = dict(headers=headers(), path="/v1/stim/latest", method="GET", amount_usd=Decimal("0.15"),
                  validation_valid=True, validation_error=None, validation_detail=None,
                  validated_payment_reference="historic-id", validated_payment_network="eip155:8453",
                  validated_payment_token=ASSET, validated_payment_amount_native=Decimal("150000"), request_id="request-1")
    first = enforcement.enforce_x402_payment(replay_checker=lambda value: checked.append(value) or False, **kwargs)
    assert first.outcome == "proceed"
    # Simulate economics persistence of the safe reference, then repeat the
    # same authorization while claims are disabled.
    second = enforcement.enforce_x402_payment(replay_checker=lambda value: value == first.payment_reference, **kwargs)
    assert second.outcome == "replay_detected"
    assert first.payment_reference.startswith("x402:artifact-v1:")
    assert checked[0] == "historic-id" and checked[1].startswith("x402:artifact-v1:")
