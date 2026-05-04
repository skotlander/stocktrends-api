import base64
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
X402_TIMEOUT_SECONDS = float(os.getenv("X402_TIMEOUT_SECONDS", "10"))
X402_API_BASE_URL = os.getenv("X402_API_BASE_URL", "").rstrip("/")


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
) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "name": X402_DEFAULT_TOKEN_NAME,
        "version": X402_DEFAULT_TOKEN_VERSION,
    }
    if X402_DEFAULT_ASSET_TRANSFER_METHOD:
        extra["assetTransferMethod"] = X402_DEFAULT_ASSET_TRANSFER_METHOD

    http_method = method.upper()
    resource_url = f"{X402_API_BASE_URL}{path}" if X402_API_BASE_URL else path

    # Bazaar extension shape differs by method class.
    # GET/HEAD/DELETE: query-param style (no body fields required).
    # POST/PUT/PATCH:  body style — declare bodyType and an empty body schema.
    _BAZAAR_OUTPUT: dict[str, Any] = {
        "type": "json",
        "description": "JSON response returned after successful payment.",
        "example": {},
    }

    _QUERY_METHODS = {"GET", "HEAD", "DELETE"}
    if http_method in _QUERY_METHODS:
        bazaar_info: dict[str, Any] = {
            "input": {"type": "http", "method": http_method},
            "output": _BAZAAR_OUTPUT,
        }
        bazaar_schema_input_props: dict[str, Any] = {
            "type": {"type": "string", "const": "http"},
            "method": {"type": "string", "enum": [http_method]},
        }
        bazaar_schema_required = ["type", "method"]
    else:
        bazaar_info = {
            "input": {"type": "http", "method": http_method, "bodyType": "json", "body": {}},
            "output": _BAZAAR_OUTPUT,
        }
        bazaar_schema_input_props = {
            "type": {"type": "string", "const": "http"},
            "method": {"type": "string", "enum": [http_method]},
            "bodyType": {"type": "string", "enum": ["json"]},
            "body": {},
        }
        bazaar_schema_required = ["type", "method", "bodyType", "body"]

    return {
        "x402Version": 2,
        # V2 canonical resource identity (ResourceInfo) — separate from accepts entries.
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": mime_type,
        },
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
        "extensions": {
            "bazaar": {
                "info": bazaar_info,
                "schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "object",
                            "properties": bazaar_schema_input_props,
                            "required": bazaar_schema_required,
                            "additionalProperties": False,
                        },
                        "output": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "description": {"type": "string"},
                                "example": {},
                            },
                            "required": ["type"],
                        },
                    },
                    "required": ["input", "output"],
                },
            }
        },
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
) -> tuple[dict[str, Any], str]:
    if not isinstance(amount_usd, Decimal):
        amount_usd = Decimal(str(amount_usd))

    requirements = build_x402_requirements(
        path=path,
        amount_usd=amount_usd,
        method=method,
        network=network,
        token=token,
        scheme=scheme,
        pay_to=pay_to,
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

def is_x402_payment_method(headers_or_payment_method) -> bool:
    if headers_or_payment_method is None:
        return False

    if isinstance(headers_or_payment_method, str):
        return headers_or_payment_method.strip().lower() == "x402"

    headers = headers_or_payment_method
    payment_method = headers.get("x-stocktrends-payment-method", "")
    if isinstance(payment_method, str) and payment_method.strip().lower() == "x402":
        return True

    if headers.get("payment-signature"):
        return True

    if headers.get("x-payment"):
        return True

    auth = headers.get("authorization", "")
    if isinstance(auth, str) and auth.lower().startswith("x402"):
        return True

    return False


def has_payment_signature(headers) -> bool:
    if headers is None:
        return False
    return bool(
        headers.get("payment-signature")
        or headers.get("x-payment")
    )


def extract_payment_signature(headers) -> Optional[str]:
    if headers is None:
        return None

    value = headers.get("payment-signature")
    if value:
        value = value.strip()
        if value:
            return value

    value = headers.get("x-payment")
    if value:
        value = value.strip()
        if value:
            return value

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
        accepted = payload.get("accepted")
        if isinstance(accepted, dict):
            raw_amount = accepted.get("amount")
    return _parse_decimal(str(raw_amount)) if raw_amount is not None else None


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

    required_amount_atomic = Decimal(
        _to_atomic_units(required_amount_usd, X402_DEFAULT_TOKEN_DECIMALS)
    )

    if amount_native is not None and amount_native < required_amount_atomic:
        return X402ValidationResult(
            valid=False,
            error_code="insufficient_payment_amount",
            error_detail=(
                f"Presented payment amount {amount_native} is less than "
                f"required amount {required_amount_atomic}."
            ),
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
        "x402Version": int(payment_payload.get("x402Version", 2)),
        "paymentPayload": payment_payload,
        "paymentRequirements": normalized_requirements,
    }

    logger.info("x402 verify request_body=%s", _json_dumps_compact(request_body))
    logger.info("x402 verify requirement=%s", _json_dumps_compact(normalized_requirements))
    logger.info("x402 verify payload=%s", _json_dumps_compact(payment_payload))
    logger.info("x402 facilitator key id=%s", X402_FACILITATOR_API_KEY)

    status, data, raw = _post_json(
        f"{X402_FACILITATOR_URL}/verify",
        request_body,
    )

    logger.info("x402 verify response status=%s body=%s", status, raw)

    if status == 0:
        return X402ValidationResult(
            valid=False,
            error_code="facilitator_verify_unreachable",
            error_detail=raw,
        )

    if status >= 400:
        detail = f"Facilitator /verify returned HTTP {status}"
        if data:
            detail = f"{detail}: {json.dumps(data)}"
        elif raw:
            detail = f"{detail}: {raw}"

        return X402ValidationResult(
            valid=False,
            error_code="facilitator_verify_failed",
            error_detail=detail,
            payment_signature=payment_signature,
            payment_payload=payment_payload,
            verification_response=data,
        )

    verified = bool((data or {}).get("isValid") or (data or {}).get("valid"))
    if not verified:
        invalid_reason = (data or {}).get("invalidReason")
        detail = "Facilitator reported invalid payment payload."
        if invalid_reason:
            detail = f"{detail} invalidReason={invalid_reason}"

        return X402ValidationResult(
            valid=False,
            error_code="payment_verification_failed",
            error_detail=detail,
            payment_signature=payment_signature,
            payment_payload=payment_payload,
            verification_response=data,
        )

    return X402ValidationResult(
        valid=True,
        payment_signature=payment_signature,
        payment_payload=payment_payload,
        verification_response=data,
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
        "x402Version": int(payment_payload.get("x402Version", 2)),
        "paymentPayload": payment_payload,
        "paymentRequirements": normalized_requirements,
    }

    logger.info("x402 settle request_body=%s", _json_dumps_compact(request_body))
    logger.info("x402 settle requirement=%s", _json_dumps_compact(normalized_requirements))
    logger.info("x402 settle payload=%s", _json_dumps_compact(payment_payload))
    logger.info("x402 facilitator key id=%s", X402_FACILITATOR_API_KEY)

    status, data, raw = _post_json(
        f"{X402_FACILITATOR_URL}/settle",
        request_body,
    )

    logger.info("x402 settle response status=%s body=%s", status, raw)

    if status == 0:
        return X402ValidationResult(
            valid=False,
            error_code="facilitator_settle_unreachable",
            error_detail=raw,
        )

    if status >= 400:
        detail = f"Facilitator /settle returned HTTP {status}"
        if data:
            detail = f"{detail}: {json.dumps(data)}"
        elif raw:
            detail = f"{detail}: {raw}"

        return X402ValidationResult(
            valid=False,
            error_code="facilitator_settle_failed",
            error_detail=detail,
            payment_signature=payment_signature,
            payment_payload=payment_payload,
            settlement_response=data,
        )

    settled = bool(
        (data or {}).get("success")
        or (data or {}).get("settled")
        or (data or {}).get("txHash")
        or (data or {}).get("transaction")
    )
    if not settled:
        return X402ValidationResult(
            valid=False,
            error_code="payment_settlement_failed",
            error_detail="Facilitator did not confirm settlement.",
            payment_signature=payment_signature,
            payment_payload=payment_payload,
            settlement_response=data,
        )

    return X402ValidationResult(
        valid=True,
        payment_signature=payment_signature,
        payment_payload=payment_payload,
        settlement_response=data,
    )