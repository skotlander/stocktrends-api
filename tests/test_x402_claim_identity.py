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
            "payTo": SELLER, "amount": "150000", "extra": {"assetTransferMethod": "eip3009"}}


def proof(**changes):
    authorization = {"from": PAYER, "to": SELLER, "value": "150000", "validAfter": "0",
                     "validBefore": "9999999999", "nonce": "0x" + "01" * 32}
    authorization.update(changes.pop("authorization", {}))
    result = {"x402Version": 2, "scheme": "exact", "network": "eip155:8453", "asset": ASSET,
              "paymentIdentifier": "caller-controlled-and-ignored",
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


def test_identity_normalizes_addresses_and_numbers_and_ignores_payment_identifier():
    first = proof()
    second = proof(paymentIdentifier="other")
    second["asset"] = ASSET.upper()
    second["payload"]["authorization"]["from"] = PAYER.upper()
    second["payload"]["authorization"]["value"] = 150000
    assert identity(first).fingerprint == identity(second).fingerprint


@pytest.mark.parametrize("change", [
    {"authorization": {"nonce": "0x" + "02" * 32}},
    {"authorization": {"from": "0x3333333333333333333333333333333333333333"}},
    {"asset": "0x4444444444444444444444444444444444444444"},
    {"network": "eip155:1"},
    {"authorization": {"value": "150001"}},
    {"authorization": {"validBefore": "9999999998"}},
])
def test_identity_changes_for_signed_authorization_fields(change):
    changed = proof(**change)
    # Asset/network/value conflicts are rejected against the server quote;
    # sender, nonce and validity changes remain valid but have new identities.
    if "asset" in change or "network" in change or "value" in change.get("authorization", {}):
        with pytest.raises(ValueError):
            identity(changed)
    else:
        assert identity(proof()).fingerprint != identity(changed).fingerprint


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(scheme="upto"),
    lambda p: p["payload"].update(signature="0x12"),
    lambda p: p["payload"].pop("authorization"),
    lambda p: p["payload"]["authorization"].update(nonce="x"),
])
def test_identity_rejects_unsupported_or_malformed_proofs(mutate):
    payload = proof()
    mutate(payload)
    with pytest.raises(ValueError):
        identity(payload)
