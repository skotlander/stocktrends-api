from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from payments.challenge import (
    CHALLENGE_ERROR_CODE,
    CHALLENGE_ERROR_DETAIL,
    challenge_mode_from_headers,
)
from payments.x402 import (
    INSUFFICIENT_PAYMENT_AMOUNT_ERROR,
    build_x402_challenge,
    build_x402_requirements,
    extract_payment_signature,
    extract_x402_payment_context,
    has_payment_signature,
    settle_with_facilitator,
    verify_with_facilitator,
    x402_insufficient_amount_detail,
)
from payments.mpp import enforce_mpp_payment


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
            payment_reference=normalized_payment_reference,
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
            payment_reference=normalized_payment_reference,
            payment_network=normalized_payment_network or required_network,
            payment_token=normalized_payment_token or required_token,
            payment_amount_native=normalized_payment_amount_native,
        )

    replay_reference = normalized_payment_reference
    if replay_reference and replay_checker(replay_reference):
        return PaymentEnforcementResult(
            outcome="replay_detected",
            error_code="replay_detected",
            error_detail="Payment reference has already been used.",
            payment_reference=replay_reference,
            payment_network=normalized_payment_network or required_network,
            payment_token=normalized_payment_token or required_token,
            payment_amount_native=normalized_payment_amount_native,
        )

    payment_signature = extract_payment_signature(headers)

    verify_result = verify_with_facilitator(
        payment_signature=payment_signature,
        payment_requirements=current_payment_requirements,
    )
    if not verify_result.valid:
        return PaymentEnforcementResult(
            outcome="verification_failed",
            error_code="payment_verification_failed",
            error_detail=verify_result.error_detail,
            payment_reference=replay_reference,
            payment_network=normalized_payment_network or required_network,
            payment_token=normalized_payment_token or required_token,
            payment_amount_native=normalized_payment_amount_native,
        )

    settle_result = settle_with_facilitator(
        payment_signature=payment_signature,
        payment_requirements=current_payment_requirements,
    )
    if not settle_result.valid:
        return PaymentEnforcementResult(
            outcome="settlement_failed",
            error_code="payment_settlement_failed",
            error_detail=settle_result.error_detail,
            payment_reference=replay_reference,
            payment_network=normalized_payment_network or required_network,
            payment_token=normalized_payment_token or required_token,
            payment_amount_native=normalized_payment_amount_native,
        )

    return PaymentEnforcementResult(
        outcome="proceed",
        payment_reference=replay_reference,
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
