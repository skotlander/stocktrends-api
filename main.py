# main.py
import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from discovery.inference_semantics import openapi_inference_extension
from discovery.provenance import AI_CONTEXT_PROVENANCE_TEXT, data_provenance, provenance_reference
from discovery.service_meta import SERVICE_POSITIONING
from middleware.request_id import RequestIdMiddleware
from middleware.api_key import ApiKeyMiddleware
from middleware.request_logger import RequestLoggerMiddleware
from middleware.metering import MeteringMiddleware

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
from routers.workflows import router as workflows_router
from routers.observability import router as observability_router

logging.basicConfig(level=logging.INFO)

APP_TITLE = "Stock Trends API"
APP_VERSION = "1.0.0"
APP_DESCRIPTION = SERVICE_POSITIONING

FREE_METERED_V1_PATHS = {
    "/ai/context",
}

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
DISCOVERY_START_HERE = "/v1/ai/tools"
DISCOVERY_SECONDARY = "/v1/ai/context"
DISCOVERY_DOCS = "/v1/docs"
DISCOVERY_OPENAPI = "/v1/openapi.json"
PUBLIC_API_BASE_URL = "https://api.stocktrends.com"
DEVELOPER_PORTAL_URL = "https://developer.stocktrends.com/"


def is_protected_v1_path(path: str) -> bool:
    return path not in FREE_METERED_V1_PATHS


def _discovery_links() -> dict[str, str]:
    return {
        "start_here": DISCOVERY_START_HERE,
        "secondary": DISCOVERY_SECONDARY,
        "docs": DISCOVERY_DOCS,
        "openapi": DISCOVERY_OPENAPI,
    }


def _absolute_url(path: str) -> str:
    return f"{PUBLIC_API_BASE_URL}{path}"


def _root_discovery_links() -> dict[str, str]:
    return {
        "developer_portal": DEVELOPER_PORTAL_URL,
        "start_here": _absolute_url(DISCOVERY_START_HERE),
        # Backward-compatible alias for clients that consumed the original root shape.
        "secondary": _absolute_url(DISCOVERY_SECONDARY),
        "secondary_context": _absolute_url(DISCOVERY_SECONDARY),
        "workflows": _absolute_url("/v1/workflows"),
        "pricing_catalog": _absolute_url("/v1/pricing/catalog"),
        "docs": _absolute_url(DISCOVERY_DOCS),
        "openapi": _absolute_url(DISCOVERY_OPENAPI),
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


def apply_api_key_security_to_openapi(v1_app: FastAPI) -> dict:
    if v1_app.openapi_schema:
        return v1_app.openapi_schema

    openapi_schema = get_openapi(
        title=f"{APP_TITLE} v1",
        version=APP_VERSION,
        description=APP_DESCRIPTION,
        routes=v1_app.routes,
    )

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

            operation["security"] = [
                {"ApiKeyAuth": []},
                {"BearerAuth": []},
            ]

            if path.startswith("/stim") or path.startswith("/indicators") or path.startswith("/prices") or path.startswith("/selections") or path.startswith("/stwr") or path.startswith("/agents") or path.startswith("/agent/screener") or path.startswith("/market") or path.startswith("/decision") or path.startswith("/portfolio") or path.startswith("/breadth/sector/") or path in ("/leadership/summary/latest", "/leadership/rotation/history", "/pricing", "/pricing/catalog", "/workflows", "/cost-estimate"):
                _ensure_parameter_refs(operation, agent_refs + payment_refs)

            inference_extension = openapi_inference_extension(path)
            if inference_extension:
                operation.update(inference_extension)

    v1_app.openapi_schema = openapi_schema
    return openapi_schema


app = FastAPI(title=APP_TITLE, version=APP_VERSION)
app.add_exception_handler(StarletteHTTPException, _discovery_http_exception_handler)


@app.get("/", include_in_schema=False)
def root():
    return {
        "message": "Start with the machine-readable tools manifest for agent discovery.",
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
        **_root_discovery_links(),
    }


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
                f"{APP_DESCRIPTION} Start with /v1/ai/tools, then use /v1/workflows, "
                "/v1/pricing/catalog, /v1/pricing, /v1/instruments/lookup, /v1/instruments/resolve, "
                "/v1/stwr/reports/catalog, and /v1/meta/* planning helpers before paid execution. "
                "Inspect x402 402 stocktrends_preview metadata before payment. Authentication or "
                "machine payment is required for protected data endpoints."
            ),
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
            "contact_email": "api@stocktrends.com",
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
v1.include_router(workflows_router)
v1.include_router(observability_router)

v1.openapi = lambda: apply_api_key_security_to_openapi(v1)

app.mount("/v1", v1)


@app.get("/health")
def health():
    return {"ok": True}
