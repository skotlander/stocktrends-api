from dataclasses import dataclass
import os

from payments.policy_provider import (
    get_agent_pay_auth_bypass_methods,
    get_effective_endpoint_payment_policy,
    is_agent_pay_route,
    is_free_metered_path,
    is_public_stocktrends_path,
)


ENABLE_AGENT_PAY = os.getenv("ENABLE_AGENT_PAY", "false").lower() == "true"
ENFORCE_AGENT_PAY = os.getenv("ENFORCE_AGENT_PAY", "false").lower() == "true"
VALIDATE_AGENT_PAY_HEADERS = os.getenv("VALIDATE_AGENT_PAY_HEADERS", "true").lower() == "true"


@dataclass
class PricingDecision:
    is_metered: int

    # access / policy layer
    access_granted: bool
    deny_reason: str | None

    # logging layer
    log_pricing_rule_id: str | None
    log_payment_method: str | None

    # economics layer
    econ_pricing_rule_id: str | None
    econ_payment_required: int
    econ_payment_status: str | None
    econ_payment_method: str | None


NON_METERED_PATHS = {
    "/",
    "/index.html",
    "/llms.txt",
    "/ai-dataset.json",
    "/tools.json",
    "/sitemap.xml",
    "/robots.txt",
    "/docs",
    "/v1/docs",
    "/openapi.json",
    "/v1/openapi.json",
    "/health",
    "/favicon.ico",
    "/v1/pricing",
    # Workflow catalog: public discovery surface, no metering.
    "/v1/workflows",
    # Planning helpers: public discovery surfaces, no metering.
    "/v1/cost-estimate",
    "/v1/instruments/lookup",
    "/v1/instruments/resolve",
    "/v1/stwr/reports/catalog",
    "/v1/stocktrends/portfolios",
    "/v1/meta/indicators",
    "/v1/meta/inference",
    "/v1/meta/stim",
    "/v1/meta/stwr",
    "/v1/leadership/definitions",
    "/v1/selections/stim-select/outcomes/summary",
    # MCP tools manifest: public discovery surface, no metering.
    "/v1/ai/tools",
    # Free proof-of-value endpoint: non-metered, no billing record.
    "/v1/ai/proof/market-edge",
}

# Path prefixes that are non-metered regardless of the full path.
# Used for operational/diagnostic surfaces that must never consume subscription quota.
NON_METERED_PREFIXES: frozenset[str] = frozenset({
    "/v1/observability/",
})

# Non-API probe/scanner traffic that should never be treated as billable API usage.
NON_API_NOISE_PREFIXES = {
    "/cdn-cgi/",
    "/.well-known/",
    "/autodiscover/",
    "/owa/",
    "/SDK/",
    "/sdk/",
    "/wp-",
    "/wordpress",
    "/phpmyadmin",
}

NON_API_NOISE_EXACT_PATHS = {
    "/jobs",
    "/favicon.ico",
}


def _is_noise_path(path: str) -> bool:
    if path in NON_API_NOISE_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in NON_API_NOISE_PREFIXES)


def _free_decision() -> PricingDecision:
    return PricingDecision(
        is_metered=0,
        access_granted=True,
        deny_reason=None,
        log_pricing_rule_id="default_free",
        log_payment_method="none",
        econ_pricing_rule_id=None,
        econ_payment_required=0,
        econ_payment_status=None,
        econ_payment_method=None,
    )


def _free_metered_decision() -> PricingDecision:
    return PricingDecision(
        is_metered=1,
        access_granted=True,
        deny_reason=None,
        log_pricing_rule_id="default_free_metered",
        log_payment_method="free",
        econ_pricing_rule_id="default_free_metered",
        econ_payment_required=0,
        econ_payment_status="not_required",
        econ_payment_method="free",
    )


def _subscription_decision(pricing_rule_id: str | None = None) -> PricingDecision:
    # pricing_rule_id: the endpoint-specific rule (e.g. "portfolio_compare").
    # When provided, it is recorded in both the event log and economics log so
    # per-endpoint STC equivalents are available for analytics and future
    # control-plane use.  Access remains quota-based: econ_payment_required=0
    # means the caller is never charged per-request regardless of the rule value.
    rule = pricing_rule_id or "default_subscription"
    return PricingDecision(
        is_metered=1,
        access_granted=True,
        deny_reason=None,
        log_pricing_rule_id=rule,
        log_payment_method="subscription",
        econ_pricing_rule_id=rule,
        econ_payment_required=0,
        econ_payment_status="not_required",
        econ_payment_method="subscription",
    )


def _agent_pay_decision(method: str, pricing_rule_id: str | None = None) -> PricingDecision:
    normalized_method = (method or "").strip().lower() or "mpp"
    rule_id = pricing_rule_id or "agent_pay_required"

    return PricingDecision(
        is_metered=1,
        access_granted=True,
        deny_reason=None,
        log_pricing_rule_id=rule_id,
        log_payment_method=normalized_method,
        econ_pricing_rule_id=rule_id,
        econ_payment_required=1,
        econ_payment_status="pending",
        econ_payment_method=normalized_method,
    )


def _deny_decision(reason: str = "access_denied") -> PricingDecision:
    return PricingDecision(
        is_metered=1,
        access_granted=False,
        deny_reason=reason,
        log_pricing_rule_id="default_subscription",
        log_payment_method="subscription",
        econ_pricing_rule_id="default_subscription",
        econ_payment_required=0,
        econ_payment_status="not_required",
        econ_payment_method="subscription",
    )


def _is_paid_plan(plan_code: str | None) -> bool:
    if not plan_code:
        return False

    normalized = str(plan_code).strip().lower()
    if not normalized:
        return False

    return normalized not in {"sandbox", "free", "trial", "test"}


def _is_identified_agent(agent_identifier: str | None) -> bool:
    return bool(agent_identifier and str(agent_identifier).strip())


def _normalize_payment_method(payment_method_header: str | None) -> str:
    return (payment_method_header or "").strip().lower()


def _has_agent_payment_intent(
    payment_method_header: str | None,
    agent_identifier: str | None,
    allowed_methods: tuple[str, ...],
) -> bool:
    normalized_payment_method = _normalize_payment_method(payment_method_header)
    if not normalized_payment_method:
        return False

    if not _is_identified_agent(agent_identifier):
        return False

    return normalized_payment_method in set(allowed_methods)


def classify_request(
    path: str,
    has_paid_auth: bool,
    payment_method_header: str | None = None,
    plan_code: str | None = None,
    agent_identifier: str | None = None,
    method: str | None = None,
) -> PricingDecision:
    """
    Classify request into pricing / metering tiers.

    Lane B-aware STIM policy:
    - Public/static/docs paths are non-metered
    - Explicit free-metered API routes are tracked but not billed
    - /v1/stim*:
        * identified agent + supported payment method + agent pay enabled => agent-pay
        * paid entitled customer => subscription
        * otherwise => deny
    - Other /v1/* routes are subscription-backed
    - Non-/v1 probe traffic is never treated as paid API usage
    """

    if path in NON_METERED_PATHS or is_public_stocktrends_path(path):
        return _free_decision()

    if _is_noise_path(path):
        return _free_decision()

    if any(path.startswith(p) for p in NON_METERED_PREFIXES):
        return _free_decision()

    if is_free_metered_path(path):
        return _free_metered_decision()

    endpoint_policy = get_effective_endpoint_payment_policy(path, method)
    is_stim = is_agent_pay_route(path, method)
    has_paid_plan = _is_paid_plan(plan_code)
    identified_agent = _is_identified_agent(agent_identifier)
    has_agent_payment_intent = _has_agent_payment_intent(
        payment_method_header,
        agent_identifier,
        get_agent_pay_auth_bypass_methods(path, method),
    )
    normalized_payment_method = _normalize_payment_method(payment_method_header)

    if endpoint_policy is not None and path.startswith("/v1/"):
        if endpoint_policy.allows_subscription and has_paid_auth:
            return _subscription_decision(pricing_rule_id=endpoint_policy.pricing_rule_id)

        if ENABLE_AGENT_PAY and identified_agent and has_agent_payment_intent:
            return _agent_pay_decision(normalized_payment_method, pricing_rule_id=endpoint_policy.pricing_rule_id)

        # Issue a 402 challenge for any no-auth request on an agent-pay-capable endpoint
        # when agent pay is enabled, so the caller learns to pay rather than receiving an
        # opaque deny. Mirrors the equivalent path in the STIM prefix block below.
        if ENABLE_AGENT_PAY and not has_paid_auth and endpoint_policy.machine_payment_rails:
            return _agent_pay_decision("x402", pricing_rule_id=endpoint_policy.pricing_rule_id)

        if endpoint_policy.allows_subscription:
            if identified_agent and endpoint_policy.machine_payment_rails:
                return _deny_decision("agent_payment_required")
            return _deny_decision("authentication_required")

        if endpoint_policy.machine_payment_rails:
            if identified_agent:
                return _deny_decision("agent_payment_required")
            return _deny_decision("authentication_required")

        return _deny_decision("access_denied")

    if is_stim:
        # Explicit Lane B path: identified agent presents a machine-payment intent.
        if ENABLE_AGENT_PAY and identified_agent and has_agent_payment_intent:
            return _agent_pay_decision(normalized_payment_method, pricing_rule_id="stim_paid")

        # Paid subscription customer remains entitled on the subscription lane.
        if has_paid_auth and has_paid_plan:
            return _subscription_decision()

        # Any keyless request with no paid auth: issue a 402 challenge so the caller
        # learns how to pay rather than receiving an opaque 403. The API key middleware
        # already gates entry to this block via is_agent_pay_enforcement_path, so all
        # traffic here is on a known agent-pay enforcement scope.
        if ENABLE_AGENT_PAY and not has_paid_auth:
            return _agent_pay_decision("x402", pricing_rule_id="stim_paid")

        # Sandbox/free/test callers are not entitled to STIM subscription access.
        normalized_plan = (plan_code or "").strip().lower()
        if normalized_plan == "sandbox":
            return _deny_decision("sandbox_plan_denied")

        if not has_paid_auth:
            return _deny_decision("authentication_required")

        return _deny_decision("stim_access_not_permitted")

    if path.startswith("/v1/"):
        return _subscription_decision()

    return _free_decision()
