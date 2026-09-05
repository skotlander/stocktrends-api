# main.py
import logging
import threading

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from discovery.inference_semantics import openapi_inference_extension
from discovery.endpoint_metadata import (
    PRICING_CATALOG_URL,
    PUBLIC_API_BASE_URL,
    get_endpoint_metadata,
)
from discovery.provenance import AI_CONTEXT_PROVENANCE_TEXT, data_provenance, provenance_reference
from discovery.service_meta import (
    SERVICE_CONTACT_EMAIL,
    SERVICE_DEVELOPER_DOCS_URL,
    SERVICE_OPENAPI_GUIDANCE,
    SERVICE_POSITIONING,
)
from discovery.x402_discovery import (
    CANONICAL_DISCOVERY_PATH,
    CANONICAL_DISCOVERY_URL,
    X402DiscoverySystemicFailure,
    X402_DISCOVERY_ALIASES,
    build_x402_discovery,
)
from api.routing import (
    assert_payment_boundary_complete,
    install_payment_execution_boundary,
)
from middleware.request_id import RequestIdMiddleware
from middleware.api_key import (
    ApiKeyMiddleware,
    is_internal_admin_api_path,
    is_truly_public_api_path,
)
from middleware.request_logger import RequestLoggerMiddleware
from middleware.metering import MeteringMiddleware
from payments.challenge import (
    challenge_precondition_metadata,
    classify_early_challenge_route,
)
import payments.policy_provider as payment_policy
import payments.x402_contract as x402_contract
from payments.mpp import MPP_PAYMENT_CHANNEL_ID_HEADERS, MPP_REQUIRED_HEADERS

from routers.instruments import router as instruments_router
from routers.prices import router as prices_router
from routers.indicators import router as indicators_router
from routers.selections import router as selections_router
from routers.stim import router as stim_router
from routers.selections_published import router as selections_published_router
from routers.stwr import router as stwr_router
from routers.meta import router as meta_router
from routers.breadth import router as breadth_router
from routers.leadership import router as leadership_router
from routers.ai import router as ai_router
from routers.pricing import router as pricing_router
from routers.agents import router as agents_router  # ✅ NEW
from routers.screener import router as screener_router
from routers.market import router as market_router
from routers.decision import router as decision_router
from routers.portfolio import router as portfolio_router
from routers.stocktrends_portfolios import router as stocktrends_portfolios_router
from routers.stocktrends_strategies import router as stocktrends_strategies_router
from routers.intelligence import router as intelligence_router
from routers.workflows import router as workflows_router
from routers.observability import (
    INTERNAL_OBSERVABILITY_SECRET_HEADER,
    router as observability_router,
)

logging.basicConfig(level=logging.INFO)

APP_TITLE = "Stock Trends API"
APP_VERSION = "1.0.0"
APP_DESCRIPTION = SERVICE_POSITIONING

FREE_METERED_V1_PATHS = {
    "/ai/context",
}

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
DISCOVERY_START_HERE = "/v1/ai/tools"
DISCOVERY_X402 = CANONICAL_DISCOVERY_PATH
DISCOVERY_SECONDARY = "/v1/ai/context"
DISCOVERY_DOCS = "/v1/docs"
DISCOVERY_OPENAPI = "/v1/openapi.json"
X402_BROWSER_ALLOWED_ORIGINS = ["https://developer.stocktrends.com"]
X402_BROWSER_ALLOWED_HEADERS = [
    "Authorization",
    "Content-Type",
    *x402_contract.X402_PROOF_HEADERS,
    "X-StockTrends-Agent-Id",
    "X-StockTrends-Agent-Type",
    "X-StockTrends-Agent-Vendor",
    "X-StockTrends-Agent-Version",
    "X-StockTrends-Challenge-Mode",
    "X-StockTrends-Payment-Amount",
    "X-StockTrends-Payment-Method",
    "X-StockTrends-Payment-Network",
    "X-StockTrends-Payment-Reference",
    "X-StockTrends-Payment-Token",
    "X-StockTrends-Request-Purpose",
    "X-StockTrends-Session-Id",
]
X402_BROWSER_EXPOSED_HEADERS = [
    "PAYMENT-REQUIRED",
    "PAYMENT-RESPONSE",
    "X-StockTrends-Accepted-Payment-Methods",
    "X-StockTrends-Effective-Price-USD",
    "X-StockTrends-Payment-Required",
    "X-StockTrends-Pricing-Rule",
    "X-StockTrends-Selected-Payment-Rail",
    "X-StockTrends-STC-Cost",
]
_OPENAPI_CONTRACT_CACHE_LOCK = threading.Lock()


def is_protected_v1_path(path: str) -> bool:
    return path not in FREE_METERED_V1_PATHS


def _discovery_links() -> dict[str, str]:
    return {
        "x402_discovery": DISCOVERY_X402,
        "start_here": DISCOVERY_START_HERE,
        "secondary": DISCOVERY_SECONDARY,
        "docs": DISCOVERY_DOCS,
        "openapi": DISCOVERY_OPENAPI,
    }


def _absolute_url(path: str) -> str:
    return f"{PUBLIC_API_BASE_URL}{path}"


def _root_discovery_links() -> dict[str, str]:
    return {
        "developer_portal": SERVICE_DEVELOPER_DOCS_URL,
        "x402_discovery": CANONICAL_DISCOVERY_URL,
        "start_here": _absolute_url(DISCOVERY_START_HERE),
        # Backward-compatible alias for clients that consumed the original root shape.
        "secondary": _absolute_url(DISCOVERY_SECONDARY),
        "secondary_context": _absolute_url(DISCOVERY_SECONDARY),
        "workflows": _absolute_url("/v1/workflows"),
        "pricing_catalog": _absolute_url("/v1/pricing/catalog"),
        "docs": _absolute_url(DISCOVERY_DOCS),
        "openapi": _absolute_url("/openapi.json"),
    }


def _not_found_payload(request: Request, detail: str) -> dict[str, str]:
    return {
        "detail": detail,
        "requested_path": request.url.path,
        **_discovery_links(),
    }


async def _discovery_http_exception_handler(request: Request, exc: StarletteHTTPException):
    is_unmatched_route = (
        exc.status_code == 404
        and request.scope.get("route") is None
    )

    if not is_unmatched_route:
        return await http_exception_handler(request, exc)

    detail = exc.detail if isinstance(exc.detail, str) else "Not Found"
    return JSONResponse(
        status_code=404,
        content=_not_found_payload(request, detail),
    )


def _ensure_parameter_refs(operation: dict, refs: list[str]) -> None:
    existing = operation.setdefault("parameters", [])
    existing_refs = {
        param.get("$ref")
        for param in existing
        if isinstance(param, dict) and "$ref" in param
    }

    for ref in refs:
        if ref not in existing_refs:
            existing.append({"$ref": ref})


def _canonical_header_name(header_name: str) -> str:
    """Format case-insensitive runtime header constants for OpenAPI display."""
    canonical_parts = {
        "id": "Id",
        "mpp": "MPP",
        "stocktrends": "StockTrends",
        "x": "X",
    }
    return "-".join(
        canonical_parts.get(part, part.title())
        for part in header_name.lower().split("-")
    )


def _mpp_security_scheme_name(header_name: str) -> str:
    return "MPP" + "".join(_canonical_header_name(header_name).split("-"))


def _x402_security_scheme_name(header_name: str) -> str:
    normalized = header_name.lower()
    if normalized == "payment-signature":
        return "X402PaymentSignature"
    if normalized == "x-payment":
        return "X402LegacyPayment"
    return "X402PaymentProof" + "".join(
        character for character in header_name.title() if character.isalnum()
    )


def apply_api_key_security_to_openapi(v1_app: FastAPI) -> dict:
    with _OPENAPI_CONTRACT_CACHE_LOCK:
        return _apply_api_key_security_to_openapi_locked(v1_app)


def _apply_api_key_security_to_openapi_locked(v1_app: FastAPI) -> dict:
    """Generate or reuse the schema while holding the contract-cache lock."""
    runtime_policy = payment_policy.get_runtime_payment_policy_config()
    policy_fingerprint = payment_policy.payment_policy_contract_fingerprint(
        runtime_policy
    )
    payment_contract_key = (
        policy_fingerprint,
        x402_contract.x402_contract_fingerprint(),
        tuple(MPP_REQUIRED_HEADERS),
        tuple(MPP_PAYMENT_CHANNEL_ID_HEADERS),
        INTERNAL_OBSERVABILITY_SECRET_HEADER,
    )
    if (
        v1_app.openapi_schema
        and getattr(v1_app.state, "openapi_payment_contract_key", None)
        == payment_contract_key
    ):
        return v1_app.openapi_schema

    openapi_schema = get_openapi(
        title=f"{APP_TITLE} v1",
        version=APP_VERSION,
        description=APP_DESCRIPTION,
        routes=v1_app.routes,
    )

    openapi_schema["info"]["x-guidance"] = SERVICE_OPENAPI_GUIDANCE
    openapi_schema["info"]["contact"] = {"email": SERVICE_CONTACT_EMAIL}
    openapi_schema["externalDocs"] = {"url": SERVICE_DEVELOPER_DOCS_URL}
    openapi_schema["servers"] = [
        {"url": "/v1", "description": "Stock Trends API v1"},
    ]
    openapi_schema["x-stocktrends-provenance-summary"] = AI_CONTEXT_PROVENANCE_TEXT
    openapi_schema["x-stocktrends-data-provenance"] = data_provenance()

    components = openapi_schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    parameters = components.setdefault("parameters", {})

    security_schemes["ApiKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }

    security_schemes["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
    }

    security_schemes["InternalSecretAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": INTERNAL_OBSERVABILITY_SECRET_HEADER,
        "description": (
            "Internal/admin authentication for observability operations. "
            "A customer API key does not satisfy this requirement."
        ),
    }

    x402_security_schemes_by_header = {
        header_name: _x402_security_scheme_name(header_name)
        for header_name in x402_contract.X402_PROOF_HEADERS
    }
    for header_name, scheme_name in x402_security_schemes_by_header.items():
        security_schemes[scheme_name] = {
            "type": "apiKey",
            "in": "header",
            "name": header_name,
            "description": (
                "Runtime-recognized x402 payment proof supplied when retrying "
                "an otherwise serviceable challenged request."
            ),
        }

    mpp_required_headers = tuple(
        _canonical_header_name(header_name) for header_name in MPP_REQUIRED_HEADERS
    )
    mpp_channel_id_headers = tuple(
        _canonical_header_name(header_name)
        for header_name in MPP_PAYMENT_CHANNEL_ID_HEADERS
    )
    mpp_security_schemes_by_header = {
        header_name: _mpp_security_scheme_name(header_name)
        for header_name in (*mpp_required_headers, *mpp_channel_id_headers)
    }
    for header_name, scheme_name in mpp_security_schemes_by_header.items():
        security_schemes[scheme_name] = {
            "type": "apiKey",
            "in": "header",
            "name": header_name,
            "description": "Runtime-recognized MPP session authorization header.",
        }

    # Agent headers
    parameters["StockTrendsAgentId"] = {
        "name": "X-StockTrends-Agent-Id",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    parameters["StockTrendsAgentType"] = {
        "name": "X-StockTrends-Agent-Type",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    parameters["StockTrendsAgentVendor"] = {
        "name": "X-StockTrends-Agent-Vendor",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    parameters["StockTrendsAgentVersion"] = {
        "name": "X-StockTrends-Agent-Version",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    parameters["StockTrendsRequestPurpose"] = {
        "name": "X-StockTrends-Request-Purpose",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    parameters["StockTrendsSessionId"] = {
        "name": "X-StockTrends-Session-Id",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    # Payment headers
    parameters["StockTrendsPaymentMethod"] = {
        "name": "X-StockTrends-Payment-Method",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    parameters["StockTrendsPaymentNetwork"] = {
        "name": "X-StockTrends-Payment-Network",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    parameters["StockTrendsPaymentToken"] = {
        "name": "X-StockTrends-Payment-Token",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    parameters["StockTrendsPaymentReference"] = {
        "name": "X-StockTrends-Payment-Reference",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    parameters["StockTrendsPaymentAmount"] = {
        "name": "X-StockTrends-Payment-Amount",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
    }

    agent_refs = [
        "#/components/parameters/StockTrendsAgentId",
        "#/components/parameters/StockTrendsAgentType",
        "#/components/parameters/StockTrendsAgentVendor",
        "#/components/parameters/StockTrendsAgentVersion",
        "#/components/parameters/StockTrendsRequestPurpose",
        "#/components/parameters/StockTrendsSessionId",
    ]

    payment_refs = [
        "#/components/parameters/StockTrendsPaymentMethod",
        "#/components/parameters/StockTrendsPaymentNetwork",
        "#/components/parameters/StockTrendsPaymentToken",
        "#/components/parameters/StockTrendsPaymentReference",
        "#/components/parameters/StockTrendsPaymentAmount",
    ]

    for path, path_item in openapi_schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue

            external_path = f"/v1{path}"
            endpoint_policy = payment_policy.get_effective_endpoint_payment_policy_from_config(
                runtime_policy,
                external_path,
                method.upper(),
            )
            if endpoint_policy is None and is_internal_admin_api_path(external_path):
                operation["security"] = [{"InternalSecretAuth": []}]
            elif endpoint_policy is None and is_truly_public_api_path(external_path):
                operation["security"] = []
            elif endpoint_policy is None:
                operation["security"] = [
                    {"ApiKeyAuth": []},
                    {"BearerAuth": []},
                ]
            else:
                operation_security = []
                if endpoint_policy.allows_subscription:
                    operation_security.extend(
                        [{"ApiKeyAuth": []}, {"BearerAuth": []}]
                    )
                if "x402" in endpoint_policy.machine_payment_rails:
                    operation_security.extend(
                        [
                            {scheme_name: []}
                            for scheme_name in x402_security_schemes_by_header.values()
                        ]
                    )
                if "mpp" in endpoint_policy.machine_payment_rails:
                    required_schemes = {
                        mpp_security_schemes_by_header[header_name]: []
                        for header_name in mpp_required_headers
                    }
                    for channel_header in mpp_channel_id_headers:
                        operation_security.append(
                            {
                                **required_schemes,
                                mpp_security_schemes_by_header[channel_header]: [],
                            }
                        )
                operation["security"] = operation_security
                payment_extension = {
                    "requires_payment": True,
                    "supported_rails": list(endpoint_policy.allowed_rails),
                    "pricing_rule_id": endpoint_policy.pricing_rule_id,
                    "pricing_catalog_url": PRICING_CATALOG_URL,
                    "x402_discovery_url": CANONICAL_DISCOVERY_URL,
                    "anonymous_challenge_supported": (
                        "x402" in endpoint_policy.machine_payment_rails
                    ),
                    # Per-operation, and rendered from the same classifier the
                    # request path consults, so the published contract cannot
                    # claim a precondition the runtime does not apply.  The
                    # `challenge_lifecycle` block names the resource's class —
                    # fixed-price, availability-gated or parameterized — so an
                    # agent is never left inferring an exception from a global
                    # boolean.
                    **challenge_precondition_metadata(
                        classify_early_challenge_route(
                            external_path,
                            method.upper(),
                            endpoint_policy=endpoint_policy,
                            route_template=external_path,
                        )
                    ),
                }
                payment_extension["challenge_lifecycle"] = {
                    key: payment_extension[key]
                    for key in (
                        "challenge_class",
                        "serviceable_request_required_before_challenge",
                        "serviceable_request_required_before_settlement",
                        "bare_canonical_probe_returns_challenge",
                        "availability_gate_precedes_challenge",
                        "parameterized_resource",
                    )
                }
                if "x402" in endpoint_policy.machine_payment_rails:
                    payment_extension["x402_version"] = x402_contract.X402_VERSION
                    payment_extension["x402_proof_headers"] = list(
                        x402_contract.X402_PROOF_HEADERS
                    )
                    payment_extension["x402_security_schemes_by_header"] = dict(
                        x402_security_schemes_by_header
                    )
                if "mpp" in endpoint_policy.machine_payment_rails:
                    payment_extension["mpp"] = {
                        "authorization_model": "session",
                        "required_headers": list(mpp_required_headers),
                        "required_one_of": {
                            "payment_channel_id_headers": list(mpp_channel_id_headers),
                        },
                        "canonical_payment_channel_id_header": mpp_channel_id_headers[0],
                        "legacy_payment_channel_id_headers": list(
                            mpp_channel_id_headers[1:]
                        ),
                        "security_schemes_by_header": dict(
                            mpp_security_schemes_by_header
                        ),
                        "uses_x402_challenge_flow": False,
                    }
                operation["x-stocktrends-payment"] = payment_extension

            endpoint_metadata = get_endpoint_metadata(external_path, method.upper())
            if endpoint_metadata and isinstance(
                endpoint_metadata.get("safe_example_request"), dict
            ):
                operation["x-stocktrends-safe-example-request"] = endpoint_metadata[
                    "safe_example_request"
                ]

            if path.startswith("/stim") or path.startswith("/indicators") or path.startswith("/prices") or path.startswith("/selections") or path.startswith("/stwr") or path.startswith("/agents") or path.startswith("/agent/screener") or path.startswith("/market") or path.startswith("/decision") or path.startswith("/portfolio") or path.startswith("/stocktrends") or path.startswith("/breadth/sector/") or path.startswith("/intelligence/guidance") or path.startswith("/intelligence/research") or path in ("/leadership/summary/latest", "/leadership/rotation/history", "/pricing", "/pricing/catalog", "/workflows", "/cost-estimate"):
                _ensure_parameter_refs(operation, agent_refs + payment_refs)

            inference_extension = openapi_inference_extension(path)
            if inference_extension:
                operation.update(inference_extension)

    v1_app.openapi_schema = openapi_schema
    v1_app.state.openapi_payment_policy_fingerprint = policy_fingerprint
    v1_app.state.openapi_payment_contract_key = payment_contract_key
    return openapi_schema


app = FastAPI(title=APP_TITLE, version=APP_VERSION)
app.add_exception_handler(StarletteHTTPException, _discovery_http_exception_handler)


@app.get("/", include_in_schema=False)
def root():
    return {
        "message": (
            "Use x402_discovery for payable-resource discovery, start_here for task "
            "discovery, and the canonical OpenAPI contract for exact request schemas."
        ),
        "description": APP_DESCRIPTION,
        "provenance_reference": provenance_reference(),
        "planning_helpers": [
            _absolute_url("/v1/cost-estimate"),
            _absolute_url("/v1/workflows"),
            _absolute_url("/v1/pricing/catalog"),
            _absolute_url("/v1/instruments/lookup"),
            _absolute_url("/v1/instruments/resolve"),
            _absolute_url("/v1/stwr/reports/catalog"),
            _absolute_url("/v1/meta/indicators"),
            _absolute_url("/v1/meta/inference"),
            _absolute_url("/v1/meta/stim"),
            _absolute_url("/v1/meta/stwr"),
            _absolute_url("/v1/leadership/definitions"),
            _absolute_url("/v1/ai/proof/market-edge"),
        ],
        # Public evidence resources, named separately from planning helpers so a
        # client evaluating the service can find them without crawling the tools
        # manifest. Each family has its own methodology and limitations; see
        # /v1/ai/context "evidence" for the separated map.
        "evidence": [
            _absolute_url("/v1/selections/stim-select/outcomes/summary"),
            _absolute_url("/v1/stocktrends/portfolios"),
            _absolute_url("/v1/stocktrends/strategies"),
        ],
        "evidence_map": _absolute_url("/v1/ai/context"),
        # Listed apart from evidence: a static synthetic illustration of response
        # structure, which measures nothing.
        "illustrative_capability_example": _absolute_url("/v1/ai/proof/market-edge"),
        **_root_discovery_links(),
    }


@app.get(X402_DISCOVERY_ALIASES[0], include_in_schema=False)
@app.get(X402_DISCOVERY_ALIASES[1], include_in_schema=False)
@app.get(X402_DISCOVERY_ALIASES[2], include_in_schema=False)
@app.get(X402_DISCOVERY_ALIASES[3], include_in_schema=False)
def x402_discovery():
    try:
        return JSONResponse(build_x402_discovery(strict=False))
    except X402DiscoverySystemicFailure:
        return JSONResponse(
            status_code=503,
            content={"detail": "x402 discovery is temporarily unavailable"},
        )


@app.get("/llms.txt", include_in_schema=False)
def llms_txt():
    return FileResponse("static/llms.txt", media_type="text/plain")

@app.get("/.well-known/ai-plugin.json", include_in_schema=False)
def ai_plugin():
    return JSONResponse(
        {
            "schema_version": "v1",
            "name_for_human": "Stock Trends API",
            "name_for_model": "stock_trends_api",
            "description_for_human": APP_DESCRIPTION,
            "description_for_model": (
                f"{APP_DESCRIPTION} Start with /.well-known/x402 for payable resources, "
                "then use /v1/ai/tools and /v1/workflows, "
                "/v1/pricing/catalog, /v1/pricing, /v1/instruments/lookup, /v1/instruments/resolve, "
                "/v1/stwr/reports/catalog, and /v1/meta/* planning helpers before paid execution. "
                "An unpaid probe of an eligible fixed-price payable resource returns its 402 payment "
                "contract; availability-gated and parameterized resources resolve availability or "
                "path parameters first. Construct a serviceable request before paying. Authentication or "
                "machine payment is required for protected data endpoints."
            ),
            "x_stocktrends_discovery": {
                "x402": CANONICAL_DISCOVERY_URL,
                "tools": "https://api.stocktrends.com/v1/ai/tools",
                "openapi": "https://api.stocktrends.com/v1/openapi.json",
                "workflows": "https://api.stocktrends.com/v1/workflows",
                "pricing_catalog": "https://api.stocktrends.com/v1/pricing/catalog",
            },
            "x_stocktrends_access": {
                "public_discovery_requires_api_key": False,
                "subscription_auth": ["X-API-Key", "Authorization: Bearer"],
                "machine_payment_rails": ["x402", "mpp"],
                "x402_proof_headers": list(x402_contract.X402_PROOF_HEADERS),
                # Qualified deliberately: the relaxed precondition holds for
                # recognized fixed-price resources, not universally.
                # Availability-gated Intelligence artifact routes resolve
                # availability first, and parameterized routes have no bare
                # canonical URL to probe.  Per-resource truth is published in
                # the OpenAPI `x-stocktrends-payment.challenge_lifecycle` block
                # and in /.well-known/x402 `resources[].challenge_lifecycle`.
                "serviceable_request_required_before_challenge_scope": (
                    "eligible recognized fixed-price resources only"
                ),
                "serviceable_request_required_before_challenge_for_fixed_price": False,
                "serviceable_request_required_before_settlement": True,
                "per_resource_lifecycle": {
                    "openapi": "https://api.stocktrends.com/v1/openapi.json",
                    "x402_discovery": CANONICAL_DISCOVERY_URL,
                },
            },
            "data_provenance": data_provenance(),
            "auth": {
                "type": "api_key",
                "in": "header",
                "name": "X-API-Key",
            },
            "api": {
                "type": "openapi",
                "url": "https://api.stocktrends.com/v1/openapi.json",
                "is_user_authenticated": True,
            },
            "logo_url": "https://stocktrends.com/images/ST-logo2.gif",
            "contact_email": SERVICE_CONTACT_EMAIL,
            "legal_info_url": "https://stocktrends.com/stock-trends-data-license",
        }
    )

@app.get("/tools.json", include_in_schema=False)
def tools_json():
    return FileResponse("static/tools.json", media_type="application/json")

# Middleware (order matters)
app.add_middleware(RequestLoggerMiddleware)
app.add_middleware(MeteringMiddleware)
app.add_middleware(ApiKeyMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=X402_BROWSER_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    allow_headers=X402_BROWSER_ALLOWED_HEADERS,
    expose_headers=X402_BROWSER_EXPOSED_HEADERS,
)


# v1 app
v1 = FastAPI(
    title=f"{APP_TITLE} v1",
    version=APP_VERSION,
    docs_url="/docs",
    openapi_url="/openapi.json",
)
v1.add_exception_handler(StarletteHTTPException, _discovery_http_exception_handler)

# Routers
v1.include_router(instruments_router)
v1.include_router(prices_router)
v1.include_router(indicators_router)
v1.include_router(selections_router)
v1.include_router(stim_router)
v1.include_router(selections_published_router)
v1.include_router(stwr_router)
v1.include_router(meta_router)
v1.include_router(breadth_router)
v1.include_router(leadership_router)
v1.include_router(ai_router)
v1.include_router(pricing_router)
v1.include_router(agents_router)  # ✅ NEW
v1.include_router(screener_router)
v1.include_router(market_router)
v1.include_router(decision_router)
v1.include_router(portfolio_router)
v1.include_router(stocktrends_portfolios_router)
v1.include_router(stocktrends_strategies_router)
v1.include_router(intelligence_router)
v1.include_router(workflows_router)
v1.include_router(observability_router)

# Payment execution boundary — must be installed after every router is included.
# Each APIRoute builds its parameter model and request handler at construction,
# so the seam is applied to the finished routes rather than to a route class.
# The wrapper is inert unless MeteringMiddleware publishes a payment gate.
_WRAPPED_V1_ROUTES = install_payment_execution_boundary(v1)
if _WRAPPED_V1_ROUTES < 1:
    raise RuntimeError(
        "payment execution boundary installed on 0 routes; the v1 route surface "
        "is empty or was not built before installation"
    )

# Coverage is verified, not assumed.  A route that reaches the surface without
# the boundary can serve paid work with no gate, so initialization fails rather
# than starting an application that would do that silently.
_GUARDED_V1_ROUTES = assert_payment_boundary_complete(v1, expected_minimum=20)

v1.openapi = lambda: apply_api_key_security_to_openapi(v1)


def canonical_openapi() -> dict:
    return v1.openapi()


app.openapi = canonical_openapi
app.mount("/v1", v1)


@app.get("/health")
def health():
    return {"ok": True}
