# Stock Trends API

##  Agent-Native Financial Intelligence API

The Stock Trends API is a **financial intelligence platform and agent-native monetization system** built on:

* FastAPI
* unified pricing (STC — Stock Trends Credits)
* multi-rail payments (subscription, x402, MPP)
* full request metering and billing

This is not just a data API.

It is:

 a **pricing engine**  
 a **payment system**  
 a **programmable financial layer for agents**

---

##  Quick Start (2 Minutes)

### 1. Make your first request

```bash
curl -X POST https://api.stocktrends.com/v1/decision/evaluate_symbol \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "AAPL",
    "exchange": "Q"
  }'
```

---

### 2. Example Response

```json
{
  "symbol": "AAPL",
  "exchange": "Q",
  "decision": "OUTPERFORM",
  "confidence": 0.62,
  "time_horizon": "13-week"
}
```

---

### 3. Payment

If payment is required:

* API returns an x402 payment request
* agents complete payment automatically
* request is fulfilled

---

##  Pricing Model (STC)

STC (Stock Trends Credits) is the **unified pricing unit** of the API.

### Key Principle

 **STC is an internal consumption unit**

### Current Policy

 **1 STC ˜ $1.00 USD**

This is a **configurable pricing policy**, not a fixed peg.

---

##  Example Endpoint Costs

| Endpoint | STC | Approx USD |
|---|---:|---:|
| `/stim/latest` | 0.0025 | $0.0025 |
| `/prices/latest` | 0.0025 | $0.0025 |
| `/agent/screener/top` | 0.5 | $0.50 |
| `/portfolio/construct` | 1.0 | $1.00 |

---

##  How Payment Works

### Subscription

* purchase STC in advance
* spend STC per request

### x402

* pay per request automatically
* no account required

#### Important

x402 responses include two price formats:

| Field | Meaning |
|---|---|
| `amount_usd` | human-readable USD price |
| `amount` | token base units |

Example:

```json
{
  "amount_usd": "0.500000",
  "amount": "500000"
}
```

 `500000` = `0.5 USDC` with 6 decimals, **not $500,000**.

### MPP

* session-based payments
* optimized for high-frequency agents
* funded via external payment rails

---

##  Core Concepts

### STC

* all endpoints are priced in STC
* STC is the single source of pricing truth
* payment rails convert into STC

### Multi-Rail Payments

Supported:

* Subscription
* x402
* MPP

Planned:

* STOK token incentives

---

##  Key Endpoints

### Decision Engine

```text
POST /v1/decision/evaluate_symbol
POST /v1/portfolio/evaluate
POST /v1/portfolio/construct
```

### Official Stock Trends Portfolios

```text
GET /v1/stocktrends/portfolios
GET /v1/stocktrends/portfolios/{port_id}
GET /v1/stocktrends/portfolios/{port_id}/returns
GET /v1/stocktrends/portfolios/{port_id}/summary
GET /v1/stocktrends/portfolios/{port_id}/positions/history
GET /v1/stocktrends/strategies
GET /v1/stocktrends/strategies/{strategy_id}
GET /v1/stocktrends/portfolios/{port_id}/strategy
```

These routes expose official Stock Trends model portfolio metadata and returns
history, a compact public history summary, and historical closed-position
records as public/free discovery data. The summary includes Stock Trends
annualized ROI on average invested capital. Current live holdings are
intentionally excluded from the public portfolio history surface.

Stock Trends strategy metadata describes the declared buy/sell rules and
economic assumptions behind official model portfolios. Strategy conditions are
exposed as legacy metadata for provenance and verification. They are not
executable query endpoints and do not return current matching stocks or current
live holdings.

### Market Intelligence

```text
GET /v1/stim/latest
GET /v1/indicators/latest
GET /v1/prices/latest
GET /v1/selections/latest
GET /v1/breadth/sector/latest
```

### Public ST-IM Select Signal Outcomes

```text
GET /v1/selections/stim-select/outcomes/summary
```

Public/free aggregate historical outcome evidence for observations meeting
Stock Trends Inference Model Select criteria. The legacy `outcomes` block uses
mature realized 13-week forward returns from `st_data.fpr_chg13` and remains
backward-compatible. Default no-date responses also expose `outcomes_by_horizon`
for realized 4-week, 13-week, and 40-week fields: `fpr_chg4`, `fpr_chg13`, and
`fpr_chg40`. It does not expose current selections, current matching symbols, or
individual historical symbols.

When both `start_date` and `end_date` are omitted, the endpoint applies a
trailing 10-year window ending at the latest mature outcome date and returns
`filters.default_window_applied: true` with the applied dates. This default
summary is served from the persistent historical summary table
`stweekly.stim_select_outcome_summary`; the API does not create or populate this
table during request handling. The response provenance includes `generated_at`
and `source_latest_mature_weekdate` so clients can judge freshness.

Supported seeded no-date summary combinations are the all-exchange summary with
`limit_rank` omitted/null and the all-exchange summary with `limit_rank=10`.
Other no-date `limit_rank` or exchange combinations require explicit date
filters or a custom summary refresh. Refresh can run manually, monthly, weekly,
or after major data updates:

```text
python -m maintenance.refresh_stim_select_outcome_summary_cache
```

The SQL definition is in `docs/operations/stim_select_outcome_summary_table.sql`.
Explicit `start_date` or `end_date` requests may still be computed live and
preserve the existing 13-week response semantics. This endpoint is historical
signal-rule evidence, not current live ST-IM Select membership.

### Published Intelligence Artifacts

```text
GET /v1/intelligence/discovery
GET /v1/intelligence/guidance/latest
GET /v1/intelligence/guidance/{artifact_id}
GET /v1/intelligence/research/latest
GET /v1/intelligence/research/{artifact_id}
GET /v1/intelligence/editorial/latest/preview
```

Read-only access to published Stock Trends Intelligence Agent artifact
envelopes exported as `PublicArtifactEnvelope.v1` plus `manifest.json`. The API
reads only exported public envelopes from `ST_INTELLIGENCE_ARTIFACTS_DIR`; it
does not call Agent graph nodes, Agent services, generation code, or raw Agent
filesystem internals. Invalid manifests return unavailable responses and
invalid, unpublished, expired, or hash-mismatched artifacts fail closed.

Access classification:

* public/free:
  * `GET /v1/intelligence/discovery`
  * `GET /v1/intelligence/editorial/latest/preview`
* paid/metered through subscription, x402, or MPP:
  * `GET /v1/intelligence/guidance/latest` -> `intelligence_guidance_latest` (0.25 STC)
  * `GET /v1/intelligence/guidance/{artifact_id}` -> `intelligence_guidance_by_id` (0.25 STC)
  * `GET /v1/intelligence/research/latest` -> `intelligence_research_latest` (0.50 STC)
  * `GET /v1/intelligence/research/{artifact_id}` -> `intelligence_research_by_id` (0.50 STC)

Discovery metadata and editorial preview may serve `published` or
`publish_ready` exports. Paid guidance and research routes serve only
`published` or `product_grade` exports. Initial pricing-rule seed SQL is in
`docs/operations/intelligence_pricing_rules.sql`.

### Cognition Metadata

```text
GET /v1/meta/inference
GET /v1/meta/stim
GET /v1/meta/indicators
```

`/v1/meta/inference` is the provider-agnostic inference contract. ST-IM is the
current baseline inference provider, not the final intelligence layer; future
Causal AI providers should fit the same cognition contract.

---

##  Cost Estimation

```text
GET /v1/cost-estimate
```

---

##  API Documentation

https://api.stocktrends.com/v1/docs

---

##  Built for AI Agents

This API is designed for:

* autonomous agents
* trading systems
* financial copilots
* workflow automation

Features:

* machine-readable pricing
* deterministic billing
* stateless payments through x402
* session-based payments through MPP
* predictable response structures

---

##  Request Lifecycle

1. request received
2. pricing evaluated in STC
3. payment verified
4. request executed
5. usage logged and billed

---

##  What Makes This Different

Most APIs:

* separate billing from usage
* rely mainly on subscriptions

Stock Trends API:

* **monetizes each request**
* **is agent-native**
* **unifies pricing across rails**

---

##  Repository Structure

```text
/routers       endpoints
/pricing       STC pricing
/payments      payment rails
/metering      logging and billing
/middleware    enforcement
/docs          system design
```

---

##  Documentation

```text
/docs/strategy/       pricing and economics
/docs/architecture/   system design
/docs/operations/     billing and policies
```

---

##  AI Agent Rules

Before interacting:

1. read `AGENTS.md`
2. follow pricing rules
3. do not bypass billing
4. use documented endpoints

---

##  Status

* STC pricing: active
* subscription: active
* x402: active
* MPP: active
* STOK: planned

---

##  Final Note

This API is a **programmable financial layer for intelligence access**, built for:

* developers
* AI agents
* automated systems

Start querying. Start building. Start monetizing intelligence.
