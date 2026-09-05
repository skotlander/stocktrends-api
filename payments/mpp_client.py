from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request


logger = logging.getLogger("stocktrends_api.mpp_client")

# ---------------------------------------------------------------------------
# Config — all values from environment, no hardcoded defaults for URLs/secrets.
# ---------------------------------------------------------------------------

_CONTROL_PLANE_BASE_URL: str = os.getenv("CONTROL_PLANE_BASE_URL", "").rstrip("/")
_CONTROL_PLANE_INTERNAL_SECRET: str = os.getenv("CONTROL_PLANE_INTERNAL_SECRET", "")
_MPP_CLIENT_TIMEOUT_SECONDS: float = float(os.getenv("MPP_CLIENT_TIMEOUT_SECONDS", "5"))


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class MppControlPlaneResult:
    success: bool
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    response_data: Optional[dict] = None


#: A control-plane call whose body was not a JSON object.
#:
#: Distinct from a rejection.  The real outcome of the operation is unknown, so
#: it is reported as a structured failure rather than inferred in either
#: direction: coercing an unreadable shape into success would claim a capture
#: the control plane may never have performed.
INVALID_CONTROL_PLANE_RESPONSE_ERROR = "control_plane_invalid_response"


def _response_body(data: Any) -> dict[str, Any]:
    """
    The parsed body as a mapping, for reading optional diagnostic fields.

    `_mpp_post` returns whatever the control plane actually sent, and JSON
    permits a string, number, list, boolean or null at the top level.  Reading
    `error_code` off one of those raises `AttributeError` -- which is how a
    control-plane response *shape* became an exception raised in the middle of
    the request finaliser, taking the request-event and economics rows with it.
    """
    return data if isinstance(data, dict) else {}


def _invalid_response_result(
    operation: str,
    status: int,
    data: Any,
) -> MppControlPlaneResult | None:
    """
    A structured failure when a successful HTTP response is not a JSON object.

    Returns `None` when the body is a usable object and interpretation may
    proceed.  Applied on the success path of every control-plane operation, so
    an unparseable or wrongly-shaped 2xx body fails closed with a stable code
    instead of raising.
    """
    if isinstance(data, dict):
        return None

    return MppControlPlaneResult(
        success=False,
        error_code=INVALID_CONTROL_PLANE_RESPONSE_ERROR,
        error_detail=(
            f"Control plane {operation} returned HTTP {status} with a "
            f"{type(data).__name__} body; a JSON object was expected."
        ),
    )


# ---------------------------------------------------------------------------
# Internal HTTP transport (mirrors the _post_json pattern from payments/x402.py)
# ---------------------------------------------------------------------------

def _mpp_post(
    endpoint: str,
    payload: dict[str, Any],
) -> tuple[int, dict | None, str | None]:
    """
    POST to a control-plane endpoint.

    Returns (http_status, parsed_body_or_None, raw_body_or_error_string).
    Returns (0, None, reason) if the request could not be made at all
    (not configured, connection failure, timeout).
    """
    if not _CONTROL_PLANE_BASE_URL:
        return 0, None, "CONTROL_PLANE_BASE_URL is not configured."

    url = f"{_CONTROL_PLANE_BASE_URL}{endpoint}"
    data = json.dumps(payload).encode("utf-8")

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if _CONTROL_PLANE_INTERNAL_SECRET:
        headers["Authorization"] = f"Bearer {_CONTROL_PLANE_INTERNAL_SECRET}"

    req = urllib_request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib_request.urlopen(req, timeout=_MPP_CLIENT_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
            try:
                parsed = json.loads(body) if body else {}
            except ValueError:
                parsed = None
            return resp.status, parsed, body

    except urllib_error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = str(exc)
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = None
        return exc.code, parsed, body

    except Exception as exc:
        return 0, None, str(exc)


# ---------------------------------------------------------------------------
# Authorize
# ---------------------------------------------------------------------------

def authorize_mpp_payment(
    *,
    channel_id: str,
    payment_reference: str,
    requested_stc: Decimal,
    pricing_rule_id: Optional[str],
    path: str,
    request_id: Optional[str],
) -> MppControlPlaneResult:
    """
    Call the control-plane authorize endpoint before serving the protected request.

    Fails closed: any non-success result (timeout, HTTP error, explicit rejection)
    must cause the caller to return a 402 and not serve the request.
    """
    payload: dict[str, Any] = {
        "channel_id": channel_id,
        "payment_reference": payment_reference,
        "requested_stc": str(requested_stc),
        "pricing_rule_id": pricing_rule_id,
        "endpoint_path": path,
        "request_id": request_id,
    }

    logger.info(
        "mpp authorize channel_id=%s payment_reference=%s requested_stc=%s path=%s request_id=%s",
        channel_id,
        payment_reference,
        requested_stc,
        path,
        request_id,
    )

    status, data, raw = _mpp_post("/v1/internal/mpp/authorize", payload)

    logger.info("mpp authorize response status=%s body=%s", status, raw)

    if status == 0:
        return MppControlPlaneResult(
            success=False,
            error_code="control_plane_unreachable",
            error_detail=raw or "Control plane did not respond.",
        )

    body = _response_body(data)

    if status >= 400:
        error_code = body.get("error_code") or "authorization_failed"
        error_detail = body.get("error_detail") or f"Control plane authorize returned HTTP {status}."
        return MppControlPlaneResult(
            success=False,
            error_code=error_code,
            error_detail=error_detail,
            response_data=body or None,
        )

    invalid = _invalid_response_result("authorize", status, data)
    if invalid is not None:
        return invalid

    # Control-plane authorize returns a reservation object with status="pending"
    # on success (HTTP 200/201).  There is no top-level "authorized" or "success"
    # boolean field.  An "id" field confirms an authorization record was created.
    cp_status = body.get("status")
    authorized = cp_status == "pending" and bool(body.get("id"))
    if not authorized:
        error_code = body.get("error_code") or "authorization_failed"
        error_detail = (
            body.get("error_detail")
            or f"Control plane authorize returned unexpected status: {cp_status!r}."
        )
        return MppControlPlaneResult(
            success=False,
            error_code=error_code,
            error_detail=error_detail,
            response_data=body,
        )

    return MppControlPlaneResult(success=True, response_data=body)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def capture_mpp_payment(
    *,
    channel_id: str,
    payment_reference: str,
    captured_stc: Decimal,
    pricing_rule_id: Optional[str],
    request_id: Optional[str],
) -> MppControlPlaneResult:
    """
    Call the control-plane capture endpoint after a successful protected response.

    Capture failure must NOT retroactively fail the user response.
    The caller is responsible for logging the failure and allowing the response
    to proceed. The control-plane is expected to handle reconciliation via
    expiry/void or async remediation.
    """
    payload: dict[str, Any] = {
        "channel_id": channel_id,
        "payment_reference": payment_reference,
        "captured_stc": str(captured_stc),
        "pricing_rule_id": pricing_rule_id,
        "request_id": request_id,
    }

    logger.info(
        "mpp capture channel_id=%s payment_reference=%s captured_stc=%s request_id=%s",
        channel_id,
        payment_reference,
        captured_stc,
        request_id,
    )

    status, data, raw = _mpp_post("/v1/internal/mpp/capture", payload)

    logger.info("mpp capture response status=%s body=%s", status, raw)

    if status == 0:
        return MppControlPlaneResult(
            success=False,
            error_code="control_plane_unreachable",
            error_detail=raw or "Control plane did not respond.",
        )

    body = _response_body(data)

    if status >= 400:
        error_code = body.get("error_code") or "capture_failed"
        error_detail = body.get("error_detail") or f"Control plane capture returned HTTP {status}."
        return MppControlPlaneResult(
            success=False,
            error_code=error_code,
            error_detail=error_detail,
            response_data=body or None,
        )

    invalid = _invalid_response_result("capture", status, data)
    if invalid is not None:
        return invalid

    # Control-plane capture returns the updated authorization object with
    # status="captured" and captured_at set on success.  Inferred from the same
    # object shape as authorize.  Confirm with control-plane contract if shape differs.
    cp_status = body.get("status")
    captured = cp_status == "captured" or bool(body.get("captured_at"))
    if not captured:
        error_code = body.get("error_code") or "capture_failed"
        error_detail = (
            body.get("error_detail")
            or f"Control plane capture returned unexpected status: {cp_status!r}."
        )
        return MppControlPlaneResult(
            success=False,
            error_code=error_code,
            error_detail=error_detail,
            response_data=body,
        )

    return MppControlPlaneResult(success=True, response_data=body)


# ---------------------------------------------------------------------------
# Void
# ---------------------------------------------------------------------------

def void_mpp_authorization(
    *,
    payment_reference: str,
    request_id: Optional[str],
) -> MppControlPlaneResult:
    """
    Call the control-plane void endpoint after a failed protected response.

    Void is best-effort: the caller must log failures but must NOT alter
    the original API error response returned to the client.

    Success is signalled by the control-plane returning status in
    {"voided", "expired"} — both indicate the reservation is released
    (or was never active).
    """
    payload: dict[str, Any] = {
        "payment_reference": payment_reference,
    }

    logger.info(
        "mpp void payment_reference=%s request_id=%s",
        payment_reference,
        request_id,
    )

    status, data, raw = _mpp_post("/v1/internal/mpp/void", payload)

    logger.info("mpp void response status=%s body=%s", status, raw)

    if status == 0:
        return MppControlPlaneResult(
            success=False,
            error_code="control_plane_unreachable",
            error_detail=raw or "Control plane did not respond.",
        )

    body = _response_body(data)

    if status >= 400:
        error_code = body.get("error_code") or "void_failed"
        error_detail = (
            body.get("error_detail")
            or f"Control plane void returned HTTP {status}."
        )
        return MppControlPlaneResult(
            success=False,
            error_code=error_code,
            error_detail=error_detail,
            response_data=body or None,
        )

    invalid = _invalid_response_result("void", status, data)
    if invalid is not None:
        return invalid

    # Control-plane void returns the authorization object.  Both "voided" and
    # "expired" indicate the reservation is released (or was already released).
    cp_status = body.get("status")
    if cp_status not in ("voided", "expired"):
        return MppControlPlaneResult(
            success=False,
            error_code="void_unexpected_status",
            error_detail=(
                f"Control plane void returned unexpected status: {cp_status!r}."
            ),
            response_data=body,
        )

    return MppControlPlaneResult(success=True, response_data=body)
