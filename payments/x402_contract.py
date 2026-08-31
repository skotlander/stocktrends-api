"""Side-effect-free x402 machine-contract configuration.

Operational facilitator code deliberately does not live here. Runtime payment
handling, OpenAPI, and discovery all consume these values so the published
proof-header and protocol contract cannot drift from request recognition.
"""

from __future__ import annotations

import hashlib
import json
import os


X402_VERSION = 2
X402_PROOF_HEADERS = ("PAYMENT-SIGNATURE", "X-Payment")

X402_DEFAULT_NETWORK = os.getenv("X402_DEFAULT_NETWORK", "eip155:8453")
X402_DEFAULT_SCHEME = os.getenv("X402_DEFAULT_SCHEME", "exact")
X402_DEFAULT_TOKEN = os.getenv(
    "X402_DEFAULT_TOKEN",
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
)
X402_DEFAULT_TOKEN_NAME = os.getenv("X402_DEFAULT_TOKEN_NAME", "USDC")
X402_DEFAULT_TOKEN_VERSION = os.getenv("X402_DEFAULT_TOKEN_VERSION", "2")
X402_DEFAULT_ASSET_TRANSFER_METHOD = os.getenv(
    "X402_DEFAULT_ASSET_TRANSFER_METHOD",
    "eip3009",
)
X402_DEFAULT_TOKEN_DECIMALS = int(os.getenv("X402_DEFAULT_TOKEN_DECIMALS", "6"))
X402_SELLER_ADDRESS = os.getenv("X402_SELLER_ADDRESS", "")


def x402_contract_fingerprint() -> str:
    """Return a semantic fingerprint of the published x402 contract."""
    payload = {
        "version": X402_VERSION,
        "proof_headers": list(X402_PROOF_HEADERS),
        "scheme": X402_DEFAULT_SCHEME,
        "network": X402_DEFAULT_NETWORK,
        "token": X402_DEFAULT_TOKEN,
        "token_name": X402_DEFAULT_TOKEN_NAME,
        "token_version": X402_DEFAULT_TOKEN_VERSION,
        "asset_transfer_method": X402_DEFAULT_ASSET_TRANSFER_METHOD,
        "token_decimals": X402_DEFAULT_TOKEN_DECIMALS,
        "seller_address": X402_SELLER_ADDRESS,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
