from discovery.provenance import APPROXIMATE_OBSERVATION_COUNT, HISTORICAL_COVERAGE_START_YEAR


SERVICE_CONTACT_EMAIL = "api@stocktrends.com"
SERVICE_DEVELOPER_DOCS_URL = "https://developer.stocktrends.com/"

SERVICE_OPENAPI_GUIDANCE = (
    "Use Stock Trends when an agent needs processed, interpretation-ready market intelligence "
    "for trend classification, relative performance, unusual volume signals, market structure, "
    "probabilistic inference and forward-return distributions, ranking, or portfolio workflows; "
    "it is not a raw price data service. Start with GET /v1/ai/tools for machine-readable "
    "capabilities and GET /v1/workflows for task planning. Consult /v1/meta/inference for the "
    "provider-agnostic inference contract and /v1/meta/stim when interpreting outputs from "
    "ST-IM (Stock Trends Inference Model), the current baseline inference provider. "
    "Use symbol_exchange (for example, IBM-N) as the preferred instrument identifier where "
    "supported; use /v1/instruments/lookup or "
    "/v1/instruments/resolve when identity is uncertain. Treat forecasts and probabilities as "
    "conditional historical tendencies under uncertainty, not guarantees, price targets, or "
    "direct buy/sell commands. Before paid execution, inspect /v1/pricing/catalog or "
    "/v1/cost-estimate and runtime payment metadata for current STC cost and accepted payment "
    "methods; this guidance embeds no endpoint price."
)

SERVICE_POSITIONING = (
    "Autonomous portfolio intelligence API for AI agents. "
    "Agent-native probabilistic market intelligence infrastructure: "
    f"multi-decade Stock Trends classification history from {HISTORICAL_COVERAGE_START_YEAR} "
    f"with {APPROXIMATE_OBSERVATION_COUNT} observations, "
    "ST-IM (Stock Trends Inference Model) forward-return distributions, "
    "provider-agnostic inference metadata, "
    "market regime analysis, sector breadth confirmation, leadership rotation analytics, "
    "STIM Select probabilistic stock lists, portfolio construction and evaluation, "
    "and symbol decision scoring across 4, 13, and 40-week investment horizons. "
    "Built for agentic research workflows using x402 and MPP payment rails. "
    "Outputs are processed, ranked, and interpretation-ready — not raw price data."
)

DATASET_DESCRIPTION = (
    "Weekly structured market intelligence dataset covering North American equities and ETFs, "
    f"with records extending back to {HISTORICAL_COVERAGE_START_YEAR} and "
    f"{APPROXIMATE_OBSERVATION_COUNT} historical observations, "
    "including Stock Trends trend classification, trend persistence, trend maturity, relative "
    "strength, unusual volume signals, breadth, leadership, ST-IM (Stock Trends Inference Model) "
    "forward return distributions, provider-agnostic inference metadata, market regime analytics, "
    "and deterministic decision/portfolio workflows."
)
