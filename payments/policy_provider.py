from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from urllib import error, request


logger = logging.getLogger("stocktrends_api.payment_policy")

_DEFAULT_CONFIG_TTL_SECONDS = 30
_DEFAULT_FETCH_TIMEOUT_SECONDS = 2.0

_cache_lock = threading.Lock()
_cached_config: "RuntimePaymentPolicyConfig | None" = None
_cached_at: float = 0.0
_last_known_good_config: "RuntimePaymentPolicyConfig | None" = None
_last_reported_fallback_reason: str | None = None


def _parse_csv_env(env_name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(env_name, default)
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass
class EndpointPaymentPolicy:
    endpoint_id: str
    path_pattern: str
    method: str
    allowed_rails: tuple[str, ...]
    pricing_rule_id: str | None = None
    machine_payments_enabled: bool = True


@dataclass
class EffectiveEndpointPaymentPolicy:
    source: str
    allowed_rails: tuple[str, ...]
    machine_payment_rails: tuple[str, ...]
    allows_subscription: bool
    pricing_rule_id: str | None = None


@dataclass
class RuntimePaymentPolicyConfig:
    source: str
    version: str | None
    ttl_seconds: int
    fetched_at: float | None
    environment: str | None
    enabled_environment_rails: tuple[str, ...]
    pricing_rule_ids: tuple[str, ...]
    endpoint_payment_policies: tuple[EndpointPaymentPolicy, ...]
    free_metered_paths: tuple[str, ...]
    agent_pay_path_prefixes: tuple[str, ...]
    agent_pay_auth_bypass_methods: tuple[str, ...]
    enforcement_path_prefixes: tuple[str, ...]
    accepted_payment_methods_agent_required_default: str
    accepted_payment_methods_agent_required_by_method: dict[str, str] = field(default_factory=dict)
    accepted_payment_methods_agent_optional: str = "subscription,x402,mpp"
    accepted_payment_methods_subscription: str = "subscription"
    accepted_payment_methods_default: str = "none"


def _normalize_string_list(value, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
        return tuple(item for item in items if item) or default
    if isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            if item is None:
                continue
            normalized = str(item).strip()
            if normalized:
                items.append(normalized)
        return tuple(items) or default
    return default


_MACHINE_PAYMENT_RAILS = frozenset({"x402", "mpp"})

# Matches standard 8-4-4-4-12 UUID format.  Control-plane systems sometimes use
# their internal database UUID as a policy identifier rather than the semantic
# DB rule_name.  A UUID-like value is not a valid api_pricing_rules.rule_name and
# would cause resolve_economic_amounts() to return (0, 0, 0).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_uuid_like(value: str) -> bool:
    """Return True if *value* looks like a UUID (internal identifier, not a DB rule_name)."""
    return bool(_UUID_RE.match(value))


def _extract_enabled_rail_codes(value) -> tuple[str, ...]:
    """Normalize allowed_rails from strings, comma-separated strings, or control-plane rail objects."""
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, (list, tuple)):
        return ()
    codes: list[str] = []
    for item in value:
        if isinstance(item, dict):
            if not item.get("enabled", True):
                continue
            code = str(item.get("rail_code") or "").strip().lower()
        else:
            code = str(item).strip().lower()
        if code:
            codes.append(code)
    return tuple(codes)


def _normalize_string_map(value, *, default: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return dict(default)

    normalized: dict[str, str] = {}
    for key, map_value in value.items():
        normalized_key = str(key).strip().lower()
        normalized_value = str(map_value).strip() if map_value is not None else ""
        if normalized_key and normalized_value:
            normalized[normalized_key] = normalized_value

    return normalized or dict(default)


def _normalize_int(value, *, default: int) -> int:
    try:
        normalized = int(value)
        return normalized if normalized > 0 else default
    except (TypeError, ValueError):
        return default


def _default_policy_config() -> RuntimePaymentPolicyConfig:
    return RuntimePaymentPolicyConfig(
        source="defaults",
        version=None,
        ttl_seconds=_normalize_int(
            os.getenv("PAYMENT_POLICY_CONFIG_TTL_SECONDS"),
            default=_DEFAULT_CONFIG_TTL_SECONDS,
        ),
        fetched_at=None,
        environment=None,
        enabled_environment_rails=(),
        pricing_rule_ids=(),
        endpoint_payment_policies=(
            EndpointPaymentPolicy(
                endpoint_id="agent_screener_top",
                path_pattern="/v1/agent/screener/top",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="agent_screener_top",
            ),
            EndpointPaymentPolicy(
                endpoint_id="market_regime_latest",
                path_pattern="/v1/market/regime/latest",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="market_regime_latest",
            ),
            EndpointPaymentPolicy(
                endpoint_id="market_regime_history",
                path_pattern="/v1/market/regime/history",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="market_regime_history",
            ),
            EndpointPaymentPolicy(
                endpoint_id="market_regime_forecast",
                path_pattern="/v1/market/regime/forecast",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="market_regime_forecast",
            ),
            EndpointPaymentPolicy(
                endpoint_id="evaluate_symbol",
                path_pattern="/v1/decision/evaluate-symbol",
                method="POST",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="evaluate_symbol",
            ),
            EndpointPaymentPolicy(
                endpoint_id="portfolio_construct",
                path_pattern="/v1/portfolio/construct",
                method="POST",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="portfolio_construct",
            ),
            EndpointPaymentPolicy(
                endpoint_id="portfolio_evaluate",
                path_pattern="/v1/portfolio/evaluate",
                method="POST",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="portfolio_evaluate",
            ),
            EndpointPaymentPolicy(
                endpoint_id="portfolio_compare",
                path_pattern="/v1/portfolio/compare",
                method="POST",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="portfolio_compare",
            ),
            # --- official Stock Trends model portfolio metadata ---
            EndpointPaymentPolicy(
                endpoint_id="stocktrends_portfolios_list_paid",
                path_pattern="/v1/stocktrends/portfolios",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="stocktrends_portfolios_list_paid",
            ),
            EndpointPaymentPolicy(
                endpoint_id="stocktrends_portfolios_detail_paid",
                path_pattern="/v1/stocktrends/portfolios/{port_id}",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="stocktrends_portfolios_detail_paid",
            ),
            # --- stim endpoints (explicit per-endpoint rules supersede the /v1/stim prefix
            #     fallback; each maps to its own active DB pricing rule so resolve_economic_amounts
            #     returns the correct non-zero STC/USD values for the x402 challenge) ---
            EndpointPaymentPolicy(
                endpoint_id="stim_latest_paid",
                path_pattern="/v1/stim/latest",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="stim_latest_paid",
            ),
            EndpointPaymentPolicy(
                endpoint_id="stim_history_paid",
                path_pattern="/v1/stim/history",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="stim_history_paid",
            ),
            # --- indicators endpoints ---
            EndpointPaymentPolicy(
                endpoint_id="indicators_latest_paid",
                path_pattern="/v1/indicators/latest",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="indicators_latest_paid",
            ),
            EndpointPaymentPolicy(
                endpoint_id="indicators_history_paid",
                path_pattern="/v1/indicators/history",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="indicators_history_paid",
            ),
            # --- prices endpoints ---
            EndpointPaymentPolicy(
                endpoint_id="prices_latest_paid",
                path_pattern="/v1/prices/latest",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="prices_latest_paid",
            ),
            EndpointPaymentPolicy(
                endpoint_id="prices_history_paid",
                path_pattern="/v1/prices/history",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="prices_history_paid",
            ),
            # --- selections endpoints ---
            EndpointPaymentPolicy(
                endpoint_id="selections_latest_paid",
                path_pattern="/v1/selections/latest",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="selections_latest_paid",
            ),
            EndpointPaymentPolicy(
                endpoint_id="selections_history_paid",
                path_pattern="/v1/selections/history",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="selections_history_paid",
            ),
            # --- stwr report endpoints ---
            EndpointPaymentPolicy(
                endpoint_id="stwr_reports_latest_paid",
                path_pattern="/v1/stwr/reports/latest",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="stwr_reports_latest_paid",
            ),
            EndpointPaymentPolicy(
                endpoint_id="stwr_reports_history_paid",
                path_pattern="/v1/stwr/reports/history",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="stwr_reports_history_paid",
            ),
            # --- breadth endpoints ---
            EndpointPaymentPolicy(
                endpoint_id="breadth_sector_latest_paid",
                path_pattern="/v1/breadth/sector/latest",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="breadth_sector_latest_paid",
            ),
            EndpointPaymentPolicy(
                endpoint_id="breadth_sector_history_paid",
                path_pattern="/v1/breadth/sector/history",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="breadth_sector_history_paid",
            ),
            # --- leadership intelligence endpoints (definitions is a public helper) ---
            EndpointPaymentPolicy(
                endpoint_id="leadership_summary_latest_paid",
                path_pattern="/v1/leadership/summary/latest",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="leadership_summary_latest_paid",
            ),
            EndpointPaymentPolicy(
                endpoint_id="leadership_rotation_history_paid",
                path_pattern="/v1/leadership/rotation/history",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="leadership_rotation_history_paid",
            ),
            # --- selections/published endpoints ---
            EndpointPaymentPolicy(
                endpoint_id="selections_published_latest_paid",
                path_pattern="/v1/selections/published/latest",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="selections_published_latest_paid",
            ),
            EndpointPaymentPolicy(
                endpoint_id="selections_published_history_paid",
                path_pattern="/v1/selections/published/history",
                method="GET",
                allowed_rails=("subscription", "x402", "mpp"),
                pricing_rule_id="selections_published_history_paid",
            ),
        ),
        free_metered_paths=(
            "/v1/ai/context",
        ),
        # Prefix-based agent-pay scope is intentionally limited to /v1/stim.
        # Newer premium routes (/v1/agent/screener/*, /v1/market/*, etc.) are
        # governed by exact endpoint_payment_policies above, not by prefix matching.
        agent_pay_path_prefixes=(
            "/v1/stim",
        ),
        agent_pay_auth_bypass_methods=(
            "mpp",
            "x402",
        ),
        enforcement_path_prefixes=_parse_csv_env(
            "AGENT_PAY_ENFORCE_PATH_PREFIXES",
            "/v1/stim",
        ),
        accepted_payment_methods_agent_required_default="subscription,x402,mpp",
        accepted_payment_methods_agent_required_by_method={
            "x402": "x402",
            "mpp": "mpp",
        },
        accepted_payment_methods_agent_optional="subscription,x402,mpp",
        accepted_payment_methods_subscription="subscription",
        accepted_payment_methods_default="none",
    )


def _normalize_enabled_rails(value) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()

    rails: list[str] = []
    for rail_name, enabled in value.items():
        normalized_name = str(rail_name).strip().lower()
        if normalized_name and bool(enabled):
            rails.append(normalized_name)

    return tuple(rails)


def _normalize_pricing_rule_ids(value) -> tuple[str, ...]:
    if isinstance(value, dict):
        value = list(value.keys())

    if not isinstance(value, (list, tuple)):
        return ()

    pricing_rule_ids: list[str] = []
    for item in value:
        if isinstance(item, dict):
            candidate = item.get("rule_name") or item.get("id") or item.get("name")
        else:
            candidate = item

        normalized = str(candidate).strip() if candidate is not None else ""
        if normalized:
            pricing_rule_ids.append(normalized)

    return tuple(pricing_rule_ids)


def _normalize_endpoint_payment_policies(value) -> tuple[EndpointPaymentPolicy, ...]:
    if isinstance(value, dict):
        value = list(value.values())

    if not isinstance(value, (list, tuple)):
        return ()

    policies: list[EndpointPaymentPolicy] = []
    for item in value:
        if not isinstance(item, dict):
            continue

        if not item.get("active", True):
            continue

        path_pattern = str(item.get("path_pattern") or "").strip()
        method = str(item.get("method") or "").strip().upper()
        if not path_pattern or not method:
            continue

        allowed_rails = _extract_enabled_rail_codes(item.get("allowed_rails"))
        endpoint_id = str(
            item.get("endpoint_id") or item.get("endpoint_code") or item.get("endpoint_key") or item.get("id") or f"{method}:{path_pattern}"
        ).strip()
        pricing_rule_id = str(item.get("pricing_rule_id") or "").strip() or None
        machine_payments_enabled = bool(item.get("machine_payments_enabled", True))

        policies.append(
            EndpointPaymentPolicy(
                endpoint_id=endpoint_id,
                path_pattern=path_pattern,
                method=method,
                allowed_rails=tuple(allowed_rails),
                pricing_rule_id=pricing_rule_id,
                machine_payments_enabled=machine_payments_enabled,
            )
        )

    return tuple(policies)


def _resolve_config_url() -> str | None:
    explicit_url = (os.getenv("PAYMENT_POLICY_CONFIG_URL") or "").strip()
    if explicit_url:
        return explicit_url

    control_plane_base = (os.getenv("CONTROL_PLANE_BASE_URL") or "").strip()
    if control_plane_base:
        return f"{control_plane_base.rstrip('/')}/v1/runtime/payment-config"

    return None


def _extract_policy_section(payload: dict) -> dict:
    for key in ("payment_policy", "payment_config", "config", "data"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            return candidate
    return payload


def _lookup_exact_endpoint_policy(
    config: RuntimePaymentPolicyConfig,
    path: str,
    method: str | None,
) -> EndpointPaymentPolicy | None:
    if not method:
        return None

    normalized_method = method.strip().upper()
    for policy in config.endpoint_payment_policies:
        if policy.method == normalized_method and _path_pattern_matches(policy.path_pattern, path):
            return policy
    return None


def _path_pattern_matches(pattern: str, path: str) -> bool:
    # Most callers pass request.url.path, but tolerate a full path with query
    # string for direct policy-helper calls.
    path = path.split("?", 1)[0]

    if pattern == path:
        return True

    pattern_parts = [part for part in pattern.strip("/").split("/") if part]
    path_parts = [part for part in path.strip("/").split("/") if part]
    if len(pattern_parts) != len(path_parts):
        return False

    for pattern_part, path_part in zip(pattern_parts, path_parts):
        is_path_param = pattern_part.startswith("{") and pattern_part.endswith("}")
        if is_path_param:
            if not path_part:
                return False
            continue
        if pattern_part != path_part:
            return False

    return True


def _derive_allowed_rails_for_endpoint(
    config: RuntimePaymentPolicyConfig,
    path: str,
    method: str | None,
) -> tuple[str, ...] | None:
    endpoint_policy = _lookup_exact_endpoint_policy(config, path, method)
    if endpoint_policy is None:
        return None

    if not config.enabled_environment_rails:
        return endpoint_policy.allowed_rails

    enabled = set(config.enabled_environment_rails)
    return tuple(rail for rail in endpoint_policy.allowed_rails if rail in enabled)


def _build_effective_endpoint_policy(
    path: str,
    method: str | None,
) -> EffectiveEndpointPaymentPolicy | None:
    config = get_runtime_payment_policy_config()
    allowed_rails = _derive_allowed_rails_for_endpoint(config, path, method)
    if allowed_rails is None:
        return None

    raw_policy = _lookup_exact_endpoint_policy(config, path, method)
    pricing_rule_id = raw_policy.pricing_rule_id if raw_policy else None
    if raw_policy and not raw_policy.machine_payments_enabled:
        machine_payment_rails: tuple[str, ...] = ()
        allowed_rails = tuple(r for r in allowed_rails if r not in _MACHINE_PAYMENT_RAILS)
    else:
        machine_payment_rails = tuple(
            rail for rail in allowed_rails if rail in _MACHINE_PAYMENT_RAILS
        )
    return EffectiveEndpointPaymentPolicy(
        source="control_plane_exact",
        allowed_rails=allowed_rails,
        machine_payment_rails=machine_payment_rails,
        allows_subscription="subscription" in allowed_rails,
        pricing_rule_id=pricing_rule_id,
    )


def _parse_config_payload(payload: dict) -> RuntimePaymentPolicyConfig:
    if not isinstance(payload, dict):
        raise ValueError("Payment policy config payload must be an object.")

    defaults = _default_policy_config()
    policy = _extract_policy_section(payload)
    accepted_methods = policy.get("accepted_payment_methods")
    if not isinstance(accepted_methods, dict):
        accepted_methods = {}
    _raw_endpoint_policies = policy.get(
        "endpoint_payment_policies",
        payload.get("endpoint_payment_policies"),
    )
    _parsed_endpoint_policies = _normalize_endpoint_payment_policies(_raw_endpoint_policies)
    if _parsed_endpoint_policies:
        _defaults_by_key = {
            (p.path_pattern, p.method): p for p in defaults.endpoint_payment_policies
        }

        # Repair pass: control-plane entries sometimes carry a UUID as their
        # pricing_rule_id (an internal CP database key) rather than the semantic
        # api_pricing_rules.rule_name.  A UUID is NOT a valid rule_name:
        # resolve_economic_amounts() would query the DB, find nothing, and return
        # (0, 0, 0), producing a zero-amount x402 challenge.
        #
        # Similarly, CP entries may omit machine payment rails (e.g. mpp) that
        # are present in the authoritative defaults.
        #
        # For every CP entry that has a matching default, we backfill:
        #   1. pricing_rule_id  — replace UUID with the default's rule_name
        #   2. allowed_rails    — add any machine rails present in default but absent from CP
        #
        # Intentional CP decisions (valid semantic rule IDs, explicit rail exclusions
        # for non-machine rails) are preserved.
        def _repair_cp_policy(cp: EndpointPaymentPolicy) -> EndpointPaymentPolicy:
            default = _defaults_by_key.get((cp.path_pattern, cp.method))
            if default is None:
                return cp  # no matching default — keep CP entry as-is

            rule_id = cp.pricing_rule_id
            if (not rule_id or _is_uuid_like(rule_id)) and default.pricing_rule_id:
                logger.warning(
                    "CP pricing_rule_id %r for %s %s looks like an internal UUID; "
                    "backfilling from defaults: %r",
                    rule_id, cp.method, cp.path_pattern, default.pricing_rule_id,
                )
                rule_id = default.pricing_rule_id

            # Add machine rails that the default includes but the CP omitted
            cp_rail_set = set(cp.allowed_rails)
            added: list[str] = []
            for rail in default.allowed_rails:
                if rail in _MACHINE_PAYMENT_RAILS and rail not in cp_rail_set:
                    added.append(rail)
            if added:
                logger.warning(
                    "CP allowed_rails for %s %s missing machine rails %r; "
                    "backfilling from defaults.",
                    cp.method, cp.path_pattern, added,
                )

            if rule_id == cp.pricing_rule_id and not added:
                return cp  # nothing changed

            new_rails = tuple(cp.allowed_rails) + tuple(added)
            return EndpointPaymentPolicy(
                endpoint_id=cp.endpoint_id,
                path_pattern=cp.path_pattern,
                method=cp.method,
                allowed_rails=new_rails,
                pricing_rule_id=rule_id,
                machine_payments_enabled=cp.machine_payments_enabled,
            )

        _repaired = tuple(_repair_cp_policy(p) for p in _parsed_endpoint_policies)
        _cp_keys = {(p.path_pattern, p.method) for p in _repaired}
        _gap_policies = tuple(
            p for p in defaults.endpoint_payment_policies
            if (p.path_pattern, p.method) not in _cp_keys
        )
        endpoint_payment_policies = _repaired + _gap_policies
        if _gap_policies:
            logger.info(
                "Payment policy config: %d control-plane endpoint policies + %d hardcoded defaults for uncovered endpoints.",
                len(_repaired),
                len(_gap_policies),
            )
    else:
        logger.warning(
            "Payment policy config provided no endpoint_payment_policies; "
            "falling back to hardcoded defaults (%d entries).",
            len(defaults.endpoint_payment_policies),
        )
        endpoint_payment_policies = defaults.endpoint_payment_policies

    agent_required = accepted_methods.get("agent_pay_required")
    if isinstance(agent_required, dict):
        agent_required_default = str(agent_required.get("default") or "").strip() or defaults.accepted_payment_methods_agent_required_default
        agent_required_by_method = _normalize_string_map(
            agent_required.get("by_method"),
            default=defaults.accepted_payment_methods_agent_required_by_method,
        )
    else:
        agent_required_default = str(agent_required or "").strip() or defaults.accepted_payment_methods_agent_required_default
        agent_required_by_method = dict(defaults.accepted_payment_methods_agent_required_by_method)

    return RuntimePaymentPolicyConfig(
        source="control_plane",
        version=str(policy.get("version") or payload.get("version") or "").strip() or None,
        ttl_seconds=_normalize_int(
            policy.get("ttl_seconds", payload.get("ttl_seconds")),
            default=defaults.ttl_seconds,
        ),
        fetched_at=time.time(),
        environment=str(policy.get("environment") or payload.get("environment") or "").strip() or None,
        enabled_environment_rails=_normalize_enabled_rails(
            policy.get("environment_rail_enablement", payload.get("environment_rail_enablement"))
        ),
        pricing_rule_ids=_normalize_pricing_rule_ids(
            policy.get("pricing_rules", payload.get("pricing_rules"))
        ),
        endpoint_payment_policies=endpoint_payment_policies,
        free_metered_paths=_normalize_string_list(
            policy.get("free_metered_paths"),
            default=defaults.free_metered_paths,
        ),
        agent_pay_path_prefixes=_normalize_string_list(
            policy.get("agent_pay_path_prefixes"),
            default=defaults.agent_pay_path_prefixes,
        ),
        agent_pay_auth_bypass_methods=_normalize_string_list(
            policy.get("agent_pay_auth_bypass_methods"),
            default=defaults.agent_pay_auth_bypass_methods,
        ),
        enforcement_path_prefixes=_normalize_string_list(
            policy.get("enforcement_path_prefixes"),
            default=defaults.enforcement_path_prefixes,
        ),
        accepted_payment_methods_agent_required_default=agent_required_default,
        accepted_payment_methods_agent_required_by_method=agent_required_by_method,
        accepted_payment_methods_agent_optional=str(
            accepted_methods.get("agent_pay_optional") or defaults.accepted_payment_methods_agent_optional
        ).strip(),
        accepted_payment_methods_subscription=str(
            accepted_methods.get("subscription_v1") or defaults.accepted_payment_methods_subscription
        ).strip(),
        accepted_payment_methods_default=str(
            accepted_methods.get("default") or defaults.accepted_payment_methods_default
        ).strip(),
    )


def _fetch_runtime_payment_policy_config() -> RuntimePaymentPolicyConfig:
    config_url = _resolve_config_url()
    if not config_url:
        raise RuntimeError("No payment policy config URL is configured.")

    timeout_seconds = _DEFAULT_FETCH_TIMEOUT_SECONDS
    timeout_raw = os.getenv("PAYMENT_POLICY_CONFIG_TIMEOUT_SECONDS")
    if timeout_raw:
        try:
            timeout_seconds = max(float(timeout_raw), 0.1)
        except ValueError:
            timeout_seconds = _DEFAULT_FETCH_TIMEOUT_SECONDS

    with request.urlopen(config_url, timeout=timeout_seconds) as response:
        payload = json.load(response)

    return _parse_config_payload(payload)


def get_runtime_payment_policy_config(force_refresh: bool = False) -> RuntimePaymentPolicyConfig:
    global _cached_at, _cached_config, _last_known_good_config, _last_reported_fallback_reason

    with _cache_lock:
        if not force_refresh and _cached_config is not None:
            age_seconds = time.time() - _cached_at
            if age_seconds < _cached_config.ttl_seconds:
                return _cached_config

        try:
            fetched_config = _fetch_runtime_payment_policy_config()
        except RuntimeError as exc:
            fallback = _last_known_good_config or _cached_config or _default_policy_config()
            _cached_config = fallback
            _cached_at = time.time()
            if _last_reported_fallback_reason != "unconfigured":
                logger.info("Payment policy config not configured; using %s snapshot.", fallback.source)
                _last_reported_fallback_reason = "unconfigured"
            return fallback
        except (ValueError, error.URLError, error.HTTPError, OSError, json.JSONDecodeError) as exc:
            fallback = _last_known_good_config or _cached_config or _default_policy_config()
            if _last_reported_fallback_reason != "fetch_failed":
                logger.warning(
                    "Payment policy config fetch failed; using %s snapshot: %s",
                    fallback.source,
                    exc,
                )
                _last_reported_fallback_reason = "fetch_failed"
            _cached_config = fallback
            _cached_at = time.time()
            return fallback

        _cached_config = fetched_config
        _cached_at = time.time()
        _last_known_good_config = fetched_config
        if _last_reported_fallback_reason is not None:
            logger.info(
                "Payment policy config restored from control plane (was: %s). "
                "version=%s environment=%s endpoints=%d",
                _last_reported_fallback_reason,
                fetched_config.version,
                fetched_config.environment,
                len(fetched_config.endpoint_payment_policies),
            )
        else:
            logger.debug(
                "Payment policy config refreshed from control plane. "
                "version=%s environment=%s endpoints=%d",
                fetched_config.version,
                fetched_config.environment,
                len(fetched_config.endpoint_payment_policies),
            )
        _last_reported_fallback_reason = None
        return fetched_config


def is_free_metered_path(path: str) -> bool:
    config = get_runtime_payment_policy_config()
    return path in config.free_metered_paths


def get_effective_endpoint_payment_policy(
    path: str,
    method: str | None = None,
) -> EffectiveEndpointPaymentPolicy | None:
    return _build_effective_endpoint_policy(path, method)


def get_allowed_payment_rails_for_path(
    path: str,
    method: str | None = None,
) -> tuple[str, ...] | None:
    effective_policy = get_effective_endpoint_payment_policy(path, method)
    if effective_policy is None:
        return None
    return effective_policy.allowed_rails


def is_agent_pay_route(path: str, method: str | None = None) -> bool:
    effective_policy = get_effective_endpoint_payment_policy(path, method)
    if effective_policy is not None:
        return bool(effective_policy.machine_payment_rails)

    config = get_runtime_payment_policy_config()
    return any(path.startswith(prefix) for prefix in config.agent_pay_path_prefixes)


def get_agent_pay_auth_bypass_methods(path: str, method: str | None = None) -> tuple[str, ...]:
    effective_policy = get_effective_endpoint_payment_policy(path, method)
    if effective_policy is not None:
        return effective_policy.machine_payment_rails

    if not is_agent_pay_route(path, method):
        return ()

    config = get_runtime_payment_policy_config()
    return tuple(method.lower() for method in config.agent_pay_auth_bypass_methods)


def is_agent_pay_auth_candidate(
    path: str,
    payment_method: str | None,
    agent_id: str | None,
    *,
    method: str | None = None,
) -> bool:
    if not agent_id:
        return False

    normalized_method = (payment_method or "").strip().lower()
    if not normalized_method:
        return False

    return normalized_method in set(get_agent_pay_auth_bypass_methods(path, method))


def is_agent_pay_enforcement_path(path: str, method: str | None = None) -> bool:
    effective_policy = get_effective_endpoint_payment_policy(path, method)
    if effective_policy is not None:
        return bool(effective_policy.machine_payment_rails)

    config = get_runtime_payment_policy_config()
    return any(path.startswith(prefix) for prefix in config.enforcement_path_prefixes)


def get_accepted_payment_methods_for_path(
    path: str,
    pricing_rule_id: str | None,
    *,
    method: str | None = None,
    enforced_payment_method: str | None = None,
) -> str:
    config = get_runtime_payment_policy_config()
    normalized_method = (enforced_payment_method or "").strip().lower()
    endpoint_allowed_rails = get_allowed_payment_rails_for_path(path, method)

    if endpoint_allowed_rails is not None:
        return ",".join(endpoint_allowed_rails) if endpoint_allowed_rails else config.accepted_payment_methods_default

    if pricing_rule_id in ("stim_paid", "agent_pay_required"):
        if normalized_method:
            return config.accepted_payment_methods_agent_required_by_method.get(
                normalized_method,
                config.accepted_payment_methods_agent_required_default,
            )
        return config.accepted_payment_methods_agent_required_default

    if any(path.startswith(prefix) for prefix in config.agent_pay_path_prefixes):
        return config.accepted_payment_methods_agent_optional

    if path.startswith("/v1/"):
        return config.accepted_payment_methods_subscription

    return config.accepted_payment_methods_default
