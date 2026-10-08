from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import logging
import os
from typing import Callable, Optional

from payments.challenge import (
    CHALLENGE_ERROR_CODE,
    CHALLENGE_ERROR_DETAIL,
    challenge_mode_from_headers,
)
from payments.x402 import (
    INSUFFICIENT_PAYMENT_AMOUNT_ERROR,
    build_x402_challenge,
    build_x402_payment_identity,
    build_x402_requirements,
    extract_payment_signature,
    extract_x402_payment_context,
    has_payment_signature,
    settle_with_facilitator,
    verify_with_facilitator,
    x402_insufficient_amount_detail,
    safe_x402_artifact_reference,
)
from payments.x402_claims import (
    ClaimRepositoryUnavailable,
    acquire_settling_claim,
    record_failed,
    record_settled,
    record_uncertain,
)
from payments.mpp import enforce_mpp_payment


logger = logging.getLogger("stocktrends_api.x402.enforcement")


class ReplayCheckUnavailable(Exception):
    """Raised when x402 replay state cannot be safely determined."""


def _extract_x402_requirement_context(payment_requirements: dict) -> tuple[str | None, str | None]:
    accepts = payment_requirements.get("accepts")
    if not isinstance(accepts, list) or not accepts or not isinstance(accepts[0], dict):
        return None, None

    requirement = accepts[0]
    network = requirement.get("network")
    token = requirement.get("asset")

    return network, token


@dataclass
class PaymentEnforcementResult:
    outcome: str
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    challenge_body: Optional[dict] = None
    payment_required_header: Optional[str] = None
    payment_reference: Optional[str] = None
    payment_network: Optional[str] = None
    payment_token: Optional[str] = None
    payment_amount_native: Optional[Decimal] = None
    payment_channel_id: Optional[str] = None
    payment_response: Optional[dict] = None


_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off", ""}


def _fail_closed_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    # Explicitly configured ambiguity must never reopen settlement or select
    # the legacy non-atomic path.
    return True


def _atomic_claims_enabled() -> bool:
    return _fail_closed_flag("X402_ATOMIC_CLAIMS_ENABLED", default=False)


def _settlement_suspended() -> bool:
    return _fail_closed_flag("X402_SETTLEMENT_SUSPENDED", default=False)


def x402_claim_control_state() -> dict[str, bool]:
    """Safe deployment/preflight view; it intentionally exposes no env values."""
    return {"atomic_claims_enabled": _atomic_claims_enabled(), "settlement_suspended": _settlement_suspended()}


def log_x402_claim_control_state() -> None:
    state = x402_claim_control_state()
    logger.info("x402 claim controls atomic_claims_enabled=%s settlement_suspended=%s", state["atomic_claims_enabled"], state["settlement_suspended"])


def enforce_x402_payment(
    *,
    headers,
    path: str,
    method: str,
    amount_usd: Decimal,
    validation_valid: bool,
    validation_error: str | None,
    validation_detail: str | None,
    validated_payment_reference: str | None,
    validated_payment_network: str | None,
    validated_payment_token: str | None,
    validated_payment_amount_native: Decimal | None,
    replay_checker: Callable[[str], bool],
    replay_checker_many: Callable[[tuple[str, ...]], bool] | None = None,
    request_id: str | None = None,
    **_kwargs,
) -> PaymentEnforcementResult:
    challenge_mode_header = challenge_mode_from_headers(headers)
    current_payment_requirements = build_x402_requirements(
        path=path,
        amount_usd=amount_usd,
        method=method,
    )
    required_network, required_token = _extract_x402_requirement_context(current_payment_requirements)

    if not has_payment_signature(headers):
        challenge_body, payment_required_header = build_x402_challenge(
            path=path,
            amount_usd=amount_usd,
            method=method,
            challenge_mode=challenge_mode_header,
        )
        return PaymentEnforcementResult(
            outcome="challenge",
            error_code=CHALLENGE_ERROR_CODE,
            error_detail=CHALLENGE_ERROR_DETAIL,
            challenge_body=challenge_body,
            payment_required_header=payment_required_header,
            payment_network=required_network,
            payment_token=required_token,
        )

    payment_signature = extract_payment_signature(headers)
    extracted_context = extract_x402_payment_context(headers)
    normalized_payment_reference = validated_payment_reference
    if normalized_payment_reference is None and extracted_context.valid:
        normalized_payment_reference = extracted_context.payment_reference

    normalized_payment_network = validated_payment_network
    if normalized_payment_network is None and extracted_context.valid:
        normalized_payment_network = extracted_context.payment_network

    normalized_payment_token = validated_payment_token
    if normalized_payment_token is None and extracted_context.valid:
        normalized_payment_token = extracted_context.payment_token

    normalized_payment_amount_native = validated_payment_amount_native
    if normalized_payment_amount_native is None and extracted_context.valid:
        normalized_payment_amount_native = extracted_context.payment_amount_native

    if not validation_valid:
        return PaymentEnforcementResult(
            outcome="validation_failed",
            error_code=validation_error,
            error_detail=validation_detail,
            payment_reference=safe_x402_artifact_reference(payment_signature),
            payment_network=normalized_payment_network or required_network,
            payment_token=normalized_payment_token or required_token,
            payment_amount_native=normalized_payment_amount_native,
        )

    # Minimum charge, enforced by the enforcement path itself.
    #
    # `validate_x402_payment` applies the same rule, but only when
    # `VALIDATE_AGENT_PAY_HEADERS` is on.  Economic safety must not depend on an
    # optional validation flag, so the check is repeated here from the same
    # shared helper: whenever enforcement is active, an artifact presenting less
    # than the quoted amount is rejected before the facilitator is contacted, so
    # it can neither verify nor settle.
    #
    # The flag still governs optional validation behaviour; it simply cannot
    # switch off the economic minimum.  With validation on, the identical
    # rejection has already been produced above, so this is a backstop rather
    # than a second rule — one definition, applied at both points.
    insufficient_detail = x402_insufficient_amount_detail(
        normalized_payment_amount_native,
        amount_usd,
    )
    if insufficient_detail is not None:
        return PaymentEnforcementResult(
            outcome="validation_failed",
            error_code=INSUFFICIENT_PAYMENT_AMOUNT_ERROR,
            error_detail=insufficient_detail,
            payment_reference=safe_x402_artifact_reference(payment_signature),
            payment_network=normalized_payment_network or required_network,
            payment_token=normalized_payment_token or required_token,
            payment_amount_native=normalized_payment_amount_native,
        )

    # In claim mode, reject a definitely malformed/unsupported V2 EIP-3009
    # structure before an unindexed historical economics scan.  This is only
    # local structure validation; the facilitator still verifies signatures.
    identity = None
    if _atomic_claims_enabled():
        try:
            identity = build_x402_payment_identity(payment_signature, current_payment_requirements)
        except ValueError:
            return PaymentEnforcementResult(
                outcome="claim_unavailable", error_code="x402_claim_unavailable",
                error_detail="Atomic payment claim protection is unavailable.",
                payment_reference=safe_x402_artifact_reference(payment_signature),
                payment_network=normalized_payment_network or required_network,
                payment_token=normalized_payment_token or required_token,
                payment_amount_native=normalized_payment_amount_native,
            )

    # Legacy lookup remains authoritative for historical references, but raw
    # artifacts must never enter new request economics.
    replay_reference = normalized_payment_reference
    safe_reference = safe_x402_artifact_reference(payment_signature)
    if replay_reference or safe_reference:
        try:
            references = tuple(dict.fromkeys(reference for reference in (replay_reference, safe_reference) if reference))
            if replay_checker_many is not None:
                replay_detected = replay_checker_many(references)
            else:
                replay_detected = bool(replay_reference and replay_checker(replay_reference))
                if not replay_detected and safe_reference:
                    replay_detected = replay_checker(safe_reference)
        except ReplayCheckUnavailable:
            return PaymentEnforcementResult(
                outcome="replay_check_unavailable",
                error_code="replay_check_unavailable",
                error_detail="Payment replay protection is temporarily unavailable. Please retry later.",
                payment_reference=safe_reference,
                payment_network=normalized_payment_network or required_network,
                payment_token=normalized_payment_token or required_token,
                payment_amount_native=normalized_payment_amount_native,
            )

        if replay_detected:
            return PaymentEnforcementResult(
                outcome="replay_detected",
                error_code="replay_detected",
                error_detail="Payment reference has already been used.",
                payment_reference=safe_reference,
                payment_network=normalized_payment_network or required_network,
                payment_token=normalized_payment_token or required_token,
                payment_amount_native=normalized_payment_amount_native,
            )

    verify_result = verify_with_facilitator(
        payment_signature=payment_signature,
        payment_requirements=current_payment_requirements,
    )
    if not verify_result.valid:
        return PaymentEnforcementResult(
            outcome="verification_failed",
            error_code="payment_verification_failed",
            error_detail=verify_result.error_detail,
            payment_reference=safe_x402_artifact_reference(payment_signature),
            payment_network=normalized_payment_network or required_network,
            payment_token=normalized_payment_token or required_token,
            payment_amount_native=normalized_payment_amount_native,
        )

    if _settlement_suspended():
        return PaymentEnforcementResult(
            outcome="settlement_suspended", error_code="x402_settlement_suspended",
            error_detail="x402 settlement is temporarily suspended.",
            payment_reference=safe_x402_artifact_reference(payment_signature),
            payment_network=normalized_payment_network or required_network,
            payment_token=normalized_payment_token or required_token,
            payment_amount_native=normalized_payment_amount_native,
        )

    if _atomic_claims_enabled():
        try:
            claim = acquire_settling_claim(
                identity_version=identity.version, payment_fingerprint=identity.fingerprint,
                owner_request_id=request_id or "unknown-request",
            )
        except ClaimRepositoryUnavailable:
            return PaymentEnforcementResult(
                outcome="claim_unavailable", error_code="x402_claim_unavailable",
                error_detail="Atomic payment claim protection is unavailable.",
                payment_reference=safe_x402_artifact_reference(payment_signature),
                payment_network=normalized_payment_network or required_network,
                payment_token=normalized_payment_token or required_token,
                payment_amount_native=normalized_payment_amount_native,
            )
        if not claim.acquired:
            return PaymentEnforcementResult(
                outcome="claim_exists", error_code="payment_claim_exists",
                error_detail="This payment authorization has already been claimed.",
                payment_reference=identity.accounting_reference,
                payment_network=normalized_payment_network or required_network,
                payment_token=normalized_payment_token or required_token,
                payment_amount_native=normalized_payment_amount_native,
            )

    settle_result = settle_with_facilitator(
        payment_signature=payment_signature,
        payment_requirements=current_payment_requirements,
    )
    if not settle_result.valid:
        if identity is not None:
            try:
                # A transport, HTTP, malformed, pending, or inconsistent
                # facilitator outcome is not evidence that funds did not move.
                record_uncertain(
                    identity_version=identity.version, payment_fingerprint=identity.fingerprint,
                    owner_request_id=request_id or "unknown-request",
                    receipt=settle_result.settlement_response,
                    error_code=settle_result.error_code or "settlement_uncertain",
                )
            except ClaimRepositoryUnavailable:
                pass
        return PaymentEnforcementResult(
            outcome="settlement_uncertain" if identity is not None else "settlement_failed",
            error_code="payment_settlement_uncertain" if identity is not None else "payment_settlement_failed",
            error_detail=settle_result.error_detail,
            payment_reference=identity.accounting_reference if identity else safe_x402_artifact_reference(payment_signature),
            payment_network=normalized_payment_network or required_network,
            payment_token=normalized_payment_token or required_token,
            payment_amount_native=normalized_payment_amount_native,
        )

    if identity is not None:
        try:
            persisted = record_settled(
                identity_version=identity.version, payment_fingerprint=identity.fingerprint,
                owner_request_id=request_id or "unknown-request", receipt=settle_result.settlement_response,
            )
        except ClaimRepositoryUnavailable:
            persisted = False
        if not persisted:
            # Settlement may have happened, but we cannot safely execute the
            # paid endpoint without its durable terminal record.
            return PaymentEnforcementResult(
                outcome="settlement_uncertain", error_code="payment_settlement_uncertain",
                error_detail="Settlement outcome could not be recorded safely.",
                payment_reference=identity.accounting_reference,
                payment_network=normalized_payment_network or required_network,
                payment_token=normalized_payment_token or required_token,
                payment_amount_native=normalized_payment_amount_native,
            )

    return PaymentEnforcementResult(
        outcome="proceed",
        payment_reference=identity.accounting_reference if identity else safe_x402_artifact_reference(payment_signature),
        payment_network=normalized_payment_network or required_network,
        payment_token=normalized_payment_token or required_token,
        payment_amount_native=normalized_payment_amount_native,
        payment_response=settle_result.settlement_response,
    )

def enforce_payment_rail(
    *,
    payment_rail: str,
    **kwargs,
) -> PaymentEnforcementResult:
    if payment_rail == "x402":
        return enforce_x402_payment(**kwargs)

    if payment_rail == "mpp":
        return enforce_mpp_payment(**kwargs)

    return PaymentEnforcementResult(outcome="not_applicable")
