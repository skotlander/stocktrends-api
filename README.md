# Stock Trends API

## Financial Intelligence for Agents, Researchers, and Quantitative Systems

Stock Trends is a **structured financial intelligence platform**, not a commodity market data API.

It provides machine-readable access to 30+ years of proprietary market intelligence built on the Stock Trends methodology — a rigorous framework for trend classification, relative performance measurement, market breadth analysis, market leadership tracking, and probabilistic forward return modeling.

At the core is **ST-IM** (the Stock Trends Inference Model): a statistical model that produces forward return expectations and probability distributions across 4-week, 13-week, and 40-week horizons, grounded in historical populations of structured market observations.

The API is designed for autonomous access — x402-native, machine-priced, and deterministic — so agents, quantitative pipelines, and investment research systems can consume intelligence without human intermediation.

---

## Why Stock Trends?

Most market data APIs give you price history and corporate fundamentals. Stock Trends gives you something different: **structured market intelligence built on a proprietary classification methodology applied to markets for over 30 years.**

| What others offer | What Stock Trends offers |
|---|---|
| Raw price data | Trend-state classification (`^+`, `v-`, `v^`, and more) |
| Price change percentages | ST-IM forward return distributions (4wk, 13wk, 40wk) |
| Volume numbers | Abnormal volume signals (accumulation/distribution context) |
| Generic screeners | STIM Select: stocks meeting statistical forward-return thresholds across all three horizons |
| No structural history | 30+ years of structured weekly market observations |
| No breadth signals | Market breadth and sector leadership intelligence |
| No published research | Intelligence Agent guidance and research artifacts |

The Stock Trends classification system converts raw weekly market behavior into structured, repeatable factor states. These states create meaningful historical populations from which forward-return distributions can be estimated, compared, and acted on.

**ST-IM probabilities are conditional historical tendencies, not guarantees.** This is a system built to improve decision-making under uncertainty — not to eliminate it.

---

## Who Is This For?

**Financial AI agents**
Autonomous systems that need machine-readable market intelligence with native per-request or session-based payment support.

**Quantitative researchers**
Researchers working with trend-state factors, relative strength analysis, forward-return distributions, and market breadth signals across long historical horizons.

**Investment research systems**
Platforms building structured research workflows that combine indicator data, breadth signals, and published research artifacts.

**Portfolio management tools**
Systems that incorporate trend classification, ST-IM forward expectations, and STIM Select rankings into construction and review workflows.

**Trading assistants and copilots**
LLM-powered tools that need grounded, structured market intelligence rather than raw price feeds.

**Autonomous financial workflows**
Pipelines that require deterministic, auditable market intelligence without human intermediation.

**MCP-connected agents**
Agents consuming Stock Trends intelligence through Model Context Protocol tooling (MCP integration is on the roadmap).

---

## What Can You Build?

**Market monitoring agents**
Agents that track trend classification changes, breadth deterioration, or unusual volume signals across sectors or portfolios.

**Portfolio ranking systems**
Systems that rank holdings or candidates by ST-IM forward return expectations, STIM Select qualification, and relative strength.

**Investment research copilots**
LLM assistants grounded in structured Stock Trends signals, breadth data, and published intelligence artifacts.

**Breadth-aware trading systems**
Pipelines that condition position sizing or entry decisions on market breadth and sector leadership signals.

**Autonomous research workflows**
End-to-end pipelines that discover, retrieve, and process published Stock Trends guidance and research artifacts without human intervention.

**Signal validation pipelines**
Systems that test indicator combinations against historical STIM Select outcome data to validate research hypotheses.

---

## Quick Start

### 1. Evaluate a symbol

This endpoint returns the ST-IM-derived decision for a given symbol — a forward-looking OUTPERFORM/UNDERPERFORM assessment with a confidence measure and time horizon. It demonstrates what separates Stock Trends from price-only data: a structured, probabilistic intelligence signal grounded in 30+ years of historical trend-state observations.

```bash
curl -X POST https://api.stocktrends.com/v1/decision/evaluate_symbol \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "AAPL",
    "exchange": "Q"
  }'
```

**Example response:**

```json
{
  "symbol": "AAPL",
  "exchange": "Q",
  "decision": "OUTPERFORM",
  "confidence": 0.62,
  "time_horizon": "13-week"
}
```

### 2. If payment is required

The API returns an x402 payment challenge. Agents complete payment automatically and resubmit. No account setup is required for x402 access.

---

## Architecture

```text
Agent / Research System / Portfolio Tool
              ↓
      Stock Trends API
              ↓
    ┌────────────────────────┐
    │  ST-IM                 │  Forward return expectations (4wk / 13wk / 40wk)
    │  Breadth               │  Market-wide and sector breadth signals
    │  Leadership            │  Sector and market leadership tracking
    │  Selections            │  STIM Select ranked by prob13wk
    │  Research Artifacts    │  Published guidance, research, editorial
    └────────────────────────┘
              ↓
    Portfolio Decisions
    Research Workflows
    Investment Intelligence
```

The intelligence stack layers from raw market data through the Stock Trends indicator framework into inference providers (currently ST-IM), a shared reasoning runtime, and outward to API, MCP, and agent-facing interfaces. The architecture is designed to accommodate future Causal AI providers within the same reasoning runtime without redesigning discovery, pricing, or agent surfaces.

---

## Built for Agents

The Stock Trends API is designed for autonomous, machine-driven consumption.

**x402 support**
Agents discover pricing, complete per-request payments, and receive intelligence without human-mediated subscription management.

**Machine-readable pricing**
Every endpoint exposes its STC cost in a consistent, discoverable format. Agents can inspect pricing before committing payment.

**Deterministic responses**
Structured, schema-stable responses with no narrative variability — suitable for downstream automated processing.

**Session-based payments via MPP**
High-frequency agent workflows can fund a session balance through MPP, avoiding per-request payment overhead across many requests.

**MCP compatibility (roadmap)**
The API architecture is designed to expose selected reasoning capabilities as MCP tools, enabling direct agent integration without REST overhead.

---

## Endpoint Reference

### Decision Engine

```text
POST /v1/decision/evaluate_symbol
POST /v1/portfolio/evaluate
POST /v1/portfolio/construct
```

### Market Intelligence

```text
GET /v1/stim/latest
GET /v1/indicators/latest
GET /v1/prices/latest
GET /v1/selections/latest
GET /v1/breadth/sector/latest
```

### Published STIM Select Signal Outcomes

```text
GET /v1/selections/stim-select/outcomes/summary
```

Public/free aggregate historical outcome evidence for observations meeting ST-IM Select criteria. Exposes `outcomes_by_horizon` for realized 4-week, 13-week, and 40-week forward returns (`fpr_chg4`, `fpr_chg13`, `fpr_chg40`). Does not expose current selections, current matching symbols, or individual historical symbols.

When `start_date` and `end_date` are omitted, a trailing 10-year window ending at the latest mature outcome date is applied (`filters.default_window_applied: true`). The response includes `generated_at` and `source_latest_mature_weekdate` for freshness assessment. Supported seeded no-date combinations: all-exchange summary with `limit_rank` omitted/null, and all-exchange summary with `limit_rank=10`. Other combinations require explicit date filters or a cache refresh:

```text
python -m maintenance.refresh_stim_select_outcome_summary_cache
```

The SQL definition is in `docs/operations/stim_select_outcome_summary_table.sql`.

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

Public/free discovery endpoints exposing official Stock Trends model portfolio metadata, returns history, a compact public history summary, and historical closed-position records. The summary includes Stock Trends annualized ROI on average invested capital. Current live holdings are intentionally excluded.

Strategy metadata describes declared buy/sell rules and economic assumptions behind official model portfolios. Strategy conditions are legacy provenance metadata and are not executable query endpoints — they do not return current matching stocks or current live holdings.

### Published Intelligence Artifacts

```text
GET /v1/intelligence/discovery
GET /v1/intelligence/guidance/latest
GET /v1/intelligence/guidance/{artifact_id}
GET /v1/intelligence/research/latest
GET /v1/intelligence/research/{artifact_id}
GET /v1/intelligence/editorial/latest/preview
```

Read-only access to published Stock Trends Intelligence Agent artifact envelopes (`PublicArtifactEnvelope.v1`). The API reads only exported public envelopes from `ST_INTELLIGENCE_ARTIFACTS_DIR`; it does not call Agent generation code, graph nodes, or internal Agent services. Invalid manifests return unavailable responses; invalid, unpublished, expired, or hash-mismatched artifacts fail closed.

**Access classification:**

| Endpoint | Access | STC Cost |
|---|---|---:|
| `GET /v1/intelligence/discovery` | Public / free | — |
| `GET /v1/intelligence/editorial/latest/preview` | Public / free | — |
| `GET /v1/intelligence/guidance/latest` | Paid | 0.25 STC |
| `GET /v1/intelligence/guidance/{artifact_id}` | Paid | 0.25 STC |
| `GET /v1/intelligence/research/latest` | Paid | 0.50 STC |
| `GET /v1/intelligence/research/{artifact_id}` | Paid | 0.50 STC |

Paid guidance and research routes verify artifact availability before any payment challenge. Missing stores return `503`; absent, expired, or hash-mismatched artifacts fail closed before payment and do not advertise subscription/x402/MPP.

### Cognition Metadata

```text
GET /v1/meta/inference
GET /v1/meta/stim
GET /v1/meta/indicators
```

`/v1/meta/inference` is the provider-agnostic inference contract. ST-IM is the current baseline inference provider; the architecture is designed so future Causal AI providers fit the same cognition contract without requiring discovery or agent surfaces to be redesigned.

### Cost Estimation

```text
GET /v1/cost-estimate
```

---

## Pricing Model (STC)

**STC — Stock Trends Credits** is the unified pricing unit across all payment rails.

* 1 STC ≈ $1.00 USD (reference value; configurable pricing policy, not a fixed peg)
* All endpoints resolve to a fixed STC cost
* Pricing is rail-independent: the same STC cost applies regardless of payment method

### Example Endpoint Costs

| Endpoint | STC | Approx USD |
|---|---:|---:|
| `/stim/latest` | 0.0025 | $0.0025 |
| `/prices/latest` | 0.0025 | $0.0025 |
| `/agent/screener/top` | 0.50 | $0.50 |
| `/portfolio/construct` | 1.00 | $1.00 |

---

## Payment Rails

### Subscription

Purchase STC in advance. STC is deducted from an account balance per request.

### x402 (per-request)

Pay per request with no account required. The API returns an x402 payment challenge; agents complete payment automatically and resubmit.

**x402 price fields:**

| Field | Meaning |
|---|---|
| `amount_usd` | Human-readable USD price |
| `amount` | Token base units (6 decimal places) |

Example: `"amount": "500000"` = 0.5 USDC — **not $500,000**.

### MPP (session-based)

Session-funded payments optimized for high-frequency agent workflows. STC is deducted from a session balance. MPP does not follow the x402 challenge/verify flow.

### STOK (planned)

Future token-based incentive layer.

---

## Request Lifecycle

1. Authentication
2. Pricing resolution (STC)
3. Payment path selection
4. Payment enforcement
5. Endpoint execution
6. Logging and metering
7. Response

---

## Repository Structure

```text
/routers       endpoints
/pricing       STC pricing
/payments      payment rails
/metering      logging and billing
/middleware    enforcement
/docs          system design
```

---

## Documentation

```text
/docs/strategy/       pricing and economics
/docs/architecture/   system design
/docs/operations/     billing and policies
```

Full API reference: https://api.stocktrends.com/v1/docs

---

## AI Agent Rules

Before interacting programmatically:

1. Read `AGENTS.md`
2. Follow pricing rules
3. Do not bypass billing
4. Use documented endpoints only

---

## Status

| Component | Status |
|---|---|
| STC pricing | Active |
| Subscription | Active |
| x402 | Active |
| MPP | Active |
| STOK | Planned |
