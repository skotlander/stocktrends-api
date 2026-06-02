import hashlib
import logging
import os
from types import SimpleNamespace
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from sqlalchemy import text

from db import get_auth_engine
from metering.logger import log_auth_failure_event
from payments.policy_provider import (
    is_agent_pay_auth_candidate,
    is_agent_pay_enforcement_path,
    is_free_metered_path,
    is_public_stocktrends_path,
)

_ENABLE_AGENT_PAY = os.getenv("ENABLE_AGENT_PAY", "false").lower() == "true"


ALLOWED_SUBSCRIPTION_STATUSES = {"active", "trialing"}
logger = logging.getLogger("stocktrends_api.auth")


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def extract_api_key(request: Request) -> Optional[str]:
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key.strip()

    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()

    return None


def _normalize_header(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


class ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)

        self.public_paths = {
            "/",
            "/index.html",
            "/llms.txt",
            "/ai-dataset.json",
            "/tools.json",
            "/sitemap.xml",
            "/robots.txt",
            "/docs",
            "/v1/docs",
            "/v1/docs/",
            "/openapi.json",
            "/v1/openapi.json",
            "/v1/openapi.js",
            "/health",
            "/v1/pricing",
            "/v1/pricing/catalog",
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
            # Workflow catalog is public discovery — no auth required.
            "/v1/workflows",
            # Discovery endpoints — public like /v1/docs and /v1/openapi.json.
            "/v1/ai/context",
            "/v1/ai/tools",
            # Free proof-of-value endpoint for autonomous agent discovery.
            "/v1/ai/proof/market-edge",
        }

        self.public_prefixes = [
            "/dataset/",
            "/.well-known/",
            # Swagger UI sub-paths (oauth2-redirect, etc.) are public doc assets.
            "/v1/docs/",
            # Observability endpoints are internal/admin-only, guarded by
            # INTERNAL_OBSERVABILITY_SECRET in the router layer.  They must
            # not be gated by customer API keys — internal tooling has no
            # customer API key to present.
            "/v1/observability/",
        ]

    def _is_agent_pay_candidate(self, request: Request) -> bool:
        path = request.url.path
        agent_id = _normalize_header(request.headers.get("x-stocktrends-agent-id"))

        payment_method = (_normalize_header(request.headers.get("x-stocktrends-payment-method")) or "").lower()
        return is_agent_pay_auth_candidate(path, payment_method, agent_id, method=request.method)

    def _apply_agent_pay_context(self, request: Request) -> None:
        request.state.api_key_id = None
        request.state.customer_id = None
        request.state.subscription_id = None
        request.state.plan_code = None
        request.state.auth_mode = "agent_pay"
        request.state.actor_type = "external_customer"
        request.state.quota_limit = None
        request.state.auth_context = SimpleNamespace(
            api_key_id=None,
            customer_id=None,
            subscription_id=None,
            plan_code=None,
            actor_type="external_customer",
            quota_limit=None,
        )

    def _authenticate_api_key(self, path: str, raw_key: str) -> tuple[bool, dict]:
        key_hash = hash_api_key(raw_key)
        engine = get_auth_engine()

        with engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT
                        k.id,
                        k.customer_id,
                        k.subscription_id,
                        k.status,
                        k.revoked_at,
                        s.status AS subscription_status,
                        p.code AS plan_code,
                        p.active AS plan_active,
                        p.monthly_quota
                    FROM api_keys k
                    LEFT JOIN api_subscriptions s
                        ON k.subscription_id = s.id
                    LEFT JOIN api_plans p
                        ON s.plan_id = p.id
                    WHERE k.key_hash = :key_hash
                    LIMIT 1
                    """
                ),
                {"key_hash": key_hash},
            ).fetchone()

            if not result:
                return False, {"detail": "Invalid API key", "status_code": 401}

            key_id = result[0]
            customer_id = result[1]
            subscription_id = result[2]
            key_status = result[3]
            revoked_at = result[4]
            subscription_status = result[5]
            plan_code = result[6]
            plan_active = result[7]
            monthly_quota = result[8]

            if key_status != "active" or revoked_at is not None:
                return False, {"detail": "API key inactive", "status_code": 403}

            if not subscription_id:
                return False, {"detail": "No subscription linked to API key", "status_code": 403}

            if subscription_status not in ALLOWED_SUBSCRIPTION_STATUSES:
                return False, {
                    "detail": f"Subscription not active ({subscription_status})",
                    "status_code": 403,
                }

            if not plan_code:
                return False, {"detail": "No plan linked to subscription", "status_code": 403}

            if not plan_active:
                return False, {"detail": "Plan is inactive", "status_code": 403}

            if not self.is_plan_allowed(path, plan_code):
                return False, {
                    "detail": f"Plan '{plan_code}' does not allow this endpoint",
                    "status_code": 403,
                }

            conn.execute(
                text(
                    """
                    UPDATE api_keys
                    SET last_used_at = NOW()
                    WHERE id = :key_id
                    """
                ),
                {"key_id": key_id},
            )

            return True, {
                "api_key_id": key_id,
                "customer_id": customer_id,
                "subscription_id": subscription_id,
                "plan_code": plan_code,
                "actor_type": "external_customer",
                "monthly_quota": monthly_quota,
            }

    def _apply_auth_context(self, request: Request, auth: dict) -> None:
        request.state.api_key_id = auth["api_key_id"]
        request.state.customer_id = auth["customer_id"]
        request.state.subscription_id = auth["subscription_id"]
        request.state.plan_code = auth["plan_code"]
        request.state.auth_mode = "api_key"
        request.state.actor_type = auth["actor_type"]
        request.state.quota_limit = auth.get("monthly_quota")
        request.state.auth_context = SimpleNamespace(
            api_key_id=auth["api_key_id"],
            customer_id=auth["customer_id"],
            subscription_id=auth["subscription_id"],
            plan_code=auth["plan_code"],
            actor_type=auth["actor_type"],
            quota_limit=auth.get("monthly_quota"),
        )

    def _log_auth_failure(self, request: Request, *, status_code: int, error_code: str, notes: str | None) -> None:
        try:
            log_auth_failure_event(
                request=request,
                status_code=status_code,
                error_code=error_code,
                notes=notes,
            )
        except Exception as exc:
            logger.error("Auth failure log insert failed: %s", exc, exc_info=True)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Default request auth context
        request.state.auth_mode = "public"
        request.state.actor_type = "unknown"
        request.state.api_key_id = None
        request.state.customer_id = None
        request.state.subscription_id = None
        request.state.plan_code = None
        request.state.quota_limit = None

        # Public routes
        if (
            path in self.public_paths
            or is_public_stocktrends_path(path)
            or any(path.startswith(prefix) for prefix in self.public_prefixes)
        ):
            return await call_next(request)

        # OPTIONS preflight requests are handled by CORSMiddleware (outermost).
        # Pass through unconditionally so auth enforcement never blocks preflights.
        if request.method == "OPTIONS":
            return await call_next(request)

        raw_key = extract_api_key(request)

        # Free-metered routes:
        # allow anonymous access, but if an API key is supplied, resolve and attach customer context
        if is_free_metered_path(path):
            if raw_key:
                ok, auth = self._authenticate_api_key(path, raw_key)
                if not ok:
                    error_code = "auth_invalid_api_key" if auth["status_code"] == 401 else "auth_forbidden"
                    self._log_auth_failure(
                        request,
                        status_code=auth["status_code"],
                        error_code=error_code,
                        notes=auth["detail"],
                    )
                    return JSONResponse(
                        {"detail": auth["detail"]},
                        status_code=auth["status_code"],
                    )
                self._apply_auth_context(request, auth)
            else:
                request.state.auth_mode = "free_metered"
            return await call_next(request)

        # Lane B anonymous agent-pay entry:
        # allow through without an API key when the request clearly presents
        # as an agent payment attempt. Subscription callers can still use API keys normally.
        if self._is_agent_pay_candidate(request):
            self._apply_agent_pay_context(request)
            return await call_next(request)

        # Lane B path-based fallback: when agent pay is enabled, keyless requests on
        # enforcement-scoped paths are forwarded to MeteringMiddleware so the 402 challenge
        # flow can run. This handles the initial anonymous request before any agent-pay
        # headers are present. is_agent_pay_enforcement_path() does not check ENABLE_AGENT_PAY
        # itself, so the guard is applied explicitly here.
        # API-key holders are unaffected — `not raw_key` ensures they continue to normal auth.
        if _ENABLE_AGENT_PAY and not raw_key and is_agent_pay_enforcement_path(path, method=request.method):
            self._apply_agent_pay_context(request)
            return await call_next(request)

        # Protected API routes
        if path.startswith("/v1/"):
            if not raw_key:
                self._log_auth_failure(
                    request,
                    status_code=401,
                    error_code="auth_missing_api_key",
                    notes="Missing API key",
                )
                return JSONResponse(
                    {"detail": "Missing API key"},
                    status_code=401,
                )

            ok, auth = self._authenticate_api_key(path, raw_key)
            if not ok:
                error_code = "auth_invalid_api_key" if auth["status_code"] == 401 else "auth_forbidden"
                self._log_auth_failure(
                    request,
                    status_code=auth["status_code"],
                    error_code=error_code,
                    notes=auth["detail"],
                )
                return JSONResponse(
                    {"detail": auth["detail"]},
                    status_code=auth["status_code"],
                )

            self._apply_auth_context(request, auth)
            return await call_next(request)

        return await call_next(request)

    def is_plan_allowed(self, path: str, plan_code: str) -> bool:
        sandbox_plus = {"sandbox", "research", "pro", "enterprise"}
        research_plus = {"research", "pro", "enterprise"}

        if path.startswith("/v1/stim"):
            return plan_code in research_plus

        if path.startswith("/v1/"):
            return plan_code in sandbox_plus

        return True
