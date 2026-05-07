import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db import get_metering_engine

logger = logging.getLogger("stocktrends_api.metering.logger")


INSERT_REQUEST_LOG_SQL = text("""
INSERT INTO api_request_logs (
    event_time_utc,
    request_id,
    environment,
    api_key_id,
    customer_id,
    subscription_id,
    plan_code,
    actor_type,
    workflow_type,
    agent_identifier,
    agent_id,
    endpoint_path,
    route_template,
    endpoint_family,
    http_method,
    query_string,
    symbol,
    exchange,
    symbol_exchange,
    status_code,
    success,
    latency_ms,
    response_size_bytes,
    client_ip,
    user_agent,
    referer,
    is_metered,
    is_billable,
    payment_rail,
    payment_method,
    payment_network,
    payment_token,
    pricing_rule_id,
    error_code,
    notes
) VALUES (
    :event_time_utc,
    :request_id,
    :environment,
    :api_key_id,
    :customer_id,
    :subscription_id,
    :plan_code,
    :actor_type,
    :workflow_type,
    :agent_identifier,
    :agent_id,
    :endpoint_path,
    :route_template,
    :endpoint_family,
    :http_method,
    :query_string,
    :symbol,
    :exchange,
    :symbol_exchange,
    :status_code,
    :success,
    :latency_ms,
    :response_size_bytes,
    :client_ip,
    :user_agent,
    :referer,
    :is_metered,
    :is_billable,
    :payment_rail,
    :payment_method,
    :payment_network,
    :payment_token,
    :pricing_rule_id,
    :error_code,
    :notes
)
""")


INSERT_REQUEST_LOG_SQL_LEGACY = text("""
INSERT INTO api_request_logs (
    event_time_utc,
    request_id,
    environment,
    api_key_id,
    customer_id,
    subscription_id,
    plan_code,
    actor_type,
    workflow_type,
    agent_identifier,
    agent_id,
    endpoint_path,
    route_template,
    endpoint_family,
    http_method,
    query_string,
    symbol,
    exchange,
    symbol_exchange,
    status_code,
    success,
    latency_ms,
    response_size_bytes,
    client_ip,
    user_agent,
    referer,
    is_metered,
    is_billable,
    payment_method,
    pricing_rule_id,
    error_code,
    notes
) VALUES (
    :event_time_utc,
    :request_id,
    :environment,
    :api_key_id,
    :customer_id,
    :subscription_id,
    :plan_code,
    :actor_type,
    :workflow_type,
    :agent_identifier,
    :agent_id,
    :endpoint_path,
    :route_template,
    :endpoint_family,
    :http_method,
    :query_string,
    :symbol,
    :exchange,
    :symbol_exchange,
    :status_code,
    :success,
    :latency_ms,
    :response_size_bytes,
    :client_ip,
    :user_agent,
    :referer,
    :is_metered,
    :is_billable,
    :payment_method,
    :pricing_rule_id,
    :error_code,
    :notes
)
""")


INSERT_REQUEST_ECONOMICS_SQL = text("""
INSERT INTO api_request_economics (
    request_id,
    customer_id,
    api_key_id,
    pricing_rule_id,
    unit_price_usd,
    billed_amount_usd,
    stc_cost,
    payment_required,
    payment_rail,
    payment_status,
    payment_method,
    payment_network,
    payment_token,
    payment_amount_native,
    payment_amount_usd,
    payment_reference,
    session_id,
    payment_channel_id,
    agent_id,
    agent_type,
    agent_vendor,
    agent_version,
    request_purpose
) VALUES (
    :request_id,
    :customer_id,
    :api_key_id,
    :pricing_rule_id,
    :unit_price_usd,
    :billed_amount_usd,
    :stc_cost,
    :payment_required,
    :payment_rail,
    :payment_status,
    :payment_method,
    :payment_network,
    :payment_token,
    :payment_amount_native,
    :payment_amount_usd,
    :payment_reference,
    :session_id,
    :payment_channel_id,
    :agent_id,
    :agent_type,
    :agent_vendor,
    :agent_version,
    :request_purpose
)
""")


UPDATE_PENDING_REQUEST_ECONOMICS_SQL = text("""
UPDATE api_request_economics
SET
    customer_id = :customer_id,
    api_key_id = :api_key_id,
    pricing_rule_id = :pricing_rule_id,
    unit_price_usd = :unit_price_usd,
    billed_amount_usd = :billed_amount_usd,
    stc_cost = :stc_cost,
    payment_required = :payment_required,
    payment_rail = :payment_rail,
    payment_status = :payment_status,
    payment_method = :payment_method,
    payment_network = :payment_network,
    payment_token = :payment_token,
    payment_amount_native = :payment_amount_native,
    payment_amount_usd = :payment_amount_usd,
    payment_reference = :payment_reference,
    session_id = :session_id,
    payment_channel_id = :payment_channel_id,
    agent_id = :agent_id,
    agent_type = :agent_type,
    agent_vendor = :agent_vendor,
    agent_version = :agent_version,
    request_purpose = :request_purpose
WHERE request_id = :request_id
  AND payment_status = 'pending'
""")


INSERT_REQUEST_ECONOMICS_SQL_LEGACY = text("""
INSERT INTO api_request_economics (
    request_id,
    customer_id,
    api_key_id,
    pricing_rule_id,
    unit_price_usd,
    billed_amount_usd,
    payment_required,
    payment_status,
    payment_method,
    payment_network,
    payment_token,
    payment_amount_native,
    payment_amount_usd,
    payment_reference,
    session_id,
    payment_channel_id,
    agent_id,
    agent_type,
    agent_vendor,
    agent_version,
    request_purpose
) VALUES (
    :request_id,
    :customer_id,
    :api_key_id,
    :pricing_rule_id,
    :unit_price_usd,
    :billed_amount_usd,
    :payment_required,
    :payment_status,
    :payment_method,
    :payment_network,
    :payment_token,
    :payment_amount_native,
    :payment_amount_usd,
    :payment_reference,
    :session_id,
    :payment_channel_id,
    :agent_id,
    :agent_type,
    :agent_vendor,
    :agent_version,
    :request_purpose
)
""")


UPDATE_PENDING_REQUEST_ECONOMICS_SQL_LEGACY = text("""
UPDATE api_request_economics
SET
    customer_id = :customer_id,
    api_key_id = :api_key_id,
    pricing_rule_id = :pricing_rule_id,
    unit_price_usd = :unit_price_usd,
    billed_amount_usd = :billed_amount_usd,
    payment_required = :payment_required,
    payment_status = :payment_status,
    payment_method = :payment_method,
    payment_network = :payment_network,
    payment_token = :payment_token,
    payment_amount_native = :payment_amount_native,
    payment_amount_usd = :payment_amount_usd,
    payment_reference = :payment_reference,
    session_id = :session_id,
    payment_channel_id = :payment_channel_id,
    agent_id = :agent_id,
    agent_type = :agent_type,
    agent_vendor = :agent_vendor,
    agent_version = :agent_version,
    request_purpose = :request_purpose
WHERE request_id = :request_id
  AND payment_status = 'pending'
""")


_request_log_legacy_warned = False
_request_econ_legacy_warned = False


def _is_missing_metering_columns_error(exc: Exception) -> bool:
    if not isinstance(exc, DBAPIError):
        return False

    message_parts = [str(exc)]

    if getattr(exc, "orig", None) is not None:
        message_parts.append(str(exc.orig))

    message = " ".join(message_parts).lower()
    mentions_metering_column = any(
        column in message
        for column in ("payment_rail", "payment_network", "payment_token", "stc_cost")
    )
    return mentions_metering_column and (
        "unknown column" in message
        or "invalid column" in message
        or "no column named" in message
    )


def _warn_legacy_fallback(table_name: str) -> None:
    global _request_log_legacy_warned, _request_econ_legacy_warned

    if table_name == "api_request_logs":
        if _request_log_legacy_warned:
            return
        _request_log_legacy_warned = True
    elif table_name == "api_request_economics":
        if _request_econ_legacy_warned:
            return
        _request_econ_legacy_warned = True

    logger.warning(
        "Metering schema mismatch detected for %s: one or more columns are missing "
        "(payment_rail, payment_network, payment_token, stc_cost). Falling back to the legacy insert "
        "shape until the schema migration is applied.",
        table_name,
    )


def log_api_request_event(event: dict) -> None:
    event = dict(event)
    event.setdefault("payment_network", None)
    event.setdefault("payment_token", None)

    engine = get_metering_engine()
    try:
        with engine.begin() as conn:
            conn.execute(INSERT_REQUEST_LOG_SQL, event)
    except DBAPIError as exc:
        if not _is_missing_metering_columns_error(exc):
            raise

        _warn_legacy_fallback("api_request_logs")
        legacy_event = dict(event)
        legacy_event.pop("payment_rail", None)
        legacy_event.pop("payment_network", None)
        legacy_event.pop("payment_token", None)

        with engine.begin() as conn:
            conn.execute(INSERT_REQUEST_LOG_SQL_LEGACY, legacy_event)


def log_api_request_economics(econ: dict) -> None:
    econ = dict(econ)
    engine = get_metering_engine()
    try:
        with engine.begin() as conn:
            if _should_update_pending_economics(econ):
                result = conn.execute(UPDATE_PENDING_REQUEST_ECONOMICS_SQL, econ)
                if getattr(result, "rowcount", 0):
                    return
            conn.execute(INSERT_REQUEST_ECONOMICS_SQL, econ)
    except DBAPIError as exc:
        if not _is_missing_metering_columns_error(exc):
            raise

        _warn_legacy_fallback("api_request_economics")
        legacy_econ = dict(econ)
        legacy_econ.pop("payment_rail", None)

        with engine.begin() as conn:
            if _should_update_pending_economics(legacy_econ):
                result = conn.execute(UPDATE_PENDING_REQUEST_ECONOMICS_SQL_LEGACY, legacy_econ)
                if getattr(result, "rowcount", 0):
                    return
            conn.execute(INSERT_REQUEST_ECONOMICS_SQL_LEGACY, legacy_econ)


def _should_update_pending_economics(econ: dict) -> bool:
    request_id = econ.get("request_id")
    payment_status = econ.get("payment_status")
    payment_required = econ.get("payment_required")
    payment_rail = str(econ.get("payment_rail") or "").strip().lower()
    payment_method = str(econ.get("payment_method") or "").strip().lower()
    is_x402 = payment_rail == "x402" or payment_method == "x402"
    return bool(request_id and payment_required and is_x402 and payment_status and payment_status != "pending")


def log_auth_failure_event(
    *,
    request,
    status_code: int,
    error_code: str,
    notes: str | None,
) -> None:
    query_string = str(request.url.query)
    client_ip = None

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    else:
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            client_ip = real_ip.strip()
        elif request.client:
            client_ip = request.client.host

    event = {
        "event_time_utc": datetime.now(timezone.utc),
        "request_id": getattr(request.state, "request_id", None),
        "environment": "production",
        "api_key_id": None,
        "customer_id": None,
        "subscription_id": None,
        "plan_code": None,
        "actor_type": "unknown",
        "workflow_type": "auth_failure",
        "agent_identifier": None,
        "agent_id": None,
        "endpoint_path": request.url.path,
        "route_template": None,
        "endpoint_family": None,
        "http_method": request.method,
        "query_string": query_string,
        "symbol": request.query_params.get("symbol"),
        "exchange": request.query_params.get("exchange"),
        "symbol_exchange": request.query_params.get("symbol_exchange"),
        "status_code": status_code,
        "success": 0,
        "latency_ms": 0,
        "response_size_bytes": 0,
        "client_ip": client_ip,
        "user_agent": request.headers.get("user-agent"),
        "referer": request.headers.get("referer"),
        "is_metered": 0,
        "is_billable": 0,
        "payment_rail": "none",
        "payment_method": "none",
        "payment_network": None,
        "payment_token": None,
        "pricing_rule_id": "auth_failure",
        "error_code": error_code,
        "notes": notes,
    }

    log_api_request_event(event)
