from __future__ import annotations

import base64
import json

import pytest

from payments.x402 import build_x402_payment_identity


ASSET = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SELLER = "0x1111111111111111111111111111111111111111"
PAYER = "0x2222222222222222222222222222222222222222"


def requirements():
    return {"scheme": "exact", "network": "eip155:8453", "asset": ASSET,
            "payTo": SELLER, "amount": "150000", "maxTimeoutSeconds": 300,
            "extra": {"assetTransferMethod": "eip3009"}}


def proof(**changes):
    authorization = {"from": PAYER, "to": SELLER, "value": "150000", "validAfter": "0",
                     "validBefore": "9999999999", "nonce": "0x" + "01" * 32}
    authorization.update(changes.pop("authorization", {}))
    result = {"x402Version": 2,
              "resource": {"url": "https://api.example.com/premium-data", "mimeType": "application/json"},
              "accepted": {"scheme": "exact", "network": "eip155:8453", "amount": "150000",
                           "asset": ASSET, "payTo": SELLER, "maxTimeoutSeconds": 300,
                           "extra": {"name": "USDC", "version": "2", "assetTransferMethod": "eip3009"}},
              "payload": {"authorization": authorization, "signature": "0x" + "aa" * 65}}
    result.update(changes)
    return result


def identity(payload):
    return build_x402_payment_identity(json.dumps(payload), requirements())


def test_identity_is_equivalent_for_json_base64_whitespace_and_key_order():
    payload = proof()
    raw = json.dumps(payload, indent=2)
    b64 = base64.b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).decode()
    assert build_x402_payment_identity(raw, requirements()).fingerprint == build_x402_payment_identity(b64, requirements()).fingerprint


def test_identity_accepts_published_v2_paymentpayload_shape():
    """Fixture adapted from x402 V2 specification §5.2.1."""
    accepted = {"scheme": "exact", "network": "eip155:84532", "amount": "10000",
                "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                "payTo": "0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
                "maxTimeoutSeconds": 60, "extra": {"name": "USDC", "version": "2", "assetTransferMethod": "eip3009"}}
    payload = {"x402Version": 2, "resource": {"url": "https://api.example.com/premium-data"},
               "accepted": accepted, "extensions": {}, "payload": {"signature": "0x" + "2d" * 65,
               "authorization": {"from": "0x857b06519E91e3A54538791bDbb0E22373e36b66",
               "to": accepted["payTo"], "value": "10000", "validAfter": "1740672089",
               "validBefore": "1740672154", "nonce": "0xf3746613c2d920b5fdabc0856f2aeb2d4f88ee6037b8cc5d04a71a4462f13480"}}}
    assert build_x402_payment_identity(json.dumps(payload), accepted).accounting_reference.startswith("x402:v1:")


def test_identity_normalizes_addresses_and_numbers_and_ignores_payment_identifier():
    first = proof()
    second = proof()
    second["accepted"]["asset"] = ASSET.upper()
    second["payload"]["authorization"]["from"] = PAYER.upper()
    second["payload"]["authorization"]["value"] = 150000
    assert identity(first).fingerprint == identity(second).fingerprint


def test_identity_accepts_exact_evm_default_when_quote_omits_transfer_method():
    """@x402/evm Exact EVM defaults this optional V2 hint to EIP-3009."""
    payload = proof()
    payload["accepted"]["extra"].pop("assetTransferMethod")
    live_requirement = requirements()
    live_requirement["extra"].pop("assetTransferMethod")

    result = build_x402_payment_identity(json.dumps(payload), live_requirement)

    assert result.accounting_reference.startswith("x402:v1:")


@pytest.mark.parametrize("accepted_method, required_method", [
    ("permit2", "permit2"),
    ("eip3009", "permit2"),
    (None, "eip3009"),
])
def test_identity_rejects_explicit_or_asymmetric_transfer_methods(accepted_method, required_method):
    payload = proof()
    if accepted_method is None:
        payload["accepted"]["extra"].pop("assetTransferMethod")
    else:
        payload["accepted"]["extra"]["assetTransferMethod"] = accepted_method
    live_requirement = requirements()
    live_requirement["extra"]["assetTransferMethod"] = required_method

    with pytest.raises(ValueError, match="EIP-3009"):
        build_x402_payment_identity(json.dumps(payload), live_requirement)


@pytest.mark.parametrize("change", [
    {"authorization": {"nonce": "0x" + "02" * 32}},
    {"authorization": {"from": "0x3333333333333333333333333333333333333333"}},
    {"accepted": {"asset": "0x4444444444444444444444444444444444444444"}},
    {"accepted": {"network": "eip155:1"}},
    {"authorization": {"value": "150001"}},
    {"authorization": {"validBefore": "9999999998"}},
])
def test_identity_changes_for_signed_authorization_fields(change):
    changed = proof(**change)
    # Asset/network/value conflicts are rejected against the server quote;
    # sender, nonce and validity changes remain valid but have new identities.
    if "accepted" in change or "value" in change.get("authorization", {}):
        with pytest.raises(ValueError):
            identity(changed)
    else:
        assert identity(proof()).fingerprint != identity(changed).fingerprint


@pytest.mark.parametrize("mutate", [
    lambda p: p["accepted"].update(scheme="upto"),
    lambda p: p["payload"].update(signature="0x12"),
    lambda p: p["payload"].pop("authorization"),
    lambda p: p["payload"]["authorization"].update(nonce="x"),
])
def test_identity_rejects_unsupported_or_malformed_proofs(mutate):
    payload = proof()
    mutate(payload)
    with pytest.raises(ValueError):
        identity(payload)
