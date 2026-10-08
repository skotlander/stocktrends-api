"""Focused regressions for x402 secret handling and replay availability."""

from __future__ import annotations

import json
import logging

import pytest

import middleware.metering as metering
import payments.x402 as x402
from payments.enforcement import ReplayCheckUnavailable
from support.payment_harness import x402_headers


_PROOF_SECRET = "proof-secret-do-not-log"
_FACILITATOR_SECRET = "facilitator-response-secret-do-not-log"
_REQUIREMENTS = {"scheme": "exact", "network": "base", "amount": "150000"}


def _payment_proof() -> str:
    return json.dumps(
        {
            "x402Version": 2,
            "scheme": "exact",
            "payload": {"signature": _PROOF_SECRET},
        }
    )


def test_verify_logs_and_errors_exclude_payment_and_facilitator_secrets(monkeypatch, caplog):
    monkeypatch.setattr(x402, "X402_FACILITATOR_API_KEY", "facilitator-key-id-secret")
    monkeypatch.setattr(
        x402,
        "_post_json",
        lambda *_args, **_kwargs: (
            502,
            {"error": _FACILITATOR_SECRET},
            _FACILITATOR_SECRET,
        ),
    )

    with caplog.at_level(logging.INFO, logger="stocktrends_api.x402"):
        result = x402.verify_with_facilitator(
            payment_signature=_payment_proof(), payment_requirements=_REQUIREMENTS
        )

    assert result.valid is False
    assert result.error_detail == "Facilitator /verify returned HTTP 502."
    assert result.verification_response is None
    assert "operation=verify status=502 outcome=http_error" in caplog.text
    for secret in (_PROOF_SECRET, _FACILITATOR_SECRET, "facilitator-key-id-secret"):
        assert secret not in caplog.text
        assert secret not in result.error_detail


def test_settlement_logs_and_response_exclude_payment_and_facilitator_secrets(monkeypatch, caplog):
    monkeypatch.setattr(x402, "X402_FACILITATOR_API_KEY", "facilitator-key-id-secret")
    monkeypatch.setattr(
        x402,
        "_post_json",
        lambda *_args, **_kwargs: (
            200,
            {"success": True, "transaction": "0xreceipt", "debug": _FACILITATOR_SECRET},
            _FACILITATOR_SECRET,
        ),
    )

    with caplog.at_level(logging.INFO, logger="stocktrends_api.x402"):
        result = x402.settle_with_facilitator(
            payment_signature=_payment_proof(), payment_requirements=_REQUIREMENTS
        )

    assert result.valid is True
    assert result.settlement_response == {"success": True, "transaction": "0xreceipt"}
    assert "operation=settle status=200 outcome=received" in caplog.text
    for secret in (_PROOF_SECRET, _FACILITATOR_SECRET, "facilitator-key-id-secret"):
        assert secret not in caplog.text
        assert secret not in json.dumps(result.settlement_response)


def test_replay_database_failure_is_classified_without_leaking_exception(monkeypatch, caplog):
    database_error = RuntimeError("postgres://user:password@db/payment-proof-secret")
    monkeypatch.setattr(metering, "get_metering_engine", lambda: (_ for _ in ()).throw(database_error))

    with caplog.at_level(logging.ERROR, logger="stocktrends_api.metering"):
        with pytest.raises(ReplayCheckUnavailable):
            metering.is_payment_reference_used("x402-reference")

    assert "Payment replay check unavailable" in caplog.text
    assert "postgres://" not in caplog.text
    assert "payment-proof-secret" not in caplog.text


def test_replay_database_failure_blocks_verify_and_settlement(payment_harness, monkeypatch):
    def unavailable(_reference):
        raise ReplayCheckUnavailable()

    monkeypatch.setattr(metering, "is_payment_reference_used", unavailable)

    response = payment_harness.client.get(
        "/v1/prices/history?symbol_exchange=IBM-N", headers=x402_headers()
    )

    assert response.status_code == 402
    assert response.json()["error"] == "replay_check_unavailable"
    assert response.json()["detail"] == (
        "Payment replay protection is temporarily unavailable. Please retry later."
    )
    assert payment_harness.verify_count == 0
    assert payment_harness.settle_count == 0
