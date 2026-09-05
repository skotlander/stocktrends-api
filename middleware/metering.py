import json
import os
import re
import time
import logging
from dataclasses import dataclass
from uuid import uuid4
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from fastapi import Request

from api.routing import (
    BOUNDARY_NOT_CONSULTED_ERROR,
    PAYMENT_GATE_STATE_ATTR,
    is_payment_wrapped,
)
from metering.logger import (
    log_api_request_event,
    log_api_request_economics,
    get_metering_engine,
)
from payments.challenge import (
    CHALLENGE_ERROR_CODE,
    CHALLENGE_ERROR_DETAIL,
    EARLY_CHALLENGE_RAIL,
    challenge_mode_from_headers,
    decide_early_challenge,
    decorate_x402_challenge,
    is_payment_bearing,
    issue_x402_challenge,
)
from payments.enforcement import enforce_payment_rail
from payments.policy_provider import (
    get_accepted_payment_methods_for_path,
    get_effective_endpoint_payment_policy,
    is_agent_pay_enforcement_path,
)
from pricing.classifier import PricingDecision, classify_request
from payments.x402 import (
    is_x402_payment_method,
    validate_x402_payment,
    encode_payment_response_header,
    X402_DEFAULT_TOKEN_DECIMALS,
)
from services.intelligence_artifact_availability import (
    intelligence_artifact_availability_error_detail,
)

logger = logging.getLogger("stocktrends_api.metering")

ENABLE_AGENT_PAY = os.getenv("ENABLE_AGENT_PAY", "false").lower() == "true"
ENFORCE_AGENT_PAY = os.getenv("ENFORCE_AGENT_PAY", "false").lower() == "true"
VALIDATE_AGENT_PAY_HEADERS = os.getenv("VALIDATE_AGENT_PAY_HEADERS", "false").lower() == "true"

MAX_AGENT_IDENTIFIER_LENGTH = 255
MAX_AGENT_TYPE_LENGTH = 32
MAX_AGENT_VENDOR_LENGTH = 64
MAX_AGENT_VERSION_LENGTH = 32
MAX_REQUEST_PURPOSE_LENGTH = 64

_AGENT_IDENTIFIER_ALLOWED_RE = re.compile(r"[^a-zA-Z0-9._:@/\-]+")


def _parse_csv_env(env_name: str, default: str = "") -> set[str]:
    raw = os.getenv(env_name, default)
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


AGENT_PAY_TEST_CUSTOMER_IDS = _parse_csv_env("AGENT_PAY_TEST_CUSTOMER_IDS")
AGENT_PAY_TEST_API_KEY_IDS = _parse_csv_env("AGENT_PAY_TEST_API_KEY_IDS")
PAYMENT_RESPONSE_CACHE_CONTROL = "no-store, private"
FREE_CACHEABLE_PRICING_RULE_IDS = {"default_free", "default_free_metered"}


@dataclass
class PaymentExecutionState:
    """
    Payment enforcement facts produced at the endpoint execution seam.

    Enforcement used to run in this middleware's own frame, so the finaliser
    could simply read its locals after `call_next()`.  It now runs inside the
    endpoint wrapper, which is a different frame entirely, so the facts travel
    back on `request.state` instead.  MPP capture/void depends on that handoff:
    without `enforcement_result` the finaliser cannot tell an authorized
    session from one that was never opened, and would either lose a capture or
    leave a dangling authorization.

    The header fields are seeded from the inbound request so the enforcement
    layer's "use the verified value, else keep the presented one" fallbacks
    survive the move unchanged.
    """

    payment_reference: str | None = None
    payment_network: str | None = None
    payment_token: str | None = None
    payment_amount: str | None = None
    payment_channel_id: str | None = None

    validation_valid: bool = True
    validation_error: str | None = None
    validation_detail: str | None = None

    enforcement_result: object | None = None

    # True only where money actually moved: x402 settled, or MPP captured.
    # `collected_amount_usd` is what that collection actually took, and is the
    # sole source of `billed_amount_usd`.  Price lookup never supplies it.
    collected: bool = False
    collected_amount_usd: Decimal | None = None

    # Recorded when the gate itself rejects the request, so the finaliser
    # reports that rejection rather than re-deriving it from a 402 it did not
    # produce.
    rejected: bool = False
    accepted_methods: str | None = None
    event_error_code: str | None = None
    event_notes: str | None = None
    econ_payment_fields: dict | None = None


class DeferredPaymentGate:
    """
    One-shot payment enforcement, invoked at the endpoint execution seam.

    The endpoint wrapper calls this once per request, but one-shot behaviour is
    guaranteed by construction rather than by convention: a second call returns
    the first call's answer without verifying again, settling again, or opening
    a second MPP authorization.

    The outcome is terminal in both directions.  A raising enforcement attempt
    caches its exception and re-raises it on every later call, so a failed
    attempt can never decay into the `None` that means "proceed" — which is
    what a bare `_invoked = True` before the call would have produced.
    """

    __slots__ = ("_enforce", "_invoked", "_response", "_exception")

    def __init__(self, enforce):
        self._enforce = enforce
        self._invoked = False
        self._response = None
        self._exception = None

    @property
    def invoked(self) -> bool:
        return self._invoked

    @property
    def failed(self) -> bool:
        """True when the single enforcement attempt raised."""
        return self._exception is not None

    def __call__(self):
        if self._invoked:
            if self._exception is not None:
                raise self._exception
            return self._response

        self._invoked = True
        try:
            self._response = self._enforce()
        except BaseException as exc:
            self._exception = exc
            raise
        return self._response


def validate_payment_headers(request: Request):
    required_headers = [
        "x-stocktrends-payment-amount",
        "x-stocktrends-payment-network",
        "x-stocktrends-payment-reference",
    ]

    missing = [h for h in required_headers if h not in request.headers]

    if missing:
        return False, "missing_payment_headers", f"Missing required payment headers: {', '.join(missing)}"

    return True, None, None


def get_endpoint_family(path: str) -> str | None:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "v1":
        return parts[1]
    return None


def get_client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    return request.client.host if request.client else None


def get_response_size_bytes(response) -> int | None:
    if response is None:
        return None

    content_length = response.headers.get("content-length")
    if not content_length:
        return None

    try:
        return int(content_length)
    except (TypeError, ValueError):
        return None


def get_accepted_payment_methods(
    path: str,
    pricing_rule_id: str | None,
    *,
    method: str | None = None,
    enforced_payment_method: str | None = None,
) -> str:
    return get_accepted_payment_methods_for_path(
        path,
        pricing_rule_id,
        method=method,
        enforced_payment_method=enforced_payment_method,
    )


def resolve_payment_rail(
    decision,
    *,
    payment_method_header: str | None = None,
) -> str:
    normalized_method = (payment_method_header or decision.econ_payment_method or decision.log_payment_method or "").strip().lower()

    if normalized_method == "subscription":
        return "subscription"

    if normalized_method == "x402":
        return "x402"

    if normalized_method == "mpp":
        return "mpp"

    if decision.econ_payment_required:
        return "none"

    if decision.log_pricing_rule_id == "default_subscription":
        return "subscription"

    return "none"


def apply_pricing_headers(response, pricing_rule_id: str | None, payment_required: bool, accepted_methods: str):
    if pricing_rule_id:
        response.headers["X-StockTrends-Pricing-Rule"] = pricing_rule_id

    response.headers["X-StockTrends-Payment-Required"] = "true" if payment_required else "false"
    response.headers["X-StockTrends-Accepted-Payment-Methods"] = accepted_methods
    if payment_required:
        apply_payment_cache_headers(response)


def apply_payment_cache_headers(response) -> None:
    response.headers["Cache-Control"] = PAYMENT_RESPONSE_CACHE_CONTROL


def should_no_store_protected_paid_response(decision) -> bool:
    pricing_rule_id = decision.econ_pricing_rule_id or decision.log_pricing_rule_id
    return bool(
        decision.access_granted
        and decision.is_metered
        and pricing_rule_id
        and pricing_rule_id not in FREE_CACHEABLE_PRICING_RULE_IDS
    )


def _apply_quota_headers(response, request: Request, decision) -> None:
    """
    Inject subscription quota headers onto metered subscription responses.

    Conditions for injection:
    - is_metered = 1  (not a free/non-metered path)
    - access_granted = True  (denied responses never receive quota headers)
    - econ_payment_required = 0  (subscription callers only; x402 has no quota semantics)
    - request.state.quota_limit is not None  (monthly_quota fetched from api_plans)

    Phase 1 (this implementation):
    - X-StockTrends-Quota-Limit: monthly_quota from api_plans (available now)
    - X-StockTrends-Quota-Period: "monthly" (static)

    Deferred to Phase 2 (requires current_period_start confirmed in stocktrends-api-control):
    - X-StockTrends-Quota-Remaining  (requires usage COUNT query against api_request_logs + caching)
    - X-StockTrends-Quota-Reset      (requires current billing period boundary datetime)
    """
    if not (
        decision.is_metered
        and decision.access_granted
        and decision.econ_payment_required == 0
    ):
        return

    quota_limit = getattr(request.state, "quota_limit", None)
    if quota_limit is None:
        # monthly_quota not available on this request (e.g. agent-pay, free-metered).
        # TODO Phase 2: wire usage COUNT query once current_period_start is confirmed.
        return

    response.headers["X-StockTrends-Quota-Limit"] = str(quota_limit)
    response.headers["X-StockTrends-Quota-Period"] = "monthly"
    # X-StockTrends-Quota-Remaining: deferred — requires COUNT(is_billable=1)
    #   per subscription_id in current billing period, with in-process TTL cache.
    # X-StockTrends-Quota-Reset: deferred — requires api_subscriptions.current_period_start.


def should_log_economics(decision) -> bool:
    return bool(decision.econ_pricing_rule_id)


def availability_gate_decision(error_code: str | None) -> PricingDecision:
    return PricingDecision(
        is_metered=0,
        access_granted=False,
        deny_reason=error_code,
        log_pricing_rule_id="default_free",
        log_payment_method="none",
        econ_pricing_rule_id=None,
        econ_payment_required=0,
        econ_payment_status=None,
        econ_payment_method=None,
    )


def normalize_workflow_type(auth_mode: str | None, agent_identifier: str | None) -> str:
    if agent_identifier:
        return "agent"
    if auth_mode in ("api_key", "free_metered"):
        return "human"
    if auth_mode == "internal_automation":
        return "internal_automation"
    return "unknown"


def is_billable_request(decision) -> int:
    # A request is billable (i.e. counts against subscription quota OR is
    # directly payable via an agent-pay rail) when it is metered AND access
    # was granted.  Denied requests and free/free-metered paths are never
    # billable.  This replaces the old rule-name allowlist, which produced
    # false-positives for denied calls and false-negatives for agent-pay
    # calls carrying a specific endpoint pricing_rule_id.
    if not decision.is_metered or not decision.access_granted:
        return 0
    if decision.log_pricing_rule_id in {"default_free", "default_free_metered"}:
        return 0
    return 1


def safe_decimal(value, default: str = "0"):
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def get_active_pricing_rule(rule_name: str | None) -> dict | None:
    if not rule_name:
        return None

    try:
        engine = get_metering_engine()
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        id,
                        rule_name,
                        endpoint_pattern,
                        endpoint_family,
                        api_version,
                        access_type,
                        cost_per_request,
                        cost_unit,
                        free_tier_limit,
                        hard_limit,
                        requires_subscription,
                        requires_payment,
                        is_active
                    FROM api_pricing_rules
                    WHERE rule_name = :rule_name
                      AND is_active = 1
                    LIMIT 1
                    """
                ),
                {"rule_name": rule_name},
            ).mappings().first()

            return dict(row) if row else None

    except Exception as e:
        logger.error("Pricing rule lookup failed for %s: %s", rule_name, e, exc_info=True)
        return None


def resolve_economic_amounts(rule_name: str | None) -> tuple[Decimal, Decimal]:
    """
    Resolve what the priced operation is worth, from the pricing rule alone.

    Returns `(unit_price_usd, stc_cost)`.

    There is deliberately no billed amount here.  `billed_amount_usd` records
    what a payment rail actually collected, which price lookup cannot know: it
    is an outcome of enforcement, decided at the gate, not a value read from a
    catalogue.  Returning one from here is what previously let a list price be
    logged as a collected amount on requests that never paid.
    """
    rule = get_active_pricing_rule(rule_name)
    if not rule:
        return Decimal("0"), Decimal("0")

    unit_price_usd = safe_decimal(rule.get("cost_per_request"), "0")
    stc_cost = unit_price_usd

    return unit_price_usd, stc_cost


def x402_settled_amount_usd(
    payment_amount_native,
    unit_price_usd: Decimal,
) -> Decimal:
    """
    USD actually settled for a successful x402 payment.

    The native amount is the atomic token value the artifact authorized and the
    facilitator settled, so it converts with the same token-decimal semantics
    used for `payment_amount_usd` elsewhere in this module.

    Three cases, stated explicitly rather than left to a coercion helper:

      None                -> the quoted unit price
      Decimal             -> converted from atomic units
      anything else       -> the quoted unit price

    Enforcement types this value `Decimal | None`, so the third case is a
    contract violation rather than an expected input.  It falls back to the
    quoted price for the same reason the `None` case does: settlement succeeded,
    so the quoted requirement was satisfied, and the quote is the best-supported
    figure available.  Routing it through `safe_decimal` instead would coerce an
    unreadable value to its "0" default and record a settled payment as having
    collected nothing — understating revenue on a request that did move money.
    Never a larger list price, and never the STC cost.
    """
    if payment_amount_native is None:
        return unit_price_usd

    if not isinstance(payment_amount_native, Decimal):
        logger.warning(
            "x402 settled amount was %s, expected Decimal or None; falling back "
            "to the quoted unit price %s",
            type(payment_amount_native).__name__,
            unit_price_usd,
        )
        return unit_price_usd

    try:
        return payment_amount_native / Decimal(10 ** X402_DEFAULT_TOKEN_DECIMALS)
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
        return unit_price_usd


def build_econ_payment_fields(
    payment_required: int,
    payment_status: str,
    payment_method_header: str | None,
    payment_network_header: str | None,
    payment_token_header: str | None,
    payment_amount_header: str | None,
    payment_reference_header: str | None,
    decision,
) -> dict:
    if not payment_required:
        return {
            "payment_status": "not_required",
            "payment_method": decision.econ_payment_method,
            "payment_network": None,
            "payment_token": None,
            "payment_amount_native": None,
            "payment_amount_usd": None,
            "payment_reference": None,
        }

    amount_native = None
    if payment_amount_header:
        try:
            amount_native = float(payment_amount_header)
        except (TypeError, ValueError):
            amount_native = None

    payment_amount_usd = None
    if amount_native is not None and is_x402_payment_method(payment_method_header):
        payment_amount_usd = Decimal(str(amount_native)) / Decimal(10 ** X402_DEFAULT_TOKEN_DECIMALS)

    return {
        "payment_status": payment_status,
        "payment_method": payment_method_header or decision.econ_payment_method,
        "payment_network": payment_network_header,
        "payment_token": payment_token_header,
        "payment_amount_native": amount_native,
        "payment_amount_usd": payment_amount_usd,
        "payment_reference": payment_reference_header,
    }


def challenge_econ_payment_fields(
    *,
    payment_method: str | None,
    payment_network: str | None,
    payment_token: str | None,
) -> dict:
    """
    The economics shape of an issued challenge: quoted, pending, uncollected.

    A challenge is not a settlement, and this is where that distinction becomes
    an accounting fact.  `pending` is the status for a request an agent may
    still pay for — deliberately not `presented` (which the billing runbook
    counts as usage), not `rejected` (which is terminal), and not `settled`.
    No payment reference and no amount are recorded, because none exists: the
    absence of an amount here is what keeps `collected_amount_usd` and
    `billed_amount_usd` at zero for every challenge.

    Shared by the deferred gate's challenge branch and the pre-input challenge
    path so that one protocol event cannot be reported two different ways.
    """
    return {
        "payment_status": "pending",
        "payment_method": payment_method,
        "payment_network": payment_network,
        "payment_token": payment_token,
        "payment_amount_native": None,
        "payment_amount_usd": None,
        "payment_reference": None,
    }


def _clean_header_value(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


def normalize_agent_identifier(agent_id_header: str | None, agent_vendor_header: str | None) -> str | None:
    raw = _clean_header_value(agent_id_header, MAX_AGENT_IDENTIFIER_LENGTH)
    if raw:
        normalized = raw.lower()
        normalized = _AGENT_IDENTIFIER_ALLOWED_RE.sub("-", normalized)
        normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
        if normalized:
            return normalized[:MAX_AGENT_IDENTIFIER_LENGTH]

    vendor = _clean_header_value(agent_vendor_header, MAX_AGENT_VENDOR_LENGTH)
    if vendor:
        normalized_vendor = vendor.lower()
        normalized_vendor = _AGENT_IDENTIFIER_ALLOWED_RE.sub("-", normalized_vendor)
        normalized_vendor = re.sub(r"-{2,}", "-", normalized_vendor).strip("-")
        if normalized_vendor:
            fallback = f"vendor:{normalized_vendor}"
            return fallback[:MAX_AGENT_IDENTIFIER_LENGTH]

    return None


def normalize_agent_type(value: str | None) -> str | None:
    return _clean_header_value(value, MAX_AGENT_TYPE_LENGTH)


def normalize_agent_vendor(value: str | None) -> str | None:
    cleaned = _clean_header_value(value, MAX_AGENT_VENDOR_LENGTH)
    return cleaned.lower() if cleaned else None


def normalize_agent_version(value: str | None) -> str | None:
    return _clean_header_value(value, MAX_AGENT_VERSION_LENGTH)


def normalize_request_purpose(value: str | None) -> str | None:
    return _clean_header_value(value, MAX_REQUEST_PURPOSE_LENGTH)


def lookup_agent_record(customer_id: str | None, agent_identifier: str | None) -> dict | None:
    if not customer_id or not agent_identifier:
        return None

    try:
        engine = get_metering_engine()
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        id,
                        customer_id,
                        agent_identifier,
                        agent_type,
                        agent_vendor,
                        display_name,
                        status,
                        created_at,
                        updated_at
                    FROM api_agents
                    WHERE customer_id = :customer_id
                      AND agent_identifier = :agent_identifier
                    LIMIT 1
                    """
                ),
                {
                    "customer_id": customer_id,
                    "agent_identifier": agent_identifier,
                },
            ).mappings().first()

            return dict(row) if row else None

    except Exception as e:
        logger.error("Agent lookup failed: %s", e, exc_info=True)
        return None


def ensure_agent_record(
    customer_id: str | None,
    agent_identifier: str | None,
    agent_type_header: str | None,
    agent_vendor_header: str | None,
) -> tuple[dict | None, bool]:
    if not customer_id or not agent_identifier:
        return None, False

    existing = lookup_agent_record(customer_id, agent_identifier)

    if existing:
        try:
            display_name = existing.get("display_name") or agent_identifier
            needs_refresh = (
                existing.get("agent_type") != agent_type_header
                or existing.get("agent_vendor") != agent_vendor_header
                or existing.get("display_name") != display_name
            )

            if needs_refresh:
                engine = get_metering_engine()
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                            UPDATE api_agents
                            SET
                                agent_type = :agent_type,
                                agent_vendor = :agent_vendor,
                                display_name = :display_name,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = :id
                            """
                        ),
                        {
                            "id": existing["id"],
                            "agent_type": agent_type_header,
                            "agent_vendor": agent_vendor_header,
                            "display_name": display_name,
                        },
                    )
            else:
                engine = get_metering_engine()
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                            UPDATE api_agents
                            SET updated_at = CURRENT_TIMESTAMP
                            WHERE id = :id
                            """
                        ),
                        {"id": existing["id"]},
                    )

            refreshed = lookup_agent_record(customer_id, agent_identifier)
            return refreshed or existing, False

        except Exception as e:
            logger.error("Agent refresh failed: %s", e, exc_info=True)
            return existing, False

    try:
        engine = get_metering_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT IGNORE INTO api_agents (
                        id,
                        customer_id,
                        agent_identifier,
                        agent_type,
                        agent_vendor,
                        display_name,
                        status
                    ) VALUES (
                        :id,
                        :customer_id,
                        :agent_identifier,
                        :agent_type,
                        :agent_vendor,
                        :display_name,
                        'active'
                    )
                    """
                ),
                {
                    "id": str(uuid4()),
                    "customer_id": customer_id,
                    "agent_identifier": agent_identifier,
                    "agent_type": agent_type_header,
                    "agent_vendor": agent_vendor_header,
                    "display_name": agent_identifier,
                },
            )
    except Exception as e:
        logger.error("Agent auto-registration failed: %s", e, exc_info=True)
        return lookup_agent_record(customer_id, agent_identifier), False

    created = lookup_agent_record(customer_id, agent_identifier)
    return created, bool(created)


def lookup_external_agent_record(agent_identifier: str | None) -> dict | None:
    if not agent_identifier:
        return None

    try:
        engine = get_metering_engine()
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        id,
                        agent_identifier,
                        agent_type,
                        agent_vendor,
                        display_name,
                        status,
                        created_at,
                        updated_at
                    FROM api_external_agents
                    WHERE agent_identifier = :agent_identifier
                    LIMIT 1
                    """
                ),
                {"agent_identifier": agent_identifier},
            ).mappings().first()

            return dict(row) if row else None

    except Exception as e:
        logger.error("External agent lookup failed: %s", e, exc_info=True)
        return None


def ensure_external_agent_record(
    agent_identifier: str | None,
    agent_type_header: str | None,
    agent_vendor_header: str | None,
) -> tuple[dict | None, bool]:
    if not agent_identifier:
        return None, False

    existing = lookup_external_agent_record(agent_identifier)

    if existing:
        try:
            display_name = existing.get("display_name") or agent_identifier
            needs_refresh = (
                existing.get("agent_type") != agent_type_header
                or existing.get("agent_vendor") != agent_vendor_header
                or existing.get("display_name") != display_name
            )

            engine = get_metering_engine()
            with engine.begin() as conn:
                if needs_refresh:
                    conn.execute(
                        text(
                            """
                            UPDATE api_external_agents
                            SET
                                agent_type = :agent_type,
                                agent_vendor = :agent_vendor,
                                display_name = :display_name,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = :id
                            """
                        ),
                        {
                            "id": existing["id"],
                            "agent_type": agent_type_header,
                            "agent_vendor": agent_vendor_header,
                            "display_name": display_name,
                        },
                    )
                else:
                    conn.execute(
                        text(
                            """
                            UPDATE api_external_agents
                            SET updated_at = CURRENT_TIMESTAMP
                            WHERE id = :id
                            """
                        ),
                        {"id": existing["id"]},
                    )

            refreshed = lookup_external_agent_record(agent_identifier)
            return refreshed or existing, False

        except Exception as e:
            logger.error("External agent refresh failed: %s", e, exc_info=True)
            return existing, False

    try:
        engine = get_metering_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT IGNORE INTO api_external_agents (
                        id,
                        agent_identifier,
                        agent_type,
                        agent_vendor,
                        display_name,
                        status
                    ) VALUES (
                        :id,
                        :agent_identifier,
                        :agent_type,
                        :agent_vendor,
                        :display_name,
                        'active'
                    )
                    """
                ),
                {
                    "id": str(uuid4()),
                    "agent_identifier": agent_identifier,
                    "agent_type": agent_type_header,
                    "agent_vendor": agent_vendor_header,
                    "display_name": agent_identifier,
                },
            )
    except Exception as e:
        logger.error("External agent auto-registration failed: %s", e, exc_info=True)
        return lookup_external_agent_record(agent_identifier), False

    created = lookup_external_agent_record(agent_identifier)
    return created, bool(created)


def is_payment_reference_used(payment_reference: str) -> bool:
    if not payment_reference:
        return False

    try:
        engine = get_metering_engine()
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM api_request_economics
                    WHERE payment_reference = :payment_reference
                      AND payment_status IN ('authorized', 'settled')
                    LIMIT 1
                    """
                ),
                {"payment_reference": payment_reference},
            ).first()

            return row is not None

    except Exception as e:
        logger.error("Payment replay check failed: %s", e, exc_info=True)
        return False


def _path_matches_enforcement_scope(path: str, method: str | None) -> bool:
    return is_agent_pay_enforcement_path(path, method)


def _caller_matches_test_allowlist(request: Request) -> bool:
    customer_id = getattr(request.state, "customer_id", None)
    api_key_id = getattr(request.state, "api_key_id", None)

    has_customer_allowlist = bool(AGENT_PAY_TEST_CUSTOMER_IDS)
    has_api_key_allowlist = bool(AGENT_PAY_TEST_API_KEY_IDS)

    if not has_customer_allowlist and not has_api_key_allowlist:
        return True

    if has_customer_allowlist and customer_id and customer_id in AGENT_PAY_TEST_CUSTOMER_IDS:
        return True

    if has_api_key_allowlist and api_key_id and api_key_id in AGENT_PAY_TEST_API_KEY_IDS:
        return True

    return False


def should_enforce_agent_pay_for_request(request: Request, path: str, method: str | None, decision) -> bool:
    if not ENABLE_AGENT_PAY or not ENFORCE_AGENT_PAY:
        return False

    if decision.econ_payment_required != 1:
        return False

    if not _path_matches_enforcement_scope(path, method):
        return False

    if not _caller_matches_test_allowlist(request):
        return False

    return True


def build_request_event(
    *,
    request_id: str | None,
    environment: str,
    api_key_id: str | None,
    customer_id: str | None,
    subscription_id: str | None,
    plan_code: str | None,
    actor_type: str | None,
    workflow_type: str,
    agent_identifier: str | None,
    agent_registry_id: str | None,
    path: str,
    method: str,
    query_string: str,
    request: Request,
    status_code: int,
    success: int,
    latency_ms: int,
    response,
    decision,
    payment_rail: str,
    payment_method: str | None,
    payment_network: str | None = None,
    payment_token: str | None = None,
    error_code: str | None,
    notes: str | None,
) -> dict:
    return {
        "event_time_utc": datetime.now(timezone.utc),
        "request_id": request_id,
        "environment": environment,
        "api_key_id": api_key_id,
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "plan_code": plan_code,
        "actor_type": actor_type or "unknown",
        "workflow_type": workflow_type,
        "agent_identifier": agent_identifier,
        "agent_id": agent_registry_id,
        "endpoint_path": path,
        "route_template": None,
        "endpoint_family": get_endpoint_family(path),
        "http_method": method,
        "query_string": query_string,
        "symbol": request.query_params.get("symbol"),
        "exchange": request.query_params.get("exchange"),
        "symbol_exchange": request.query_params.get("symbol_exchange"),
        "status_code": status_code,
        "success": success,
        "latency_ms": latency_ms,
        "response_size_bytes": get_response_size_bytes(response),
        "client_ip": get_client_ip(request),
        "user_agent": request.headers.get("user-agent"),
        "referer": request.headers.get("referer"),
        "is_metered": decision.is_metered,
        "is_billable": is_billable_request(decision),
        "payment_rail": payment_rail,
        "payment_method": payment_method,
        "payment_network": payment_network,
        "payment_token": payment_token,
        "pricing_rule_id": decision.log_pricing_rule_id,
        "error_code": error_code,
        "notes": notes[:255] if notes else None,
    }


def build_request_econ(
    *,
    request_id: str | None,
    customer_id: str | None,
    api_key_id: str | None,
    pricing_rule_id: str | None,
    unit_price_usd: Decimal,
    billed_amount_usd: Decimal,
    stc_cost: Decimal,
    payment_required: int,
    payment_rail: str,
    payment_channel_id: str | None,
    econ_payment_fields: dict,
    session_id_header: str | None,
    agent_registry_id: str | None,
    agent_type: str | None,
    agent_vendor: str | None,
    agent_version: str | None,
    request_purpose: str | None,
) -> dict:
    return {
        "request_id": request_id,
        "customer_id": customer_id,
        "api_key_id": api_key_id,
        "pricing_rule_id": pricing_rule_id,
        "unit_price_usd": unit_price_usd,
        "billed_amount_usd": billed_amount_usd,
        "stc_cost": stc_cost,
        "payment_required": payment_required,
        "payment_rail": payment_rail,
        **econ_payment_fields,
        "session_id": session_id_header,
        "payment_channel_id": payment_channel_id,
        "agent_id": agent_registry_id,
        "agent_type": agent_type,
        "agent_vendor": agent_vendor,
        "agent_version": agent_version,
        "request_purpose": request_purpose,
    }


class MeteringMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()

        request_id = getattr(request.state, "request_id", None)
        path = request.url.path
        method = request.method
        query_string = str(request.url.query)

        payment_method_header = request.headers.get("x-stocktrends-payment-method")
        payment_network_header = request.headers.get("x-stocktrends-payment-network")
        payment_token_header = request.headers.get("x-stocktrends-payment-token")
        payment_reference_header = request.headers.get("x-stocktrends-payment-reference")
        payment_amount_header = request.headers.get("x-stocktrends-payment-amount")

        agent_id_header = request.headers.get("x-stocktrends-agent-id")
        agent_type_header = normalize_agent_type(request.headers.get("x-stocktrends-agent-type"))
        agent_vendor_header = normalize_agent_vendor(request.headers.get("x-stocktrends-agent-vendor"))
        agent_version_header = normalize_agent_version(request.headers.get("x-stocktrends-agent-version"))
        request_purpose_header = normalize_request_purpose(request.headers.get("x-stocktrends-request-purpose"))
        session_id_header = request.headers.get("x-stocktrends-session-id")

        auth_mode = getattr(request.state, "auth_mode", "unknown")
        has_paid_auth = auth_mode == "api_key"
        plan_code = getattr(request.state, "plan_code", None)
        customer_id = getattr(request.state, "customer_id", None)
        api_key_id = getattr(request.state, "api_key_id", None)
        subscription_id = getattr(request.state, "subscription_id", None)
        actor_type = getattr(request.state, "actor_type", "unknown")

        agent_identifier = normalize_agent_identifier(agent_id_header, agent_vendor_header)

        availability = getattr(request.state, "intelligence_artifact_availability", None)
        if availability is not None:
            error_code = getattr(availability, "error_code", None)
            decision = availability_gate_decision(error_code)
            response = JSONResponse(
                status_code=getattr(availability, "status_code", None) or 503,
                content={
                    "detail": intelligence_artifact_availability_error_detail(
                        availability,
                        request_id=request_id,
                    ),
                },
            )
            apply_pricing_headers(
                response,
                pricing_rule_id=None,
                payment_required=False,
                accepted_methods="none",
            )

            latency_ms = int((time.time() - start_time) * 1000)
            event = build_request_event(
                request_id=request_id,
                environment="production",
                api_key_id=api_key_id,
                customer_id=customer_id,
                subscription_id=subscription_id,
                plan_code=plan_code,
                actor_type=actor_type,
                workflow_type=normalize_workflow_type(auth_mode, agent_identifier),
                agent_identifier=agent_identifier,
                agent_registry_id=None,
                path=path,
                method=method,
                query_string=query_string,
                request=request,
                status_code=response.status_code,
                success=0,
                latency_ms=latency_ms,
                response=response,
                decision=decision,
                payment_rail="none",
                payment_method="none",
                error_code=error_code,
                notes=getattr(availability, "message", None),
            )

            try:
                log_api_request_event(event)
            except Exception as e:
                logger.error("Metering request-log insert failed: %s", e, exc_info=True)

            return response

        if customer_id:
            agent_record, agent_auto_registered = ensure_agent_record(
                customer_id=customer_id,
                agent_identifier=agent_identifier,
                agent_type_header=agent_type_header,
                agent_vendor_header=agent_vendor_header,
            )
        else:
            agent_record, agent_auto_registered = ensure_external_agent_record(
                agent_identifier=agent_identifier,
                agent_type_header=agent_type_header,
                agent_vendor_header=agent_vendor_header,
            )

        agent_registered = bool(agent_record)
        agent_registry_id = agent_record["id"] if agent_record else None
        agent_registry_status = agent_record["status"] if agent_record else None

        request.state.agent_identifier_normalized = agent_identifier
        request.state.agent_registered = agent_registered
        request.state.agent_registry_id = agent_registry_id
        request.state.agent_registry_status = agent_registry_status
        request.state.agent_auto_registered = agent_auto_registered
        request.state.x402_payment_response = None

        decision = classify_request(
            path=path,
            has_paid_auth=has_paid_auth,
            payment_method_header=payment_method_header,
            plan_code=plan_code,
            agent_identifier=agent_identifier,
            method=method,
        )

        request.state.pricing_rule_id = decision.log_pricing_rule_id
        request.state.is_metered = decision.is_metered
        request.state.payment_required = decision.econ_payment_required
        request.state.payment_method_resolved = decision.econ_payment_method
        request.state.econ_pricing_rule_id = decision.econ_pricing_rule_id
        request.state.econ_payment_status = decision.econ_payment_status

        economic_rule_name = decision.econ_pricing_rule_id or decision.log_pricing_rule_id
        unit_price_usd, stc_cost = resolve_economic_amounts(economic_rule_name)
        # The three economics fields answer three different questions and must
        # never be conflated:
        #
        #   unit_price_usd     what the operation is quoted at, collected or not
        #   stc_cost           the Stock Trends analytical/economic value measure
        #   billed_amount_usd  what a payment rail actually collected
        #
        # billed_amount_usd has no pre-payment source at all: price resolution
        # does not return one.  It starts at zero and is set from the collection
        # the gate confirms — the settled x402 amount, or the captured MPP
        # amount.  Every other state (challenge, malformed artifact, replay,
        # verification or settlement failure, framework rejection before the
        # gate, route miss, subscription quota) leaves it at zero, so a row
        # never implies a charge that was not taken.
        billed_amount_usd = Decimal("0")
        workflow_type = normalize_workflow_type(auth_mode, agent_identifier)
        resolved_payment_method = payment_method_header or decision.log_payment_method
        effective_payment_method = (
            payment_method_header
            or decision.econ_payment_method
            or decision.log_payment_method
        )
        # Standard x402 clients may send only X-Payment/payment-signature; rail
        # resolution must still yield x402 so accounting never depends on our private header.
        payment_rail = resolve_payment_rail(
            decision,
            payment_method_header=payment_method_header,
        )

        request.state.unit_price_usd = unit_price_usd
        request.state.billed_amount_usd = billed_amount_usd
        request.state.payment_rail = payment_rail
        request.state.payment_channel_id = None

        if agent_identifier and agent_registered and agent_registry_status == "disabled":
            response = JSONResponse(
                status_code=403,
                content={
                    "error": "agent_disabled",
                    "detail": "This agent is disabled for this customer",
                    "request_id": request_id,
                },
            )

            apply_pricing_headers(
                response,
                pricing_rule_id=decision.log_pricing_rule_id,
                payment_required=bool(decision.econ_payment_required),
                accepted_methods=get_accepted_payment_methods(path, decision.log_pricing_rule_id, method=method),
            )

            latency_ms = int((time.time() - start_time) * 1000)

            event = build_request_event(
                request_id=request_id,
                environment="production",
                api_key_id=api_key_id,
                customer_id=customer_id,
                subscription_id=subscription_id,
                plan_code=plan_code,
                actor_type=actor_type,
                workflow_type=workflow_type,
                agent_identifier=agent_identifier,
                agent_registry_id=agent_registry_id,
                path=path,
                method=method,
                query_string=query_string,
                request=request,
                status_code=403,
                success=0,
                latency_ms=latency_ms,
                response=response,
                decision=decision,
                payment_rail=payment_rail,
                payment_method=resolved_payment_method,
                error_code="agent_disabled",
                notes="This agent is disabled for this customer",
            )

            try:
                log_api_request_event(event)
            except Exception as e:
                logger.error("Metering request-log insert failed: %s", e, exc_info=True)

            if should_log_economics(decision):
                econ_payment_fields = build_econ_payment_fields(
                    payment_required=decision.econ_payment_required,
                    payment_status=decision.econ_payment_status or "not_required",
                    payment_method_header=payment_method_header,
                    payment_network_header=payment_network_header,
                    payment_token_header=payment_token_header,
                    payment_amount_header=payment_amount_header,
                    payment_reference_header=payment_reference_header,
                    decision=decision,
                )

                econ = build_request_econ(
                    request_id=request_id,
                    customer_id=customer_id,
                    api_key_id=api_key_id,
                    pricing_rule_id=decision.econ_pricing_rule_id or decision.log_pricing_rule_id,
                    unit_price_usd=unit_price_usd,
                    billed_amount_usd=billed_amount_usd,
                    stc_cost=stc_cost,
                    payment_required=decision.econ_payment_required,
                    payment_rail=payment_rail,
                    payment_channel_id=None,
                    econ_payment_fields=econ_payment_fields,
                    session_id_header=session_id_header,
                    agent_registry_id=agent_registry_id,
                    agent_type=agent_type_header,
                    agent_vendor=agent_vendor_header,
                    agent_version=agent_version_header,
                    request_purpose=request_purpose_header,
                )

                try:
                    log_api_request_economics(econ)
                except Exception as e:
                    logger.error("Metering economics-log insert failed: %s", e, exc_info=True)

            return response

        if not decision.access_granted:
            response = JSONResponse(
                status_code=403,
                content={
                    "error": decision.deny_reason or "access_denied",
                    "detail": "STIM access not permitted for this account",
                    "request_id": request_id,
                },
            )

            apply_pricing_headers(
                response,
                pricing_rule_id=decision.log_pricing_rule_id,
                payment_required=bool(decision.econ_payment_required),
                accepted_methods=get_accepted_payment_methods(path, decision.log_pricing_rule_id, method=method),
            )

            latency_ms = int((time.time() - start_time) * 1000)

            event = build_request_event(
                request_id=request_id,
                environment="production",
                api_key_id=api_key_id,
                customer_id=customer_id,
                subscription_id=subscription_id,
                plan_code=plan_code,
                actor_type=actor_type,
                workflow_type=workflow_type,
                agent_identifier=agent_identifier,
                agent_registry_id=agent_registry_id,
                path=path,
                method=method,
                query_string=query_string,
                request=request,
                status_code=403,
                success=0,
                latency_ms=latency_ms,
                response=response,
                decision=decision,
                payment_rail=payment_rail,
                payment_method=resolved_payment_method,
                error_code=decision.deny_reason or "access_denied",
                notes="STIM access not permitted for this account",
            )

            try:
                log_api_request_event(event)
            except Exception as e:
                logger.error("Metering request-log insert failed: %s", e, exc_info=True)

            if should_log_economics(decision):
                econ_payment_fields = build_econ_payment_fields(
                    payment_required=decision.econ_payment_required,
                    payment_status=decision.econ_payment_status or "not_required",
                    payment_method_header=payment_method_header,
                    payment_network_header=payment_network_header,
                    payment_token_header=payment_token_header,
                    payment_amount_header=payment_amount_header,
                    payment_reference_header=payment_reference_header,
                    decision=decision,
                )

                econ = build_request_econ(
                    request_id=request_id,
                    customer_id=customer_id,
                    api_key_id=api_key_id,
                    pricing_rule_id=decision.econ_pricing_rule_id or decision.log_pricing_rule_id,
                    unit_price_usd=unit_price_usd,
                    billed_amount_usd=billed_amount_usd,
                    stc_cost=stc_cost,
                    payment_required=decision.econ_payment_required,
                    payment_rail=payment_rail,
                    payment_channel_id=None,
                    econ_payment_fields=econ_payment_fields,
                    session_id_header=session_id_header,
                    agent_registry_id=agent_registry_id,
                    agent_type=agent_type_header,
                    agent_vendor=agent_vendor_header,
                    agent_version=agent_version_header,
                    request_purpose=request_purpose_header,
                )

                try:
                    log_api_request_economics(econ)
                except Exception as e:
                    logger.error("Metering economics-log insert failed: %s", e, exc_info=True)

            return response

        normalized_payment_method = (effective_payment_method or "").strip().lower()

        should_validate_agent_pay = (
            ENABLE_AGENT_PAY
            and VALIDATE_AGENT_PAY_HEADERS
            and decision.econ_payment_required == 1
            and normalized_payment_method in {"mpp", "x402"}
        )

        should_enforce_agent_pay = should_enforce_agent_pay_for_request(request, path, method, decision)

        # ------------------------------------------------------------------
        # Deferred payment gate
        #
        # Enforcement deliberately does NOT run here.  Middleware executes
        # before routing, so settling at this point charges for requests
        # FastAPI has not yet accepted: a path matching no route, a body that
        # does not parse, a query value outside its declared constraints.
        # Everything above this line is pre-routing work that moves no money —
        # auth context, pricing classification, rail resolution, pricing
        # metadata.
        #
        # The gate below is published on request.state and invoked by the
        # endpoint wrapper (api/routing.py) at `dependant.call`, which FastAPI
        # reaches only once route matching, body parsing and Pydantic/query
        # validation have all succeeded.
        #
        # Its result is published back on request.state rather than left in
        # this frame: the finaliser runs after call_next() and can no longer
        # assume enforcement happened in its own scope.  MPP capture/void
        # depends on that handoff.
        # ------------------------------------------------------------------
        gate_state = PaymentExecutionState(
            payment_reference=payment_reference_header,
            payment_network=payment_network_header,
            payment_token=payment_token_header,
            payment_amount=payment_amount_header,
        )
        request.state.payment_enforcement = gate_state

        # ------------------------------------------------------------------
        # Challenge issuance, separated from payment settlement
        #
        # This is the PR3 decision point, and it is deliberately NOT the
        # payment gate moved back to middleware.  Two properties keep it safe:
        #
        #   1. It runs only when the request presents no payment authorization
        #      or proof at all, so there is nothing here to verify or settle.
        #   2. Its whole payment action is `issue_x402_challenge`, which calls
        #      the pure challenge builder.  No facilitator, no MPP control
        #      plane, no database, no endpoint.
        #
        # Route and method recognition come from the application's own routers
        # (`decide_early_challenge`), so an unknown path still reaches its 404
        # and a wrong method still reaches its 405.  Eligibility comes from an
        # exact endpoint payment policy, so a paid-looking URL that no policy
        # prices is never challenged.
        #
        # A request that *does* carry payment material skips this entirely and
        # takes the unchanged path: FastAPI structural validation, then
        # request-only semantic validation, then the deferred gate, then the
        # endpoint.  The no-malformed-settlement invariant is untouched.
        # ------------------------------------------------------------------
        challenge_only_response = None

        if (
            should_enforce_agent_pay
            and decision.econ_payment_required == 1
            and payment_rail == EARLY_CHALLENGE_RAIL
            and not is_payment_bearing(request.headers)
        ):
            early_challenge = decide_early_challenge(
                app=request.scope.get("app"),
                scope=request.scope,
                path=path,
                method=method,
                endpoint_policy=get_effective_endpoint_payment_policy(path, method),
            )

            if early_challenge.eligible:
                challenge = issue_x402_challenge(
                    path=path,
                    method=method,
                    amount_usd=unit_price_usd,
                    pricing_rule_id=decision.econ_pricing_rule_id or decision.log_pricing_rule_id,
                    challenge_mode=challenge_mode_from_headers(request.headers),
                )

                challenge_only_response = JSONResponse(
                    status_code=402,
                    content=challenge.body,
                )
                challenge_only_response.headers["PAYMENT-REQUIRED"] = (
                    challenge.payment_required_header
                )

                # Recorded exactly as the gate records its own challenge, so
                # the two are indistinguishable in the logs — because they are
                # the same protocol event.  `collected` stays False and no
                # payment reference is set, so nothing downstream can report
                # this as billed, settled or successful paid execution.
                gate_state.rejected = True
                gate_state.accepted_methods = challenge.accepted_payment_methods
                gate_state.event_error_code = CHALLENGE_ERROR_CODE
                gate_state.event_notes = CHALLENGE_ERROR_DETAIL
                gate_state.payment_network = (
                    challenge.payment_network or payment_network_header
                )
                gate_state.payment_token = (
                    challenge.payment_token or payment_token_header
                )
                gate_state.econ_payment_fields = challenge_econ_payment_fields(
                    payment_method=payment_method_header or decision.econ_payment_method,
                    payment_network=gate_state.payment_network,
                    payment_token=gate_state.payment_token,
                )

        def run_payment_gate():
            """
            Validate and enforce payment for a request FastAPI has accepted.

            Returns the response to send in place of executing the endpoint, or
            None when the caller may proceed.  Records what happened on
            `gate_state` so the finaliser can log it and settle MPP correctly.
            """
            validated_payment_reference = None
            validated_payment_network = None
            validated_payment_token = None
            validated_payment_amount_native = None

            if should_validate_agent_pay:
                if is_x402_payment_method(normalized_payment_method):
                    x402_result = validate_x402_payment(
                        request.headers,
                        required_amount_usd=unit_price_usd,
                    )
                    gate_state.validation_valid = x402_result.valid
                    gate_state.validation_error = x402_result.error_code
                    gate_state.validation_detail = x402_result.error_detail
                    validated_payment_reference = x402_result.payment_reference
                    validated_payment_network = x402_result.payment_network
                    validated_payment_token = x402_result.payment_token
                    validated_payment_amount_native = x402_result.payment_amount_native
                else:
                    (
                        gate_state.validation_valid,
                        gate_state.validation_error,
                        gate_state.validation_detail,
                    ) = validate_payment_headers(request)

            if not (should_enforce_agent_pay and decision.econ_payment_required == 1):
                return None

            local_enforcement_result = None
            if payment_rail in {"x402", "mpp"}:
                local_enforcement_result = enforce_payment_rail(
                    payment_rail=payment_rail,
                    headers=request.headers,
                    path=path,
                    method=method,
                    amount_usd=unit_price_usd,
                    validation_valid=gate_state.validation_valid,
                    validation_error=gate_state.validation_error,
                    validation_detail=gate_state.validation_detail,
                    validated_payment_reference=validated_payment_reference,
                    validated_payment_network=validated_payment_network,
                    validated_payment_token=validated_payment_token,
                    validated_payment_amount_native=validated_payment_amount_native,
                    replay_checker=is_payment_reference_used,
                    pricing_rule_id=economic_rule_name,
                    request_id=request_id,
                )
                gate_state.enforcement_result = local_enforcement_result

            pricing_rule_for_headers = decision.econ_pricing_rule_id or decision.log_pricing_rule_id

            def reject(
                *,
                enforcement,
                content: dict,
                accepted_methods: str,
                event_error_code: str,
                event_notes: str | None,
                econ_payment_fields: dict,
                payment_required_header: str | None = None,
            ):
                """Build the 402 and record what the finaliser must report."""
                rejection = JSONResponse(status_code=402, content=content)
                if payment_required_header is not None:
                    rejection.headers["PAYMENT-REQUIRED"] = payment_required_header

                gate_state.rejected = True
                gate_state.accepted_methods = accepted_methods
                gate_state.event_error_code = event_error_code
                gate_state.event_notes = event_notes
                gate_state.econ_payment_fields = econ_payment_fields

                # A conformant x402 client sends only `X-Payment`, so the
                # network and token are known from the enforcement result rather
                # than from any inbound Stock Trends header.  Carry them onto the
                # state the request-event builder reads, or api_request_logs
                # loses that context on every rejection — the economics row keeps
                # it, the event row would not.
                if enforcement is not None:
                    gate_state.payment_network = (
                        enforcement.payment_network or payment_network_header
                    )
                    gate_state.payment_token = (
                        enforcement.payment_token or payment_token_header
                    )
                return rejection

            if payment_rail == "x402":
                if local_enforcement_result.outcome == "challenge":
                    # Composed by the shared challenge decorator, which resolves
                    # accepted methods from policy so both the header and the
                    # body carry the full endpoint capability list
                    # (subscription,x402,mpp for paid endpoints) rather than the
                    # selected challenge rail only, and injects the schema-only
                    # preview.  The pre-input challenge path uses the same
                    # decorator, so a challenge obtained before validation and
                    # one obtained here describe the same payable contract.
                    challenge = decorate_x402_challenge(
                        path=path,
                        method=method,
                        challenge_body=local_enforcement_result.challenge_body,
                        payment_required_header=local_enforcement_result.payment_required_header,
                        pricing_rule_id=pricing_rule_for_headers,
                        amount_usd=unit_price_usd,
                        payment_network=local_enforcement_result.payment_network,
                        payment_token=local_enforcement_result.payment_token,
                    )

                    return reject(
                        enforcement=local_enforcement_result,
                        content=challenge.body,
                        accepted_methods=challenge.accepted_payment_methods,
                        event_error_code=CHALLENGE_ERROR_CODE,
                        event_notes=CHALLENGE_ERROR_DETAIL,
                        payment_required_header=challenge.payment_required_header,
                        econ_payment_fields=challenge_econ_payment_fields(
                            payment_method=payment_method_header or decision.econ_payment_method,
                            payment_network=challenge.payment_network or payment_network_header,
                            payment_token=challenge.payment_token or payment_token_header,
                        ),
                    )

                x402_rejection_methods = get_accepted_payment_methods(
                    path,
                    pricing_rule_for_headers,
                    method=method,
                    enforced_payment_method="x402",
                )
                x402_amount_native = (
                    float(local_enforcement_result.payment_amount_native)
                    if local_enforcement_result.payment_amount_native is not None
                    else None
                )

                if local_enforcement_result.outcome == "validation_failed":
                    return reject(
                        enforcement=local_enforcement_result,
                        content={
                            "error": local_enforcement_result.error_code,
                            "detail": local_enforcement_result.error_detail,
                            "request_id": request_id,
                        },
                        accepted_methods=x402_rejection_methods,
                        event_error_code=local_enforcement_result.error_code,
                        event_notes=local_enforcement_result.error_detail,
                        econ_payment_fields={
                            "payment_status": "failed_validation",
                            "payment_method": payment_method_header or decision.econ_payment_method,
                            "payment_network": local_enforcement_result.payment_network or payment_network_header,
                            "payment_token": local_enforcement_result.payment_token or payment_token_header,
                            "payment_amount_native": x402_amount_native,
                            "payment_amount_usd": None,
                            "payment_reference": local_enforcement_result.payment_reference,
                        },
                    )

                replay_reference = local_enforcement_result.payment_reference

                if local_enforcement_result.outcome == "replay_detected":
                    return reject(
                        enforcement=local_enforcement_result,
                        content={
                            "error": "replay_detected",
                            "detail": "Payment reference has already been used.",
                            "request_id": request_id,
                        },
                        accepted_methods=x402_rejection_methods,
                        event_error_code=local_enforcement_result.error_code,
                        event_notes=local_enforcement_result.error_detail,
                        econ_payment_fields={
                            "payment_status": "failed_validation",
                            "payment_method": payment_method_header or decision.econ_payment_method,
                            "payment_network": local_enforcement_result.payment_network or payment_network_header,
                            "payment_token": local_enforcement_result.payment_token or payment_token_header,
                            "payment_amount_native": x402_amount_native,
                            "payment_amount_usd": None,
                            "payment_reference": replay_reference,
                        },
                    )

                if local_enforcement_result.outcome == "verification_failed":
                    return reject(
                        enforcement=local_enforcement_result,
                        content={
                            "error": "payment_verification_failed",
                            "detail": local_enforcement_result.error_detail,
                            "request_id": request_id,
                        },
                        accepted_methods=x402_rejection_methods,
                        event_error_code="payment_verification_failed",
                        event_notes=local_enforcement_result.error_detail,
                        econ_payment_fields={
                            "payment_status": "failed_validation",
                            "payment_method": payment_method_header or decision.econ_payment_method,
                            "payment_network": local_enforcement_result.payment_network or payment_network_header,
                            "payment_token": local_enforcement_result.payment_token or payment_token_header,
                            "payment_amount_native": x402_amount_native,
                            "payment_amount_usd": None,
                            "payment_reference": replay_reference,
                        },
                    )

                if local_enforcement_result.outcome == "settlement_failed":
                    return reject(
                        enforcement=local_enforcement_result,
                        content={
                            "error": "payment_settlement_failed",
                            "detail": local_enforcement_result.error_detail,
                            "request_id": request_id,
                        },
                        accepted_methods=x402_rejection_methods,
                        event_error_code="payment_settlement_failed",
                        event_notes=local_enforcement_result.error_detail,
                        econ_payment_fields={
                            "payment_status": "failed",
                            "payment_method": payment_method_header or decision.econ_payment_method,
                            "payment_network": local_enforcement_result.payment_network or payment_network_header,
                            "payment_token": local_enforcement_result.payment_token or payment_token_header,
                            "payment_amount_native": x402_amount_native,
                            "payment_amount_usd": None,
                            "payment_reference": replay_reference,
                        },
                    )

                # Settled.  This is the only x402 path on which money moved, so
                # it is the only one that records a collected amount.
                request.state.x402_payment_response = local_enforcement_result.payment_response
                gate_state.collected = True
                gate_state.collected_amount_usd = x402_settled_amount_usd(
                    local_enforcement_result.payment_amount_native,
                    unit_price_usd,
                )
                gate_state.payment_reference = replay_reference
                gate_state.payment_network = (
                    local_enforcement_result.payment_network or payment_network_header
                )
                gate_state.payment_token = (
                    local_enforcement_result.payment_token or payment_token_header
                )
                if local_enforcement_result.payment_amount_native is not None:
                    gate_state.payment_amount = str(local_enforcement_result.payment_amount_native)
                gate_state.payment_channel_id = local_enforcement_result.payment_channel_id
                request.state.payment_channel_id = gate_state.payment_channel_id

            if payment_rail == "mpp":
                gate_state.payment_reference = (
                    local_enforcement_result.payment_reference or payment_reference_header
                )
                gate_state.payment_network = (
                    local_enforcement_result.payment_network or payment_network_header
                )
                gate_state.payment_token = (
                    local_enforcement_result.payment_token or payment_token_header
                )
                if local_enforcement_result.payment_amount_native is not None:
                    gate_state.payment_amount = str(local_enforcement_result.payment_amount_native)
                gate_state.payment_channel_id = local_enforcement_result.payment_channel_id
                request.state.payment_channel_id = gate_state.payment_channel_id
                if local_enforcement_result.outcome not in {"proceed", "authorized"}:
                    gate_state.validation_valid = False
                    gate_state.validation_error = local_enforcement_result.error_code
                    gate_state.validation_detail = local_enforcement_result.error_detail

            if payment_rail != "x402" and not gate_state.validation_valid:
                return reject(
                    enforcement=local_enforcement_result,
                    content={
                        "error": gate_state.validation_error,
                        "detail": gate_state.validation_detail,
                        "request_id": request_id,
                    },
                    accepted_methods=get_accepted_payment_methods(
                        path,
                        pricing_rule_for_headers,
                        method=method,
                        enforced_payment_method=None,
                    ),
                    event_error_code=gate_state.validation_error,
                    event_notes=gate_state.validation_detail,
                    econ_payment_fields=build_econ_payment_fields(
                        payment_required=1,
                        payment_status="failed_validation",
                        payment_method_header=payment_method_header,
                        payment_network_header=gate_state.payment_network,
                        payment_token_header=gate_state.payment_token,
                        payment_amount_header=gate_state.payment_amount,
                        payment_reference_header=gate_state.payment_reference,
                        decision=decision,
                    ),
                )

            return None

        # No gate is published for a challenge-only request: the endpoint is
        # never reached, so nothing would ever invoke it, and publishing one
        # would make the finaliser read this challenge as a pre-gate rejection
        # (`rejected`) instead of the live `pending` a challenge is.
        payment_gate = None
        if challenge_only_response is None and (
            should_validate_agent_pay or should_enforce_agent_pay
        ):
            payment_gate = DeferredPaymentGate(run_payment_gate)
            setattr(request.state, PAYMENT_GATE_STATE_ATTR, payment_gate)

        response = None
        caught_exception = None

        try:
            # The challenge-only response replaces the downstream call rather
            # than short-circuiting the whole dispatch: the finaliser below owns
            # pricing headers, request-event and economics logging, and a
            # challenge issued here must be recorded exactly like any other.
            if challenge_only_response is not None:
                response = challenge_only_response
            else:
                response = await call_next(request)
        except Exception as exc:
            caught_exception = exc
            raise
        finally:
            # ------------------------------------------------------------------
            # Execution-boundary backstop.
            #
            # A route that reached the surface without the wrapper can execute
            # paid work and return its payload with no gate consulted, no
            # challenge issued and nothing settled.  Startup verification makes
            # that unreachable in this application; this is the second line, for
            # anything that mounts MeteringMiddleware over a surface it did not
            # verify.
            #
            # The breach is read from the matched route rather than inferred
            # from the status code: `scope["route"]` is set only when a route
            # matched, so a route miss (no route) and a rejection on a properly
            # wrapped route are both correctly left alone, while an unwrapped
            # route is caught whether it returned 2xx, 4xx or 5xx.
            #
            # Payment is NOT invoked here.  Enforcement after call_next() would
            # charge for work already done and cannot be undone; the only safe
            # answer is to refuse to deliver the result.
            # ------------------------------------------------------------------
            matched_route = request.scope.get("route")
            boundary_breached = (
                payment_gate is not None
                and not payment_gate.invoked
                and should_enforce_agent_pay
                and decision.econ_payment_required == 1
                and response is not None
                and matched_route is not None
                and not is_payment_wrapped(matched_route)
            )

            # ------------------------------------------------------------------
            # Pre-gate rejection.
            #
            # The complement of the breach above.  A gate was published for this
            # request and the wrapper never called it, but the route surface was
            # intact — so the request was denied on the way in, before payment
            # and before the endpoint.  Route miss, framework validation error,
            # or a registered semantic validator raising: in every case nothing
            # was verified, settled, authorized or executed.
            #
            # This has to be recorded as a terminal non-consumption status.  The
            # MPP branch below otherwise fell through to "presented", which the
            # billing runbook counts as billable usage — so a malformed request
            # that never reached the control plane entered reported request
            # counts and SUM(stc_cost) totals.  No money moved, but the usage
            # reporting was wrong, and it was wrong in the direction of
            # overstating what customers consumed.
            #
            # Read from the gate rather than from the status code: `invoked` is
            # False only when the wrapper never reached enforcement.  A valid
            # unpaid request *does* invoke the gate — the gate is what issues its
            # 402 — so a genuine challenge stays `pending` and is untouched here,
            # as are every facilitator-side failure and every post-payment
            # outcome, all of which run with the gate invoked.
            # ------------------------------------------------------------------
            pre_gate_rejection = (
                payment_gate is not None
                and not payment_gate.invoked
                and not boundary_breached
                and decision.econ_payment_required == 1
                and response is not None
                and response.status_code >= 400
            )

            if boundary_breached:
                logger.critical(
                    "%s — a payment-governed endpoint executed without consulting "
                    "the payment gate; the paid result is being discarded and the "
                    "request failed closed: request_id=%s path=%s method=%s "
                    "rail=%s downstream_status=%s",
                    BOUNDARY_NOT_CONSULTED_ERROR,
                    request_id,
                    path,
                    method,
                    payment_rail,
                    response.status_code,
                )
                response = JSONResponse(
                    status_code=500,
                    content={
                        "error": BOUNDARY_NOT_CONSULTED_ERROR,
                        "detail": (
                            "The request could not be completed: an internal "
                            "payment execution boundary invariant was violated. "
                            "No payment was taken."
                        ),
                        "request_id": request_id,
                    },
                )

            latency_ms = int((time.time() - start_time) * 1000)
            status_code = response.status_code if response is not None else 500
            success = 1 if status_code < 400 else 0

            # Collect what the gate did.  When it never ran — route miss,
            # framework validation rejection, or a request that was never
            # payment-governed — these keep their inbound values and nothing
            # below reports a payment that did not happen.
            enforcement_result = gate_state.enforcement_result
            validation_valid = gate_state.validation_valid
            payment_reference_header = gate_state.payment_reference
            payment_network_header = gate_state.payment_network
            payment_token_header = gate_state.payment_token
            payment_amount_header = gate_state.payment_amount
            payment_channel_id = gate_state.payment_channel_id

            if boundary_breached:
                # Nothing was verified, settled or authorized, so every payment
                # fact stays at its uncollected default and the row records the
                # invariant failure rather than a delivered paid result.
                gate_state.collected = False
                gate_state.collected_amount_usd = None
                gate_state.event_error_code = BOUNDARY_NOT_CONSULTED_ERROR
                gate_state.event_notes = (
                    "endpoint executed without the payment execution boundary; "
                    "paid result discarded"
                )

            pricing_rule_for_headers = decision.econ_pricing_rule_id or decision.log_pricing_rule_id
            payment_required_for_headers = bool(decision.econ_payment_required)
            accepted_methods = get_accepted_payment_methods(path, pricing_rule_for_headers, method=method)

            if response is not None:
                if gate_state.rejected:
                    # The gate rejected the request and already resolved which
                    # methods that particular rejection advertises — a challenge
                    # offers the endpoint's full capability list, an enforcement
                    # failure narrows to the attempted rail.
                    accepted_methods = gate_state.accepted_methods
                elif decision.econ_payment_required and is_x402_payment_method(normalized_payment_method):
                    accepted_methods = get_accepted_payment_methods(
                        path,
                        pricing_rule_for_headers,
                        method=method,
                        enforced_payment_method="x402",
                    )

                apply_pricing_headers(
                    response,
                    pricing_rule_id=pricing_rule_for_headers,
                    payment_required=payment_required_for_headers,
                    accepted_methods=accepted_methods,
                )

                if getattr(request.state, "x402_payment_response", None):
                    response.headers["PAYMENT-RESPONSE"] = encode_payment_response_header(
                        request.state.x402_payment_response,
                    )

                _apply_quota_headers(response, request, decision)
                if should_no_store_protected_paid_response(decision):
                    apply_payment_cache_headers(response)

            event = build_request_event(
                request_id=request_id,
                environment="production",
                api_key_id=api_key_id,
                customer_id=customer_id,
                subscription_id=subscription_id,
                plan_code=plan_code,
                actor_type=actor_type,
                workflow_type=workflow_type,
                agent_identifier=agent_identifier,
                agent_registry_id=agent_registry_id,
                path=path,
                method=method,
                query_string=query_string,
                request=request,
                status_code=status_code,
                success=success,
                latency_ms=latency_ms,
                response=response,
                decision=decision,
                payment_rail=payment_rail,
                payment_method=resolved_payment_method,
                payment_network=payment_network_header,
                payment_token=payment_token_header,
                error_code=(
                    caught_exception.__class__.__name__
                    if caught_exception
                    else gate_state.event_error_code
                ),
                notes=(
                    str(caught_exception)
                    if caught_exception
                    else gate_state.event_notes
                ),
            )

            try:
                log_api_request_event(event)
            except Exception as e:
                logger.error("Metering request-log insert failed: %s", e, exc_info=True)

            # ------------------------------------------------------------------
            # MPP capture — must run after the downstream response is known and
            # before the economics log so payment_status reflects the outcome.
            # Capture only when:
            #   - rail is mpp and enforcement was active
            #   - enforce_mpp_payment returned "authorized" (control-plane ack'd)
            #   - downstream succeeded (status_code < 400, no exception)
            # ------------------------------------------------------------------
            mpp_capture_outcome: str | None = None

            if (
                payment_rail == "mpp"
                and should_enforce_agent_pay
                and enforcement_result is not None
                and enforcement_result.outcome == "authorized"
            ):
                if response is not None and status_code < 400:
                    from payments.mpp_client import capture_mpp_payment
                    # Capture the amount that was authorized.  enforce_mpp_payment
                    # authorizes `unit_price_usd` (the quoted charge), so capture
                    # must use the same figure or the two legs of one payment
                    # disagree.  stc_cost is the analytical measure and never
                    # decides how much is taken; production hid the discrepancy
                    # only because the two values happen to be equal today.
                    _cap = capture_mpp_payment(
                        channel_id=enforcement_result.payment_channel_id,
                        payment_reference=enforcement_result.payment_reference,
                        captured_stc=unit_price_usd,
                        pricing_rule_id=economic_rule_name,
                        request_id=request_id,
                    )
                    if _cap.success:
                        mpp_capture_outcome = "captured"
                        gate_state.collected_amount_usd = unit_price_usd
                    else:
                        mpp_capture_outcome = "capture_failed"
                        logger.error(
                            "mpp capture failed after successful response — "
                            "request_id=%s channel_id=%s payment_reference=%s "
                            "error_code=%s error_detail=%s",
                            request_id,
                            enforcement_result.payment_channel_id,
                            enforcement_result.payment_reference,
                            _cap.error_code,
                            _cap.error_detail,
                        )
                else:
                    # Authorized but downstream failed — void the authorization
                    # so reserved STC is returned to available immediately.
                    # Void is best-effort: errors are logged but must not alter
                    # the original API error response returned to the client.
                    try:
                        from payments.mpp_client import void_mpp_authorization
                        _void = void_mpp_authorization(
                            payment_reference=enforcement_result.payment_reference,
                            request_id=request_id,
                        )
                        if _void.success:
                            mpp_capture_outcome = "voided"
                            logger.info(
                                "mpp void succeeded — "
                                "request_id=%s channel_id=%s payment_reference=%s status_code=%s",
                                request_id,
                                enforcement_result.payment_channel_id,
                                enforcement_result.payment_reference,
                                status_code,
                            )
                        else:
                            mpp_capture_outcome = "void_failed"
                            logger.error(
                                "mpp void failed after authorized downstream failure — "
                                "request_id=%s channel_id=%s payment_reference=%s "
                                "error_code=%s error_detail=%s",
                                request_id,
                                enforcement_result.payment_channel_id,
                                enforcement_result.payment_reference,
                                _void.error_code,
                                _void.error_detail,
                            )
                    except Exception as _void_exc:
                        mpp_capture_outcome = "void_failed"
                        logger.error(
                            "mpp void raised exception — "
                            "request_id=%s payment_reference=%s exc=%s",
                            request_id,
                            enforcement_result.payment_reference,
                            _void_exc,
                            exc_info=True,
                        )

            # MPP capture is the second and last way money moves.  Recorded on
            # the same state the gate wrote to, so the billed amount below has a
            # single source of truth for both rails.
            if mpp_capture_outcome == "captured":
                gate_state.collected = True

            # billed_amount_usd is the amount a rail actually collected, and
            # rises off zero only here.  unit_price_usd and stc_cost keep their
            # own meanings and are unaffected.
            if gate_state.collected and gate_state.collected_amount_usd is not None:
                billed_amount_usd = gate_state.collected_amount_usd

            if should_log_economics(decision):
                payment_status = decision.econ_payment_status

                if decision.econ_payment_required:
                    if pre_gate_rejection:
                        # Denied before the payment gate: rail-independent, and
                        # terminal.  `rejected` is the canonical status for a
                        # request that was denied without payment execution, and
                        # is already excluded from the runbook's billable
                        # (`presented`/`covered`) queries and included in its
                        # failed-payment diagnostics.
                        payment_status = "rejected"
                    elif payment_rail == "x402" or is_x402_payment_method(normalized_payment_method):
                        if gate_state.collected and payment_reference_header:
                            payment_status = "settled"
                        elif payment_reference_header and not validation_valid:
                            payment_status = "failed_validation"
                        else:
                            payment_status = "pending"
                    elif normalized_payment_method == "mpp":
                        if validation_valid and payment_reference_header:
                            if mpp_capture_outcome == "captured":
                                payment_status = "captured"
                            elif mpp_capture_outcome == "capture_failed":
                                payment_status = "capture_failed"
                            elif mpp_capture_outcome == "voided":
                                payment_status = "voided"
                            elif mpp_capture_outcome == "void_failed":
                                # Void was attempted but failed; funds remain reserved.
                                # Logged as void_failed for ops visibility and remediation.
                                payment_status = "void_failed"
                            else:
                                # An authorization was opened and the request ran,
                                # but the finaliser recorded no capture outcome —
                                # enforcement disabled mid-flight, or a control-plane
                                # path that reserved without resolving.  The payment
                                # was presented, so it stays billable usage.
                                #
                                # This branch no longer absorbs pre-gate rejections:
                                # those are classified `rejected` above, before any
                                # rail-specific derivation runs.
                                payment_status = "presented"
                        elif not validation_valid:
                            payment_status = "failed_validation" if should_enforce_agent_pay else "pending"

                if gate_state.rejected:
                    # The gate rejected the request and knows exactly what it
                    # rejected it for.  Re-deriving that from the 402 alone
                    # would lose the distinction between a challenge, a replay,
                    # and a settlement failure.
                    econ_payment_fields = dict(gate_state.econ_payment_fields)
                else:
                    econ_payment_fields = build_econ_payment_fields(
                        payment_required=decision.econ_payment_required,
                        payment_status=payment_status or "pending",
                        payment_method_header=effective_payment_method,
                        payment_network_header=payment_network_header,
                        payment_token_header=payment_token_header,
                        payment_amount_header=payment_amount_header,
                        payment_reference_header=payment_reference_header,
                        decision=decision,
                    )

                econ = build_request_econ(
                    request_id=request_id,
                    customer_id=customer_id,
                    api_key_id=api_key_id,
                    pricing_rule_id=economic_rule_name,
                    unit_price_usd=unit_price_usd,
                    billed_amount_usd=billed_amount_usd,
                    stc_cost=stc_cost,
                    payment_required=decision.econ_payment_required,
                    payment_rail=payment_rail,
                    payment_channel_id=payment_channel_id,
                    econ_payment_fields=econ_payment_fields,
                    session_id_header=session_id_header,
                    agent_registry_id=agent_registry_id,
                    agent_type=agent_type_header,
                    agent_vendor=agent_vendor_header,
                    agent_version=agent_version_header,
                    request_purpose=request_purpose_header,
                )

                try:
                    log_api_request_economics(econ)
                except Exception as e:
                    logger.error("Metering economics-log insert failed: %s", e, exc_info=True)

        # Returned after the finaliser, not from inside the try: the backstop
        # above may have replaced a paid payload that was produced without the
        # execution boundary, and a `return` inside the try would have captured
        # the original response before that substitution.
        return response
