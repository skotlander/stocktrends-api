import json
import os
import re
import time
import logging
from uuid import uuid4
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from fastapi import Request

from metering.logger import (
    log_api_request_event,
    log_api_request_economics,
    get_metering_engine,
)
from payments.enforcement import enforce_payment_rail
from payments.policy_provider import (
    get_accepted_payment_methods_for_path,
    is_agent_pay_enforcement_path,
)
from discovery.preview import get_endpoint_preview
from pricing.classifier import classify_request
from payments.x402 import (
    is_x402_payment_method,
    validate_x402_payment,
    encode_payment_response_header,
    X402_DEFAULT_TOKEN_DECIMALS,
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


def resolve_economic_amounts(rule_name: str | None) -> tuple[Decimal, Decimal, Decimal]:
    rule = get_active_pricing_rule(rule_name)
    if not rule:
        return Decimal("0"), Decimal("0"), Decimal("0")

    unit_price_usd = safe_decimal(rule.get("cost_per_request"), "0")
    access_type = rule.get("access_type")

    if access_type == "paid":
        billed_amount_usd = unit_price_usd
    else:
        billed_amount_usd = Decimal("0")

    stc_cost = unit_price_usd

    return unit_price_usd, billed_amount_usd, stc_cost


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
        unit_price_usd, billed_amount_usd, stc_cost = resolve_economic_amounts(economic_rule_name)
        # Subscription and other quota-based callers are not billed per-request.
        # Zero billed_amount so the economics log never implies a charge that
        # wasn't collected.  stc_cost is preserved — it is the STC-equivalent
        # value used for analytics and future control-plane intelligence.
        if decision.econ_payment_required == 0:
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

        validation_valid = True
        validation_error = None
        validation_detail = None
        validated_payment_reference = None
        validated_payment_network = None
        validated_payment_token = None
        validated_payment_amount_native = None
        payment_channel_id = None

        if should_validate_agent_pay:
            if is_x402_payment_method(normalized_payment_method):
                x402_result = validate_x402_payment(
                    request.headers,
                    required_amount_usd=unit_price_usd,
                )
                validation_valid = x402_result.valid
                validation_error = x402_result.error_code
                validation_detail = x402_result.error_detail
                validated_payment_reference = x402_result.payment_reference
                validated_payment_network = x402_result.payment_network
                validated_payment_token = x402_result.payment_token
                validated_payment_amount_native = x402_result.payment_amount_native
            else:
                validation_valid, validation_error, validation_detail = validate_payment_headers(request)

        enforcement_result = None

        if should_enforce_agent_pay and decision.econ_payment_required == 1:
            if payment_rail in {"x402", "mpp"}:
                enforcement_result = enforce_payment_rail(
                    payment_rail=payment_rail,
                    headers=request.headers,
                    path=path,
                    method=method,
                    amount_usd=unit_price_usd,
                    validation_valid=validation_valid,
                    validation_error=validation_error,
                    validation_detail=validation_detail,
                    validated_payment_reference=validated_payment_reference,
                    validated_payment_network=validated_payment_network,
                    validated_payment_token=validated_payment_token,
                    validated_payment_amount_native=validated_payment_amount_native,
                    replay_checker=is_payment_reference_used,
                    pricing_rule_id=economic_rule_name,
                    request_id=request_id,
                )

            if payment_rail == "x402":
                if enforcement_result.outcome == "challenge":
                    # Resolve accepted methods from policy before building the
                    # response so both the header and the body carry the full
                    # endpoint capability list (subscription,x402,mpp for paid
                    # endpoints) rather than the selected challenge rail only.
                    pricing_rule_for_headers = decision.econ_pricing_rule_id or decision.log_pricing_rule_id
                    accepted_methods_str = get_accepted_payment_methods(
                        path,
                        pricing_rule_for_headers,
                        method=method,
                    )

                    # Shallow-copy so we don't mutate the cached enforcement result.
                    challenge_body = dict(enforcement_result.challenge_body)
                    challenge_body["accepted_payment_methods"] = accepted_methods_str.split(",")
                    # Inject non-sensitive schema preview for known agent endpoints only.
                    # Omitted entirely for unknown paths; never contains live data.
                    _preview = get_endpoint_preview(path)
                    if _preview is not None:
                        challenge_body["stocktrends_preview"] = _preview
                    payment_required_header = enforcement_result.payment_required_header

                    response = JSONResponse(
                        status_code=402,
                        content=challenge_body,
                    )
                    response.headers["PAYMENT-REQUIRED"] = payment_required_header

                    apply_pricing_headers(
                        response,
                        pricing_rule_id=pricing_rule_for_headers,
                        payment_required=True,
                        accepted_methods=accepted_methods_str,
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
                        status_code=402,
                        success=0,
                        latency_ms=latency_ms,
                        response=response,
                        decision=decision,
                        payment_rail=payment_rail,
                        payment_method=resolved_payment_method,
                        payment_network=enforcement_result.payment_network or payment_network_header,
                        payment_token=enforcement_result.payment_token or payment_token_header,
                        error_code="payment_required",
                        notes="x402 payment required",
                    )

                    try:
                        log_api_request_event(event)
                    except Exception as e:
                        logger.error("Metering request-log insert failed: %s", e, exc_info=True)

                    if should_log_economics(decision):
                        econ_payment_fields = {
                            "payment_status": "pending",
                            "payment_method": payment_method_header or decision.econ_payment_method,
                            "payment_network": enforcement_result.payment_network or payment_network_header,
                            "payment_token": enforcement_result.payment_token or payment_token_header,
                            "payment_amount_native": None,
                            "payment_amount_usd": None,
                            "payment_reference": None,
                        }

                        econ = build_request_econ(
                            request_id=request_id,
                            customer_id=customer_id,
                            api_key_id=api_key_id,
                            pricing_rule_id=economic_rule_name,
                            unit_price_usd=unit_price_usd,
                            billed_amount_usd=billed_amount_usd,
                            stc_cost=stc_cost,
                            payment_required=1,
                            payment_rail=payment_rail,
                            payment_channel_id=enforcement_result.payment_channel_id,
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

                if enforcement_result.outcome == "validation_failed":
                    response = JSONResponse(
                        status_code=402,
                        content={
                            "error": enforcement_result.error_code,
                            "detail": enforcement_result.error_detail,
                            "request_id": request_id,
                        },
                    )

                    pricing_rule_for_headers = decision.econ_pricing_rule_id or decision.log_pricing_rule_id
                    apply_pricing_headers(
                        response,
                        pricing_rule_id=pricing_rule_for_headers,
                        payment_required=True,
                        accepted_methods=get_accepted_payment_methods(
                            path,
                            pricing_rule_for_headers,
                            method=method,
                            enforced_payment_method="x402",
                        ),
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
                        status_code=402,
                        success=0,
                        latency_ms=latency_ms,
                        response=response,
                        decision=decision,
                        payment_rail=payment_rail,
                        payment_method=resolved_payment_method,
                        payment_network=enforcement_result.payment_network or payment_network_header,
                        payment_token=enforcement_result.payment_token or payment_token_header,
                        error_code=enforcement_result.error_code,
                        notes=enforcement_result.error_detail,
                    )

                    try:
                        log_api_request_event(event)
                    except Exception as e:
                        logger.error("Metering request-log insert failed: %s", e, exc_info=True)

                    if should_log_economics(decision):
                        econ_payment_fields = {
                            "payment_status": "failed_validation",
                            "payment_method": payment_method_header or decision.econ_payment_method,
                            "payment_network": enforcement_result.payment_network or payment_network_header,
                            "payment_token": enforcement_result.payment_token or payment_token_header,
                            "payment_amount_native": float(enforcement_result.payment_amount_native) if enforcement_result.payment_amount_native is not None else None,
                            "payment_amount_usd": None,
                            "payment_reference": enforcement_result.payment_reference,
                        }

                        econ = build_request_econ(
                            request_id=request_id,
                            customer_id=customer_id,
                            api_key_id=api_key_id,
                            pricing_rule_id=economic_rule_name,
                            unit_price_usd=unit_price_usd,
                            billed_amount_usd=billed_amount_usd,
                            stc_cost=stc_cost,
                            payment_required=1,
                            payment_rail=payment_rail,
                            payment_channel_id=enforcement_result.payment_channel_id,
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

                replay_reference = enforcement_result.payment_reference
                if enforcement_result.outcome == "replay_detected":
                    response = JSONResponse(
                        status_code=402,
                        content={
                            "error": "replay_detected",
                            "detail": "Payment reference has already been used.",
                            "request_id": request_id,
                        },
                    )

                    pricing_rule_for_headers = decision.econ_pricing_rule_id or decision.log_pricing_rule_id
                    apply_pricing_headers(
                        response,
                        pricing_rule_id=pricing_rule_for_headers,
                        payment_required=True,
                        accepted_methods=get_accepted_payment_methods(
                            path,
                            pricing_rule_for_headers,
                            method=method,
                            enforced_payment_method="x402",
                        ),
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
                        status_code=402,
                        success=0,
                        latency_ms=latency_ms,
                        response=response,
                        decision=decision,
                        payment_rail=payment_rail,
                        payment_method=resolved_payment_method,
                        payment_network=enforcement_result.payment_network or payment_network_header,
                        payment_token=enforcement_result.payment_token or payment_token_header,
                        error_code=enforcement_result.error_code,
                        notes=enforcement_result.error_detail,
                    )

                    try:
                        log_api_request_event(event)
                    except Exception as e:
                        logger.error("Metering request-log insert failed: %s", e, exc_info=True)

                    if should_log_economics(decision):
                        econ_payment_fields = {
                            "payment_status": "failed_validation",
                            "payment_method": payment_method_header or decision.econ_payment_method,
                            "payment_network": enforcement_result.payment_network or payment_network_header,
                            "payment_token": enforcement_result.payment_token or payment_token_header,
                            "payment_amount_native": float(enforcement_result.payment_amount_native) if enforcement_result.payment_amount_native is not None else None,
                            "payment_amount_usd": None,
                            "payment_reference": replay_reference,
                        }

                        econ = build_request_econ(
                            request_id=request_id,
                            customer_id=customer_id,
                            api_key_id=api_key_id,
                            pricing_rule_id=economic_rule_name,
                            unit_price_usd=unit_price_usd,
                            billed_amount_usd=billed_amount_usd,
                            stc_cost=stc_cost,
                            payment_required=1,
                            payment_rail=payment_rail,
                            payment_channel_id=enforcement_result.payment_channel_id,
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

                if enforcement_result.outcome == "verification_failed":
                    response = JSONResponse(
                        status_code=402,
                        content={
                            "error": "payment_verification_failed",
                            "detail": enforcement_result.error_detail,
                            "request_id": request_id,
                        },
                    )

                    pricing_rule_for_headers = decision.econ_pricing_rule_id or decision.log_pricing_rule_id
                    apply_pricing_headers(
                        response,
                        pricing_rule_id=pricing_rule_for_headers,
                        payment_required=True,
                        accepted_methods=get_accepted_payment_methods(
                            path,
                            pricing_rule_for_headers,
                            method=method,
                            enforced_payment_method="x402",
                        ),
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
                        status_code=402,
                        success=0,
                        latency_ms=latency_ms,
                        response=response,
                        decision=decision,
                        payment_rail=payment_rail,
                        payment_method=resolved_payment_method,
                        payment_network=enforcement_result.payment_network or payment_network_header,
                        payment_token=enforcement_result.payment_token or payment_token_header,
                        error_code="payment_verification_failed",
                        notes=enforcement_result.error_detail,
                    )

                    try:
                        log_api_request_event(event)
                    except Exception as e:
                        logger.error("Metering request-log insert failed: %s", e, exc_info=True)

                    if should_log_economics(decision):
                        econ_payment_fields = {
                            "payment_status": "failed_validation",
                            "payment_method": payment_method_header or decision.econ_payment_method,
                            "payment_network": enforcement_result.payment_network or payment_network_header,
                            "payment_token": enforcement_result.payment_token or payment_token_header,
                            "payment_amount_native": float(enforcement_result.payment_amount_native) if enforcement_result.payment_amount_native is not None else None,
                            "payment_amount_usd": None,
                            "payment_reference": replay_reference,
                        }

                        econ = build_request_econ(
                            request_id=request_id,
                            customer_id=customer_id,
                            api_key_id=api_key_id,
                            pricing_rule_id=economic_rule_name,
                            unit_price_usd=unit_price_usd,
                            billed_amount_usd=billed_amount_usd,
                            stc_cost=stc_cost,
                            payment_required=1,
                            payment_rail=payment_rail,
                            payment_channel_id=enforcement_result.payment_channel_id,
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

                if enforcement_result.outcome == "settlement_failed":
                    response = JSONResponse(
                        status_code=402,
                        content={
                            "error": "payment_settlement_failed",
                            "detail": enforcement_result.error_detail,
                            "request_id": request_id,
                        },
                    )

                    pricing_rule_for_headers = decision.econ_pricing_rule_id or decision.log_pricing_rule_id
                    apply_pricing_headers(
                        response,
                        pricing_rule_id=pricing_rule_for_headers,
                        payment_required=True,
                        accepted_methods=get_accepted_payment_methods(
                            path,
                            pricing_rule_for_headers,
                            method=method,
                            enforced_payment_method="x402",
                        ),
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
                        status_code=402,
                        success=0,
                        latency_ms=latency_ms,
                        response=response,
                        decision=decision,
                        payment_rail=payment_rail,
                        payment_method=resolved_payment_method,
                        payment_network=enforcement_result.payment_network or payment_network_header,
                        payment_token=enforcement_result.payment_token or payment_token_header,
                        error_code="payment_settlement_failed",
                        notes=enforcement_result.error_detail,
                    )

                    try:
                        log_api_request_event(event)
                    except Exception as e:
                        logger.error("Metering request-log insert failed: %s", e, exc_info=True)

                    if should_log_economics(decision):
                        econ_payment_fields = {
                            "payment_status": "failed",
                            "payment_method": payment_method_header or decision.econ_payment_method,
                            "payment_network": enforcement_result.payment_network or payment_network_header,
                            "payment_token": enforcement_result.payment_token or payment_token_header,
                            "payment_amount_native": float(enforcement_result.payment_amount_native) if enforcement_result.payment_amount_native is not None else None,
                            "payment_amount_usd": None,
                            "payment_reference": replay_reference,
                        }

                        econ = build_request_econ(
                            request_id=request_id,
                            customer_id=customer_id,
                            api_key_id=api_key_id,
                            pricing_rule_id=economic_rule_name,
                            unit_price_usd=unit_price_usd,
                            billed_amount_usd=billed_amount_usd,
                            stc_cost=stc_cost,
                            payment_required=1,
                            payment_rail=payment_rail,
                            payment_channel_id=enforcement_result.payment_channel_id,
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

                request.state.x402_payment_response = enforcement_result.payment_response

                payment_reference_header = replay_reference
                payment_network_header = enforcement_result.payment_network or payment_network_header
                payment_token_header = enforcement_result.payment_token or payment_token_header
                if enforcement_result.payment_amount_native is not None:
                    payment_amount_header = str(enforcement_result.payment_amount_native)
                payment_channel_id = enforcement_result.payment_channel_id
                request.state.payment_channel_id = payment_channel_id

            if payment_rail == "mpp":
                payment_reference_header = enforcement_result.payment_reference or payment_reference_header
                payment_network_header = enforcement_result.payment_network or payment_network_header
                payment_token_header = enforcement_result.payment_token or payment_token_header
                if enforcement_result.payment_amount_native is not None:
                    payment_amount_header = str(enforcement_result.payment_amount_native)
                payment_channel_id = enforcement_result.payment_channel_id
                request.state.payment_channel_id = payment_channel_id
                if enforcement_result.outcome not in {"proceed", "authorized"}:
                    validation_valid = False
                    validation_error = enforcement_result.error_code
                    validation_detail = enforcement_result.error_detail

            if payment_rail != "x402" and not validation_valid:
                response = JSONResponse(
                    status_code=402,
                    content={
                        "error": validation_error,
                        "detail": validation_detail,
                        "request_id": request_id,
                    },
                )

                pricing_rule_for_headers = decision.econ_pricing_rule_id or decision.log_pricing_rule_id
                apply_pricing_headers(
                    response,
                    pricing_rule_id=pricing_rule_for_headers,
                    payment_required=True,
                    accepted_methods=get_accepted_payment_methods(
                        path,
                        pricing_rule_for_headers,
                        method=method,
                        enforced_payment_method=None,
                    ),
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
                    status_code=402,
                    success=0,
                    latency_ms=latency_ms,
                    response=response,
                    decision=decision,
                    payment_rail=payment_rail,
                    payment_method=resolved_payment_method,
                    error_code=validation_error,
                    notes=validation_detail,
                )

                try:
                    log_api_request_event(event)
                except Exception as e:
                    logger.error("Metering request-log insert failed: %s", e, exc_info=True)

                if should_log_economics(decision):
                    econ_payment_fields = build_econ_payment_fields(
                        payment_required=1,
                        payment_status="failed_validation",
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
                        pricing_rule_id=economic_rule_name,
                        unit_price_usd=unit_price_usd,
                        billed_amount_usd=billed_amount_usd,
                        stc_cost=stc_cost,
                        payment_required=1,
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

                return response

        response = None
        caught_exception = None

        try:
            response = await call_next(request)
            return response
        except Exception as exc:
            caught_exception = exc
            raise
        finally:
            latency_ms = int((time.time() - start_time) * 1000)
            status_code = response.status_code if response is not None else 500
            success = 1 if status_code < 400 else 0

            pricing_rule_for_headers = decision.econ_pricing_rule_id or decision.log_pricing_rule_id
            payment_required_for_headers = bool(decision.econ_payment_required)
            accepted_methods = get_accepted_payment_methods(path, pricing_rule_for_headers, method=method)

            if response is not None:
                if decision.econ_payment_required and is_x402_payment_method(normalized_payment_method):
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
                error_code=caught_exception.__class__.__name__ if caught_exception else None,
                notes=str(caught_exception) if caught_exception else None,
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
                    _cap = capture_mpp_payment(
                        channel_id=enforcement_result.payment_channel_id,
                        payment_reference=enforcement_result.payment_reference,
                        captured_stc=stc_cost,
                        pricing_rule_id=economic_rule_name,
                        request_id=request_id,
                    )
                    if _cap.success:
                        mpp_capture_outcome = "captured"
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

            if should_log_economics(decision):
                payment_status = decision.econ_payment_status

                if decision.econ_payment_required:
                    if payment_rail == "x402" or is_x402_payment_method(normalized_payment_method):
                        if validation_valid and payment_reference_header:
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
                                # Enforcement disabled or pre-control-plane path.
                                payment_status = "presented"
                        elif not validation_valid:
                            payment_status = "failed_validation" if should_enforce_agent_pay else "pending"

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
