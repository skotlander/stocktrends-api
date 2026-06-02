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
```

These routes expose official Stock Trends model portfolio metadata and returns
history, a compact public history summary, and historical closed-position
records as public/free discovery data. The summary includes Stock Trends
annualized ROI on average invested capital. Current live holdings are
intentionally excluded from the public portfolio history surface.

### Market Intelligence

```text
GET /v1/stim/latest
GET /v1/indicators/latest
GET /v1/prices/latest
GET /v1/selections/latest
GET /v1/breadth/sector/latest
```

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
