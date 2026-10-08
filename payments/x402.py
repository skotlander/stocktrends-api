import base64
import binascii
import hashlib
import json
import os
import time
import uuid
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from discovery.endpoint_metadata import (
    SERVICE_ICON_URL,
    SERVICE_NAME,
    build_bazaar_extension,
    build_compact_bazaar_extension,
    get_resource_description,
    get_x402_resource_tags,
)
import payments.x402_contract as x402_contract
from payments.x402_contract import (
    X402_DEFAULT_ASSET_TRANSFER_METHOD,
    X402_DEFAULT_NETWORK,
    X402_DEFAULT_SCHEME,
    X402_DEFAULT_TOKEN,
    X402_DEFAULT_TOKEN_DECIMALS,
    X402_DEFAULT_TOKEN_NAME,
    X402_DEFAULT_TOKEN_VERSION,
    X402_SELLER_ADDRESS,
)


logger = logging.getLogger("stocktrends_api.x402")


# =========================================================
# CONFIG
# =========================================================

X402_FACILITATOR_URL = os.getenv(
    "X402_FACILITATOR_URL",
    "https://api.cdp.coinbase.com/platform/v2/x402",
).rstrip("/")

X402_FACILITATOR_API_KEY = os.getenv("X402_FACILITATOR_API_KEY")
X402_FACILITATOR_API_SECRET = os.getenv("X402_FACILITATOR_API_SECRET")

X402_TIMEOUT_SECONDS = float(os.getenv("X402_TIMEOUT_SECONDS", "10"))
X402_API_BASE_URL = os.getenv("X402_API_BASE_URL", "").rstrip("/")
X402_CHALLENGE_MODE_HEADER = "X-StockTrends-Challenge-Mode"
X402_PAYMENT_REQUIRED_HEADER_MODE_ENV = "X402_PAYMENT_REQUIRED_HEADER_MODE"
X402_CHALLENGE_MODE_FULL = "full"
X402_CHALLENGE_MODE_COMPACT = "compact"
X402_IDENTITY_VERSION = 1


# =========================================================
# DATA STRUCTURES
# =========================================================

@dataclass
class X402ValidationResult:
    valid: bool
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    payment_reference: Optional[str] = None
    payment_network: Optional[str] = None
    payment_token: Optional[str] = None
    payment_amount_native: Optional[Decimal] = None
    payment_signature: Optional[str] = None
    payment_payload: Optional[dict[str, Any]] = None
    verification_response: Optional[dict[str, Any]] = None
    settlement_response: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class X402PaymentIdentity:
    """The safe, versioned identity for one EIP-3009 authorization."""
    version: int
    fingerprint: bytes
    accounting_reference: str


# =========================================================
# BASIC HELPERS
# =========================================================

def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _to_atomic_units(amount: Decimal, decimals: int) -> str:
    quantized = (amount * (Decimal(10) ** decimals)).quantize(Decimal("1"))
    return str(int(quantized))


def _normalize_private_key(secret: str) -> str:
    secret = secret.strip()
    if "\\n" in secret:
        secret = secret.replace("\\n", "\n")
    return secret


def _json_dumps_compact(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _b64_json(data: dict[str, Any]) -> str:
    raw = _json_dumps_compact(data).encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


def _decode_b64_json(value: str) -> dict[str, Any]:
    decoded = base64.b64decode(value)
    parsed = json.loads(decoded.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Decoded base64 JSON is not an object.")
    return parsed


def _explicit_challenge_mode(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()
    if normalized == X402_CHALLENGE_MODE_COMPACT:
        return X402_CHALLENGE_MODE_COMPACT
    if normalized in {X402_CHALLENGE_MODE_FULL, "rich"}:
        return X402_CHALLENGE_MODE_FULL
    return None


def normalize_challenge_mode(value: str | None) -> str:
    return _explicit_challenge_mode(value) or X402_CHALLENGE_MODE_FULL


def normalize_payment_required_challenge_mode(value: str | None = None) -> str:
    """Resolve the compact-by-default challenge mode used in 402 responses."""
    return (
        _explicit_challenge_mode(value)
        or _explicit_challenge_mode(os.getenv(X402_PAYMENT_REQUIRED_HEADER_MODE_ENV))
        or X402_CHALLENGE_MODE_COMPACT
    )


# =========================================================
# CDP FACILITATOR AUTH
# =========================================================

def _load_cdp_signing_key(secret: str) -> tuple[Any, str]:
    """
    Supports both CDP Secret API Key formats:

    1. ECDSA / ES256 PEM private key
    2. Ed25519 / EdDSA base64-encoded secret

    CDP Ed25519 secrets may decode to:
    - 32 bytes: raw private key seed
    - 64 bytes: 32-byte seed + 32-byte public key

    Returns:
        (private_key_object, jwt_algorithm)
    """
    normalized = _normalize_private_key(secret)

    # ECDSA PEM path
    if "BEGIN" in normalized:
        private_key = load_pem_private_key(normalized.encode("utf-8"), password=None)
        return private_key, "ES256"

    # Ed25519 path
    try:
        raw = base64.b64decode(normalized)
    except Exception as e:
        raise ValueError(f"Unable to base64-decode CDP API secret as Ed25519 key: {e}") from e

    if len(raw) == 32:
        seed = raw
        return Ed25519PrivateKey.from_private_bytes(seed), "EdDSA"

    if len(raw) == 64:
        seed = raw[:32]
        return Ed25519PrivateKey.from_private_bytes(seed), "EdDSA"

    raise ValueError(
        f"Unsupported Ed25519 secret length: {len(raw)} bytes. "
        "Expected 32 bytes (seed), 64 bytes (seed + public key), or PEM for ES256."
    )


def _build_cdp_bearer_token(method: str, url: str) -> str | None:
    if not X402_FACILITATOR_API_KEY or not X402_FACILITATOR_API_SECRET:
        return None

    now = int(time.time())
    parsed = urlparse(url)
    request_host = parsed.netloc
    request_path = parsed.path
    uri = f"{method.upper()} {request_host}{request_path}"

    private_key, algorithm = _load_cdp_signing_key(X402_FACILITATOR_API_SECRET)

    payload = {
        "sub": X402_FACILITATOR_API_KEY,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": uri,
    }

    headers = {
        "kid": X402_FACILITATOR_API_KEY,
        "nonce": uuid.uuid4().hex,
    }

    return jwt.encode(
        payload,
        private_key,
        algorithm=algorithm,
        headers=headers,
    )


def _facilitator_headers(method: str, url: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    bearer = _build_cdp_bearer_token(method, url)
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _post_json(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str | None]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=data,
        headers=_facilitator_headers("POST", url),
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=X402_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
            try:
                parsed = json.loads(body) if body else {}
            except ValueError:
                parsed = None
            return resp.status, parsed, body
    except urllib_error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = str(e)
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = None
        return e.code, parsed, body
    except Exception as e:
        return 0, None, str(e)


_MAX_SETTLEMENT_RECEIPT_FIELD_LENGTH = 512
_MAX_SETTLEMENT_AMOUNT_LENGTH = 128


def _safe_settlement_receipt_string(value: Any, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        return None
    return normalized


def _safe_facilitator_receipt(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return bounded, standard x402 settlement receipt metadata only."""
    if not isinstance(data, dict):
        return None

    receipt: dict[str, Any] = {}
    if isinstance(data.get("success"), bool):
        receipt["success"] = data["success"]

    for field in ("transaction", "network", "payer", "errorReason"):
        value = _safe_settlement_receipt_string(
            data.get(field), max_length=_MAX_SETTLEMENT_RECEIPT_FIELD_LENGTH
        )
        if value is not None:
            receipt[field] = value

    amount = _safe_settlement_receipt_string(
        data.get("amount"), max_length=_MAX_SETTLEMENT_AMOUNT_LENGTH
    )
    if amount is not None:
        receipt["amount"] = amount

    return receipt or None


# =========================================================
# REQUIREMENTS / CHALLENGE
# =========================================================

def build_x402_requirements(
    *,
    path: str,
    amount_usd: Decimal,
    method: str = "GET",
    network: str = X402_DEFAULT_NETWORK,
    token: str = X402_DEFAULT_TOKEN,
    scheme: str = X402_DEFAULT_SCHEME,
    pay_to: str = X402_SELLER_ADDRESS,
    max_timeout_seconds: int = 300,
    description: str = "",
    mime_type: str = "application/json",
    challenge_mode: str = X402_CHALLENGE_MODE_FULL,
) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "name": X402_DEFAULT_TOKEN_NAME,
        "version": X402_DEFAULT_TOKEN_VERSION,
    }
    if X402_DEFAULT_ASSET_TRANSFER_METHOD:
        extra["assetTransferMethod"] = X402_DEFAULT_ASSET_TRANSFER_METHOD

    http_method = method.upper()
    resource_url = f"{X402_API_BASE_URL}{path}" if X402_API_BASE_URL else path
    resource_description = description or get_resource_description(path)
    resource_info = {
        "url": resource_url,
        "description": resource_description,
        "mimeType": mime_type,
        "serviceName": SERVICE_NAME,
        # Endpoint-aware, and read from the canonical endpoint registry. Payment
        # code must not carry a second path->tags table of its own.
        "tags": get_x402_resource_tags(path, http_method),
        "iconUrl": SERVICE_ICON_URL,
    }
    extra["resource"] = dict(resource_info)
    normalized_challenge_mode = normalize_challenge_mode(challenge_mode)
    if normalized_challenge_mode == X402_CHALLENGE_MODE_COMPACT:
        bazaar_extension = build_compact_bazaar_extension(path, http_method)
    else:
        bazaar_extension = build_bazaar_extension(path, http_method)

    # Registry-backed Bazaar metadata keeps v2 discovery construction in one place.
    return {
        "x402Version": x402_contract.X402_VERSION,
        # V2 canonical resource identity (ResourceInfo) — separate from accepts entries.
        "resource": resource_info,
        "accepts": [
            {
                "scheme": scheme,
                "network": network,
                # x402 V2 PaymentRequirements canonical field is "amount".
                "amount": _to_atomic_units(amount_usd, X402_DEFAULT_TOKEN_DECIMALS),
                "asset": token,
                "payTo": pay_to,
                "maxTimeoutSeconds": max_timeout_seconds,
                "extra": extra,
            }
        ],
        "extensions": bazaar_extension,
    }


def _extract_single_requirement(payment_requirements: Any) -> dict[str, Any]:
    if isinstance(payment_requirements, dict):
        obj = payment_requirements
    elif isinstance(payment_requirements, str):
        try:
            obj = json.loads(payment_requirements)
        except Exception:
            obj = _decode_b64_json(payment_requirements)
    else:
        raise ValueError("payment_requirements must be dict or string.")

    if not isinstance(obj, dict):
        raise ValueError("payment_requirements must resolve to an object.")

    accepts = obj.get("accepts")
    if isinstance(accepts, list) and accepts and isinstance(accepts[0], dict):
        return accepts[0]

    if "scheme" in obj:
        return obj

    raise ValueError("No single payment requirement with 'scheme' was found.")


def build_x402_challenge(
    *,
    path: str,
    amount_usd,
    method: str = "GET",
    network: str = X402_DEFAULT_NETWORK,
    token: str = X402_DEFAULT_TOKEN,
    scheme: str = X402_DEFAULT_SCHEME,
    pay_to: str = X402_SELLER_ADDRESS,
    challenge_mode: str | None = None,
) -> tuple[dict[str, Any], str]:
    if not isinstance(amount_usd, Decimal):
        amount_usd = Decimal(str(amount_usd))

    resolved_challenge_mode = normalize_payment_required_challenge_mode(challenge_mode)
    requirements = build_x402_requirements(
        path=path,
        amount_usd=amount_usd,
        method=method,
        network=network,
        token=token,
        scheme=scheme,
        pay_to=pay_to,
        challenge_mode=resolved_challenge_mode,
    )

    challenge_body = {
        "error": "payment_required",
        "detail": "Payment is required to access this endpoint.",
        "protocol": "x402",
        "resource": requirements["resource"]["url"],
        "pricing": {
            "amount_usd": f"{amount_usd:.6f}",
            "unit": "request",
            "network": network,
            "token": token,
            "scheme": scheme,
        },
        "accepted_payment_methods": ["x402"],
        "payment_required": requirements,
    }

    payment_required_header = _b64_json(requirements)
    return challenge_body, payment_required_header


# =========================================================
# HEADER / PAYMENT DETECTION
# =========================================================

def _get_header(headers, canonical_name: str):
    """Read a canonical header from Starlette Headers or a plain mapping."""
    return headers.get(canonical_name) or headers.get(canonical_name.lower())


def is_x402_payment_method(headers_or_payment_method) -> bool:
    """
    True when the caller is on the x402 rail — by declaration, hint, or proof.

    Rail identification, deliberately broader than `has_x402_payment_proof` and
    strictly separate from it.  Naming x402 in `X-StockTrends-Payment-Method`
    selects the rail while paying nothing, and an `Authorization: x402 …` header
    is an x402-intent hint that the verify/settle path never consumes as an
    artifact.  Both belong here and neither is payment.

    Use this to decide *which rail* a request is on.  Use
    `has_x402_payment_proof` to decide whether it has actually presented an
    authorization artifact.  Conflating the two is what let an unpaid caller
    suppress the challenge it needed.
    """
    if headers_or_payment_method is None:
        return False

    if isinstance(headers_or_payment_method, str):
        return headers_or_payment_method.strip().lower() == "x402"

    headers = headers_or_payment_method
    payment_method = headers.get("x-stocktrends-payment-method", "")
    if isinstance(payment_method, str) and payment_method.strip().lower() == "x402":
        return True

    if has_x402_payment_proof(headers):
        return True

    # Rail hint only.  Retained because it predates PR3 and identifies the rail;
    # it is emphatically not routed through the proof predicate.
    auth = _get_header(headers, "Authorization") or ""
    return isinstance(auth, str) and auth.strip().lower().startswith("x402")


def has_payment_signature(headers) -> bool:
    """
    True when a request carries an artifact the facilitator path can actually use.

    Defined as "extraction yields something", never as raw header truthiness.
    The two are not the same: a header present but blank is truthy while
    `extract_payment_signature` normalizes it away, and that gap was a second
    definition of "payment presented" — one that classified a whitespace-only
    `X-Payment` as payment-bearing, suppressed the early challenge, and answered
    a bare canonical probe with an input error while no consumable artifact
    existed anywhere in the request.

    Deriving it from the extractor makes the two agree by construction rather
    than by both being maintained correctly.
    """
    return extract_payment_signature(headers) is not None


def has_x402_payment_proof(headers) -> bool:
    """
    True when the request presents an x402 payment artifact this system accepts.

    The canonical, single definition of "this caller has presented payment", and
    deliberately nothing more than `has_payment_signature`: the carrier set is
    exactly the published `X402_PROOF_HEADERS` contract, which is what
    `enforce_x402_payment` gates on, and the value must survive the same
    normalization `extract_payment_signature` applies before handing the
    artifact to the facilitator.  Presence and extractability are therefore one
    question, not two — a blank carrier is no artifact at all.

    The set must not be wider than what verify/settle can actually consume.  An
    earlier revision also accepted `Authorization: x402 …`, which no part of the
    enforcement path parses as an artifact and which the published contract does
    not advertise.  The result was two definitions of proof: a caller sending
    that header alone was classified payment-bearing by the early-challenge
    guard, skipped the challenge, and received an application input error for a
    bare canonical probe — the exact failure PR3 exists to remove — while
    enforcement would have treated the very same request as unpaid.
    `Authorization: x402` remains a rail *hint* in `is_x402_payment_method`;
    rail identification is not payment.

    Descriptive Stock Trends payment headers — network, token, amount,
    reference, channel id — are likewise not proof: a caller can state what it
    intends to pay with while holding no authorization at all, and that caller
    is precisely the one that needs the challenge.
    `X-StockTrends-Payment-Method` is a rail declaration, not payment.

    Any layer asking "has this caller presented payment?" must call this rather
    than assembling a second header list of its own.  Widening it is a change to
    the published proof contract and must be made in
    `payments.x402_contract.X402_PROOF_HEADERS`, so discovery, OpenAPI, CORS,
    enforcement and this predicate move together.
    """
    return has_payment_signature(headers)


def extract_payment_signature(headers) -> Optional[str]:
    """
    The x402 artifact this request presents, normalized, or `None`.

    The single definition of both *whether* an artifact was presented and *what*
    the facilitator receives.  `has_payment_signature`, `has_x402_payment_proof`
    and the early-challenge guard all resolve to this function, so there is one
    normalization and no way for presence and extraction to disagree.

    Normalization is `str.strip()`, which removes every character Python
    considers whitespace — ASCII spaces and tabs, and Unicode whitespace such as
    NBSP where the HTTP stack lets it through.  A carrier that normalizes to
    nothing is *not* an artifact: the caller is unpaid and needs the challenge.

    A non-blank value that happens to be malformed IS an artifact.  It takes the
    payment-bearing path and is rejected later as an invalid payment, which is a
    different outcome from having presented nothing at all — and deliberately
    so.

    Carriers are tried in `X402_PROOF_HEADERS` order and a blank one does not
    stop the search, so a request with a blank `PAYMENT-SIGNATURE` and a real
    `X-Payment` still resolves to the real artifact.
    """
    if headers is None:
        return None

    for header_name in x402_contract.X402_PROOF_HEADERS:
        value = _get_header(headers, header_name)
        if not isinstance(value, str):
            # Only a string can be normalized into an artifact.  Skipping keeps
            # presence and extraction identical for every possible input.
            continue
        normalized = value.strip()
        if normalized:
            return normalized

    return None


# =========================================================
# PAYMENT PAYLOAD HANDLING
# =========================================================

def _parse_payment_payload_from_header(raw_value: str) -> dict[str, Any]:
    value = raw_value.strip()

    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    try:
        return _decode_b64_json(value)
    except Exception:
        pass

    raise ValueError("PAYMENT-SIGNATURE is neither JSON nor base64-encoded JSON object.")


def safe_x402_artifact_reference(raw_value: str | None) -> str | None:
    """A non-reversible accounting reference for rejected x402 artifacts."""
    if not raw_value:
        return None
    return "x402:artifact-v1:" + hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def _normal_address(value: Any, field: str) -> str:
    if not isinstance(value, str) or value[:2].lower() != "0x" or len(value) != 42:
        raise ValueError(f"{field} must be a 20-byte hexadecimal address.")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be a hexadecimal address.") from exc
    return value.lower()


def _normal_uint(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{field} must be an unsigned integer.")
    text_value = str(value)
    if not text_value.isdigit():
        raise ValueError(f"{field} must be an unsigned integer.")
    return str(int(text_value, 10))


def _normal_nonce(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 66:
        raise ValueError("authorization nonce must be a 32-byte hexadecimal value.")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise ValueError("authorization nonce must be hexadecimal.") from exc
    return value.lower()


def _require_signature(value: Any) -> None:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 132:
        raise ValueError("EIP-3009 signature must be a 65-byte hexadecimal value.")
    try:
        binascii.unhexlify(value[2:])
    except (binascii.Error, ValueError) as exc:
        raise ValueError("EIP-3009 signature must be hexadecimal.") from exc


def build_x402_payment_identity(
    payment_signature: str,
    payment_requirements: dict[str, Any] | str,
) -> X402PaymentIdentity:
    """Strictly parse the sole supported V2/exact/EIP-3009 proof format.

    Cryptographic validity remains the facilitator's responsibility.  This only
    establishes an unambiguous authorization identity before a settlement may
    be claimed.
    """
    proof = _parse_payment_payload_from_header(payment_signature)
    requirement = _normalize_payment_requirements_input(payment_requirements)
    if proof.get("x402Version") != 2 or requirement.get("scheme") != "exact":
        raise ValueError("Only x402 V2 exact payments are supported for atomic claims.")
    # PaymentPayload V2 contains the selected requirement under ``accepted``;
    # resource and extensions are standard optional fields and intentionally do
    # not participate in the authorization identity.
    if set(proof) - {"x402Version", "resource", "accepted", "payload", "extensions"}:
        raise ValueError("Payment payload has unsupported top-level fields.")
    accepted = proof.get("accepted")
    if not isinstance(accepted, dict):
        raise ValueError("PaymentPayload accepted requirements are required.")
    if set(accepted) - {"scheme", "network", "amount", "asset", "payTo", "maxTimeoutSeconds", "extra"}:
        raise ValueError("Accepted payment requirements are unsupported.")
    required_requirement_fields = {"scheme", "network", "amount", "asset", "payTo", "maxTimeoutSeconds"}
    if not required_requirement_fields.issubset(accepted):
        raise ValueError("Accepted payment requirements are incomplete.")
    if accepted.get("scheme") != "exact":
        raise ValueError("Payment scheme must be exact.")
    network = accepted.get("network")
    if not isinstance(network, str) or network != requirement.get("network"):
        raise ValueError("Payment network does not match server requirements.")
    asset = _normal_address(accepted.get("asset"), "payment asset")
    if asset != _normal_address(requirement.get("asset"), "required asset"):
        raise ValueError("Payment asset does not match server requirements.")
    extra = accepted.get("extra")
    required_extra = requirement.get("extra")
    if not isinstance(extra, dict) or extra.get("assetTransferMethod") != "eip3009":
        raise ValueError("Server requirements do not specify EIP-3009.")
    if not isinstance(required_extra, dict) or required_extra.get("assetTransferMethod") != "eip3009":
        raise ValueError("Server requirements do not specify EIP-3009.")
    if accepted.get("maxTimeoutSeconds") != requirement.get("maxTimeoutSeconds"):
        raise ValueError("Accepted timeout does not match server requirements.")
    payload = proof.get("payload")
    if not isinstance(payload, dict) or set(payload) - {"authorization", "signature"}:
        raise ValueError("Payment payload is ambiguous or unsupported.")
    authorization = payload.get("authorization")
    if not isinstance(authorization, dict):
        raise ValueError("EIP-3009 authorization is required.")
    required_fields = {"from", "to", "value", "validAfter", "validBefore", "nonce"}
    if set(authorization) != required_fields:
        raise ValueError("EIP-3009 authorization has unsupported or missing fields.")
    sender = _normal_address(authorization["from"], "authorization from")
    recipient = _normal_address(authorization["to"], "authorization to")
    if recipient != _normal_address(accepted.get("payTo"), "accepted payTo") or recipient != _normal_address(requirement.get("payTo"), "required payTo"):
        raise ValueError("Authorization recipient does not match server requirements.")
    value = _normal_uint(authorization["value"], "authorization value")
    if value != _normal_uint(accepted.get("amount"), "accepted amount") or value != _normal_uint(requirement.get("amount"), "required amount"):
        raise ValueError("Authorization value does not match server requirements.")
    valid_after = _normal_uint(authorization["validAfter"], "authorization validAfter")
    valid_before = _normal_uint(authorization["validBefore"], "authorization validBefore")
    if int(valid_before) <= int(valid_after):
        raise ValueError("Authorization validity window is invalid.")
    nonce = _normal_nonce(authorization["nonce"])
    _require_signature(payload.get("signature"))
    canonical = {
        "identity_version": X402_IDENTITY_VERSION, "x402_version": 2,
        "scheme": "exact", "network": network, "asset": asset,
        "asset_transfer_method": "eip3009", "sender": sender,
        "recipient": recipient, "amount": value, "valid_after": valid_after,
        "valid_before": valid_before, "nonce": nonce,
    }
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).digest()
    return X402PaymentIdentity(X402_IDENTITY_VERSION, digest, "x402:v1:" + digest.hex())


def encode_payment_response_header(payload: dict[str, Any]) -> str:
    return _b64_json(payload)


def _normalize_payment_requirements_input(payment_requirements: Any) -> dict[str, Any]:
    return _extract_single_requirement(payment_requirements)


def _extract_x402_amount_native(payload: dict[str, Any]) -> Decimal | None:
    raw_amount = (
        payload.get("amount")
        or payload.get("maxAmountRequired")
        or payload.get("value")
        or payload.get("paymentAmount")
    )
    if raw_amount is None:
        payment_payload = payload.get("paymentPayload")
        if isinstance(payment_payload, dict):
            nested_payload = payment_payload.get("payload")
            if isinstance(nested_payload, dict):
                authorization = nested_payload.get("authorization")
                if isinstance(authorization, dict):
                    raw_amount = authorization.get("value")
    if raw_amount is None:
        nested_payload = payload.get("payload")
        if isinstance(nested_payload, dict):
            authorization = nested_payload.get("authorization")
            if isinstance(authorization, dict):
                raw_amount = authorization.get("value")
    if raw_amount is None:
        accepted = payload.get("accepted")
        if isinstance(accepted, dict):
            raw_amount = accepted.get("amount")
    return _parse_decimal(str(raw_amount)) if raw_amount is not None else None


# =========================================================
# AMOUNT SUFFICIENCY
#
# One definition of the minimum-charge rule, shared by optional validation and
# by enforcement.
#
# `VALIDATE_AGENT_PAY_HEADERS` used to be the only thing standing between an
# underpaid artifact and the facilitator: with validation off, nothing compared
# the presented amount to the quoted price, and enforcement went straight to
# verify and settle.  An economic minimum must not be switchable by an optional
# validation flag, so `enforce_x402_payment` applies this same helper before it
# contacts the facilitator, whatever the flag says.
# =========================================================

INSUFFICIENT_PAYMENT_AMOUNT_ERROR = "insufficient_payment_amount"


def x402_required_amount_atomic(required_amount_usd: Decimal) -> Decimal:
    """The quoted price as an atomic token amount, in the token's decimals."""
    if not isinstance(required_amount_usd, Decimal):
        required_amount_usd = Decimal(str(required_amount_usd))
    return Decimal(_to_atomic_units(required_amount_usd, X402_DEFAULT_TOKEN_DECIMALS))


def x402_insufficient_amount_detail(
    payment_amount_native: Decimal | None,
    required_amount_usd: Decimal,
) -> str | None:
    """
    The rejection detail for an underpaid artifact, or `None` if acceptable.

    `None` for an amount at or above the requirement, and `None` when the
    artifact carries no amount this layer can read — an unreadable amount is not
    evidence of underpayment, and the facilitator remains the authority on
    whether the payload actually pays.  Presenting *less* than the quoted price
    is the one thing decidable here, so it is the one thing rejected here.
    """
    if payment_amount_native is None:
        return None

    required_amount_atomic = x402_required_amount_atomic(required_amount_usd)
    if payment_amount_native < required_amount_atomic:
        return (
            f"Presented payment amount {payment_amount_native} is less than "
            f"required amount {required_amount_atomic}."
        )
    return None


# =========================================================
# VALIDATION
# =========================================================

def validate_x402_payment(
    headers,
    *,
    required_amount_usd: Decimal,
) -> X402ValidationResult:
    artifact = extract_payment_signature(headers)
    if not artifact:
        return X402ValidationResult(
            valid=False,
            error_code="missing_payment_signature",
            error_detail="PAYMENT-SIGNATURE header is required.",
        )

    try:
        payload = _parse_payment_payload_from_header(artifact)
    except Exception as e:
        return X402ValidationResult(
            valid=False,
            error_code="invalid_payment_signature",
            error_detail=f"Could not decode PAYMENT-SIGNATURE payload: {e}",
        )

    amount_native: Optional[Decimal] = None
    payment_reference: Optional[str] = None
    payment_network: Optional[str] = None
    payment_token: Optional[str] = None

    if isinstance(payload, dict):
        payment_reference = str(
            payload.get("paymentIdentifier")
            or payload.get("payment_id")
            or payload.get("id")
            or artifact
        )

        payment_network = (
            payload.get("network")
            or payload.get("chain")
            or payload.get("paymentNetwork")
        )

        payment_token = (
            payload.get("asset")
            or payload.get("tokenAddress")
            or payload.get("contractAddress")
            or payload.get("paymentTokenAddress")
        )

        amount_native = _extract_x402_amount_native(payload)

    insufficient_detail = x402_insufficient_amount_detail(
        amount_native, required_amount_usd
    )

    if insufficient_detail is not None:
        return X402ValidationResult(
            valid=False,
            error_code=INSUFFICIENT_PAYMENT_AMOUNT_ERROR,
            error_detail=insufficient_detail,
            payment_signature=artifact,
            payment_payload=payload,
            payment_reference=payment_reference,
            payment_network=payment_network,
            payment_token=payment_token,
            payment_amount_native=amount_native,
        )

    return X402ValidationResult(
        valid=True,
        payment_signature=artifact,
        payment_payload=payload,
        payment_reference=payment_reference or artifact,
        payment_network=payment_network,
        payment_token=payment_token,
        payment_amount_native=amount_native,
    )


def extract_x402_payment_context(headers) -> X402ValidationResult:
    artifact = extract_payment_signature(headers)
    if not artifact:
        return X402ValidationResult(
            valid=False,
            error_code="missing_payment_signature",
            error_detail="PAYMENT-SIGNATURE header is required.",
        )

    try:
        payload = _parse_payment_payload_from_header(artifact)
    except Exception as e:
        return X402ValidationResult(
            valid=False,
            error_code="invalid_payment_signature",
            error_detail=f"Could not decode PAYMENT-SIGNATURE payload: {e}",
        )

    amount_native: Optional[Decimal] = None
    payment_reference: Optional[str] = None
    payment_network: Optional[str] = None
    payment_token: Optional[str] = None

    if isinstance(payload, dict):
        payment_reference = str(
            payload.get("paymentIdentifier")
            or payload.get("payment_id")
            or payload.get("id")
            or artifact
        )

        payment_network = (
            payload.get("network")
            or payload.get("chain")
            or payload.get("paymentNetwork")
        )

        payment_token = (
            payload.get("asset")
            or payload.get("tokenAddress")
            or payload.get("contractAddress")
            or payload.get("paymentTokenAddress")
        )

        amount_native = _extract_x402_amount_native(payload)

    return X402ValidationResult(
        valid=True,
        payment_signature=artifact,
        payment_payload=payload,
        payment_reference=payment_reference or artifact,
        payment_network=payment_network,
        payment_token=payment_token,
        payment_amount_native=amount_native,
    )


# =========================================================
# FACILITATOR
# =========================================================

def verify_with_facilitator(
    *,
    payment_signature: str,
    payment_requirements: dict[str, Any] | str,
) -> X402ValidationResult:
    try:
        payment_payload = _parse_payment_payload_from_header(payment_signature)
    except Exception as e:
        return X402ValidationResult(
            valid=False,
            error_code="invalid_payment_signature",
            error_detail=f"Invalid PAYMENT-SIGNATURE payload: {e}",
        )

    try:
        normalized_requirements = _normalize_payment_requirements_input(payment_requirements)
    except Exception as e:
        return X402ValidationResult(
            valid=False,
            error_code="invalid_payment_requirements",
            error_detail=f"Invalid payment requirements payload: {e}",
        )

    request_body = {
        "x402Version": int(
            payment_payload.get("x402Version", x402_contract.X402_VERSION)
        ),
        "paymentPayload": payment_payload,
        "paymentRequirements": normalized_requirements,
    }

    status, data, raw = _post_json(
        f"{X402_FACILITATOR_URL}/verify",
        request_body,
    )
    logger.info(
        "x402 facilitator operation=verify status=%s outcome=%s",
        status,
        "unreachable" if status == 0 else "http_error" if status >= 400 else "received",
    )

    if status == 0:
        return X402ValidationResult(
            valid=False,
            error_code="facilitator_verify_unreachable",
            error_detail="Facilitator /verify is unavailable.",
        )

    if status >= 400:
        detail = f"Facilitator /verify returned HTTP {status}."

        return X402ValidationResult(
            valid=False,
            error_code="facilitator_verify_failed",
            error_detail=detail,
            payment_signature=payment_signature,
            payment_payload=payment_payload,
            verification_response=_safe_facilitator_receipt(data),
        )

    verified = bool((data or {}).get("isValid") or (data or {}).get("valid"))
    if not verified:
        detail = "Facilitator reported invalid payment payload."

        return X402ValidationResult(
            valid=False,
            error_code="payment_verification_failed",
            error_detail=detail,
            payment_signature=payment_signature,
            payment_payload=payment_payload,
            verification_response=_safe_facilitator_receipt(data),
        )

    return X402ValidationResult(
        valid=True,
        payment_signature=payment_signature,
        payment_payload=payment_payload,
        verification_response=_safe_facilitator_receipt(data),
    )


def settle_with_facilitator(
    *,
    payment_signature: str,
    payment_requirements: dict[str, Any] | str,
) -> X402ValidationResult:
    try:
        payment_payload = _parse_payment_payload_from_header(payment_signature)
    except Exception as e:
        return X402ValidationResult(
            valid=False,
            error_code="invalid_payment_signature",
            error_detail=f"Invalid PAYMENT-SIGNATURE payload: {e}",
        )

    try:
        normalized_requirements = _normalize_payment_requirements_input(payment_requirements)
    except Exception as e:
        return X402ValidationResult(
            valid=False,
            error_code="invalid_payment_requirements",
            error_detail=f"Invalid payment requirements payload: {e}",
        )

    request_body = {
        "x402Version": int(
            payment_payload.get("x402Version", x402_contract.X402_VERSION)
        ),
        "paymentPayload": payment_payload,
        "paymentRequirements": normalized_requirements,
    }

    status, data, raw = _post_json(
        f"{X402_FACILITATOR_URL}/settle",
        request_body,
    )
    logger.info(
        "x402 facilitator operation=settle status=%s outcome=%s",
        status,
        "unreachable" if status == 0 else "http_error" if status >= 400 else "received",
    )

    if status == 0:
        return X402ValidationResult(
            valid=False,
            error_code="facilitator_settle_unreachable",
            error_detail="Facilitator /settle is unavailable.",
        )

    if status >= 400:
        detail = f"Facilitator /settle returned HTTP {status}."

        return X402ValidationResult(
            valid=False,
            error_code="facilitator_settle_failed",
            error_detail=detail,
            payment_signature=payment_signature,
            payment_payload=payment_payload,
            settlement_response=_safe_facilitator_receipt(data),
        )

    # ``success`` is the V2 terminal settlement signal.  A transaction hash
    # alone may accompany settlement_pending, and any contradictory or
    # malformed result remains deliberately uncertain to the claim layer.
    settled_field = data.get("settled") if isinstance(data, dict) else None
    pending_field = data.get("settlement_pending") if isinstance(data, dict) else None
    settled = (
        isinstance(data, dict)
        and data.get("success") is True
        and ("settled" not in data or settled_field is True)
        and ("settlement_pending" not in data or pending_field is False)
        and data.get("errorReason") in (None, "")
    )
    if not settled:
        return X402ValidationResult(
            valid=False,
            error_code="payment_settlement_failed",
            error_detail="Facilitator did not confirm settlement.",
            payment_signature=payment_signature,
            payment_payload=payment_payload,
            settlement_response=_safe_facilitator_receipt(data),
        )

    return X402ValidationResult(
        valid=True,
        payment_signature=payment_signature,
        payment_payload=payment_payload,
        settlement_response=_safe_facilitator_receipt(data),
    )
