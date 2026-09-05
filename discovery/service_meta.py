from discovery.provenance import APPROXIMATE_OBSERVATION_COUNT, HISTORICAL_COVERAGE_START_YEAR


SERVICE_CONTACT_EMAIL = "api@stocktrends.com"
SERVICE_DEVELOPER_DOCS_URL = "https://developer.stocktrends.com/"

# ---------------------------------------------------------------------------
# Canonical neutral acquisition-evaluation guidance.
#
# One definition, referenced by the OpenAPI service guidance, /v1/ai/context,
# /v1/ai/tools, the compact x402 preview, and the static manifests.  Restating
# it independently on each surface is how those surfaces drift apart.
#
# It is procedural throughout: it tells a client HOW to evaluate, and never what
# to conclude.  It must not acquire language asserting that Stock Trends is
# valuable, that it improves any result, or that anything should be purchased.
# ---------------------------------------------------------------------------
SERVICE_EVALUATION_GUIDANCE = (
    "When deciding whether additional Stock Trends information is worth acquiring for a "
    "task, inspect the public resources relevant to that task before deciding: what is "
    "offered (GET /.well-known/x402 for payable resources, GET /v1/ai/tools, "
    "GET /v1/workflows), how it should be interpreted "
    "(GET /v1/meta/inference, GET /v1/meta/stim, GET /v1/meta/indicators), what evidence "
    "is available (GET /v1/selections/stim-select/outcomes/summary, "
    "GET /v1/stocktrends/portfolios, GET /v1/ai/proof/market-edge), and what it costs "
    "(GET /v1/pricing/catalog, GET /v1/cost-estimate). A service description is not "
    "methodology, and methodology is not evidence; read each from its own source. "
    "Availability is not evidence of usefulness. Select only the resources relevant to "
    "the task, and base the acquisition decision on the material actually inspected."
)

# Compact form for byte-sensitive surfaces (OpenAPI info.x-guidance, x402
# challenge previews).  Same procedure, fewer words; it is a pointer to the
# canonical statement, not a second version of it.
SERVICE_EVALUATION_GUIDANCE_SUMMARY = (
    "When deciding whether additional Stock Trends information is worth acquiring for a "
    "task, first inspect the relevant public resources: capabilities "
    "(/.well-known/x402, /v1/ai/tools, /v1/workflows), interpretation semantics "
    "(/v1/meta/inference, /v1/meta/stim, "
    "/v1/meta/indicators), available evidence "
    "(/v1/selections/stim-select/outcomes/summary, /v1/stocktrends/portfolios, "
    "/v1/ai/proof/market-edge), and cost (/v1/pricing/catalog, /v1/cost-estimate). "
    "Treat those as separate categories, do not infer usefulness from availability, "
    "and decide from what was actually inspected."
)

SERVICE_EVALUATION_GUIDANCE_SOURCE = "/v1/ai/context"

# Shortest form, for the x402 challenge preview.  That body is size-sensitive
# and is emitted at the moment a client is deciding whether to pay, so it gets a
# pointer to the canonical statement rather than a copy of it.
SERVICE_EVALUATION_GUIDANCE_POINTER = (
    "Before deciding whether to acquire this data, inspect the task-relevant public "
    "capability (/.well-known/x402), interpretation, evidence, and pricing resources. "
    "Those are separate "
    "categories, availability is not evidence of usefulness, and none of them require "
    f"payment. Full statement: {SERVICE_EVALUATION_GUIDANCE_SOURCE} "
    "(acquisition_evaluation_guidance)."
)

# How Stock Trends positions itself relative to a consumer's own process.  This
# is a description of role, not a claim of benefit: it tells a client what kind
# of thing this is so the client can judge fit, and it deliberately stops short
# of asserting that the combination performs better.
SERVICE_AUGMENTATION_ROLE = (
    "Stock Trends supplies independently derived market context intended to be combined "
    "with the consumer's own strategy, research process, forecasting model, or agent "
    "objective. It is designed to augment an existing analytical process rather than to "
    "replace it, and it does not supply the consumer's objective, constraints, or "
    "decision rule. Whether the added context is useful for a given task is for the "
    "consumer to determine from the methodology and evidence resources."
)

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
    "methods; this guidance embeds no endpoint price. "
    + SERVICE_EVALUATION_GUIDANCE_SUMMARY
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
