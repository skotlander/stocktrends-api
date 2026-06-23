# Stock Trends API — Examples

These examples demonstrate real-world workflows, agents, and research systems
built on Stock Trends intelligence. They are not endpoint demonstrations.
Each example solves a meaningful problem that a financial AI builder,
quantitative researcher, or agent developer would actually face.

---

## What these examples demonstrate

Stock Trends is a **financial intelligence platform**, not a commodity market
data API. These examples reflect that positioning:

- Market regime, breadth, and leadership compose into structured intelligence
- ST-IM (Stock Trends Inference Model) produces probabilistic forward return
  distributions — not price targets, not buy/sell signals
- Published intelligence artifacts are the output of an Intelligence Agent,
  consumable autonomously without human mediation
- The x402 payment protocol enables per-request agent access without
  subscription management

---

## Quick start

### Python

Requirements: `httpx` and `python-dotenv` (both in `requirements.txt`).

```bash
# Set your API credentials
export ST_API_BASE_URL=https://api.stocktrends.com
export ST_API_KEY=your-api-key         # omit for x402 / free endpoints

# Run any example
python examples/python/market_monitor.py
python examples/python/portfolio_ranker.py
python examples/python/portfolio_ranker.py NVDA-Q MSFT-Q AAPL-Q TSLA-Q
python examples/python/research_workflow.py
```

### TypeScript

Requirements: Node.js 18+ (for native fetch). Run with `tsx` or `ts-node`.

```bash
# Install tsx if needed
npm install -g tsx

# Set credentials
export ST_API_BASE_URL=https://api.stocktrends.com
export ST_API_KEY=your-api-key

# Run examples
npx tsx examples/typescript/market_monitor.ts
npx tsx examples/typescript/x402_client.ts
```

---

## Environment variables

| Variable           | Required       | Description                                           |
|--------------------|----------------|-------------------------------------------------------|
| `ST_API_BASE_URL`  | No             | API base URL. Default: `https://api.stocktrends.com`  |
| `ST_API_KEY`       | Paid endpoints | API key for subscription access (STC balance)         |
| `WALLET_PRIVATE_KEY` | x402 only   | EVM private key (hex, 0x-prefixed) for on-chain payment |
| `WALLET_ADDRESS`   | x402 only      | EVM wallet address funding x402 payments              |

Free endpoints (`/v1/intelligence/discovery`, `/v1/intelligence/editorial/latest/preview`)
require no credentials. All other endpoints require either `ST_API_KEY` (subscription)
or completion of the x402 per-request payment flow.

---

## Examples

### `python/market_monitor.py`

**A market-monitoring agent that synthesizes regime, breadth, and leadership.**

Workflow:
1. Retrieve current market regime — classification of the overall market
   trend distribution (`bullish`, `bearish`, or `mixed`) with a regime_score
   ranging from −1 (fully bearish) to +1 (fully bullish)
2. Retrieve sector breadth — what fraction of each sector's instruments
   are currently in bullish vs. bearish trend states
3. Retrieve sector leadership — which sectors contain instruments with
   the strongest relative strength (RSI > 100 = outperforming benchmark)
4. Synthesize and print a structured market intelligence summary

Expected output:
```
Stock Trends Market Intelligence Summary
========================================================
Market Regime:  BULLISH (high confidence)
  Regime score: +0.381  (range: −1 bearish → +1 bullish)
  Bullish:      68.8% of 8,432 instruments
  Bearish:      30.7%
  Week:         2026-01-24

Sector Breadth  (week: 2026-01-24)
  Improving (≥60% bullish):     Technology, Healthcare, Financials
  Deteriorating (≥55% bearish): Energy, Utilities
  Neutral:                      Consumer Discretionary, Industrials

Sector Leadership  (week: 2026-01-24)
  (RSI > 100 = outperforming benchmark; RSI < 100 = underperforming)
  Strongest sectors by avg RSI:
    Technology                        RSI  121.4
    Healthcare                        RSI  114.7
    Financials                        RSI  109.2
    Communication Services            RSI  107.8
  Weakest sectors by avg RSI:
    Energy                            RSI   88.1
    Utilities                         RSI   91.4
```

Architecture notes:
- `GET /v1/market/regime/latest` — trend distribution across all common stocks
- `GET /v1/breadth/sector/latest` — per-sector bullish/bearish counts and percentages
- `GET /v1/leadership/summary/latest` — instruments with RSI ≥ 110 and mt_cnt ≥ 4,
  grouped by sector

---

### `python/portfolio_ranker.py`

**Rank a portfolio or watchlist by Stock Trends intelligence signals.**

Workflow:
1. For each symbol, call the decision evaluation endpoint — a composite
   assessment that combines the symbol's trend state with the live market
   regime to produce a `decision_score` (0–1), `bias`, and `alignment`
2. Retrieve ST-IM forward return distributions for each symbol —
   expected returns and standard deviations across 4-week, 13-week,
   and 40-week horizons
3. Rank symbols by `decision_score` descending, with ST-IM 13wk as tiebreaker

Expected output:
```
Stock Trends Portfolio Ranking
============================================================
  Market Regime:  BULLISH (score +0.381)
  Week:           2026-01-24
  Ranked by:      decision_score (regime alignment + trend strength + RSI)

#1  NVDA-Q        score=0.871  BULLISH / aligned
      Trend:  ^+  cnt=12  mt=28  RSI=134
      ST-IM 13wk: +8.4% ± 4.2%  CI [+6.1% → +10.7%]  |  4wk: +2.9%  40wk: +18.6%
      Note:   Mature trend state (12 weeks)
      Note:   Long-standing major trend (28 weeks)

#2  MSFT-Q        score=0.741  BULLISH / aligned
      Trend:  ^+  cnt=6  mt=14  RSI=118
      ST-IM 13wk: +6.1% ± 3.9%  CI [+4.2% → +8.0%]  |  4wk: +2.1%  40wk: +13.4%
```

Architecture notes:
- `POST /v1/decision/evaluate-symbol` — per-symbol composite scoring
- `GET /v1/stim/latest?symbol_exchange={SYM-EX}` — ST-IM distributions

Both endpoints also accept `symbol` + `exchange` as separate parameters
(`?symbol=NVDA&exchange=Q` / `{"symbol": "NVDA", "exchange": "Q"}`).
The `symbol_exchange` combined form is preferred for agent use.

**ST-IM field reference:**
- `x13wk` — expected 13-week forward return (mean of historical distribution)
- `x13wksd` — standard deviation of 13-week return distribution
- `x13wk1` / `x13wk2` — lower / upper confidence bounds
- `x4wk`, `x40wk` — expected returns at 4-week and 40-week horizons

ST-IM outputs are conditional historical tendencies. They are best used for
ranking and portfolio construction, not for precise price targets or certainty
about individual-stock outcomes.

---

### `python/research_workflow.py`

**Autonomous consumption of published Stock Trends intelligence artifacts.**

This example demonstrates the intelligence layer of Stock Trends — research
artifacts produced by the Stock Trends Intelligence Agent, distributed via
the API as read-only `PublicArtifactEnvelope.v1` exports.

Workflow:
1. Discover available artifacts — free endpoint returns a catalog of what
   the Intelligence Agent has published and their availability status
2. Retrieve the latest market guidance artifact (0.25 STC) — the Agent's
   current market outlook and actionable guidance
3. Retrieve the latest market research report (0.50 STC) — in-depth analysis
   covering sector structure, breadth, leadership, and ST-IM signal interpretation
4. Summarize artifact metadata: artifact_id, weekdate, published_at, provider,
   payload structure, content_hash

Expected output:
```
Stock Trends Research Workflow
========================================================
  Discovery Metadata  (week: 2026-01-24)
  Published artifacts in catalog: 3
    • market_guidance           guid-2026-01-24-v1    [published]
    • market_research_report    rpt-2026-01-24-v1     [published]
    • editorial_preview         ed-2026-01-24-v1      [publish_ready]

  Market Guidance Artifact
  ────────────────────────────────────────────────────
  Artifact ID:     guid-2026-01-24-v1
  Type:            market_guidance
  Status:          published
  Week:            2026-01-24
  Published:       2026-01-25T09:14:32Z
  Revision:        1
  Provider:        stock-trends-intelligence-agent v2.1
  Payload keys:    title, regime_assessment, sector_outlook, guidance
  Content hash:    sha256:a3f9c2d1b8e4...
```

Architecture notes:
- `GET /v1/intelligence/discovery` — free; discovery_metadata envelope
- `GET /v1/intelligence/guidance/latest` — paid (0.25 STC); market_guidance envelope
- `GET /v1/intelligence/research/latest` — paid (0.50 STC); market_research_report envelope

The API reads exported `PublicArtifactEnvelope.v1` files. It does not call Agent
generation code — it is a read-only distribution boundary. Artifacts include a
deterministic `content_hash` (sha256) for integrity verification.

---

### `typescript/market_monitor.ts`

**TypeScript equivalent of `python/market_monitor.py`.**

Same three-step workflow (regime → breadth → leadership) implemented in
TypeScript with native `fetch`, full type annotations for all API responses,
and inline x402 detection. Suitable as the foundation for an MCP tool or
a Node.js/Next.js agent that needs current market structure context.

---

### `typescript/x402_client.ts`

**Demonstrates the x402 per-request payment flow.**

The x402 protocol enables agents to access Stock Trends without a subscription
account. Each request is priced and paid individually, with no session state.

Flow demonstrated:
1. Agent sends request without a payment header
2. API returns `402 Payment Required` with a challenge body:
   - `pricing.amount_usd` — human-readable USD amount (e.g. `"0.002500"`)
   - `payment_required.accepts[0].amount` — token base units (USDC, 6 decimals):
     `"2500"` = 0.0025 USDC = $0.0025 — **not** $2,500
   - `payment_required.accepts[0].network` — `eip155:8453` (Base)
   - `payment_required.accepts[0].payTo` — Stock Trends receiver address
3. Agent builds payment proof (stubbed — requires CDP SDK integration)
4. Agent resubmits with `X-Payment` header containing base64-encoded proof
5. API verifies with CDP facilitator and returns response

The example includes a reusable `fetchWithX402<T>()` wrapper that handles the
detect → pay → retry pattern for any endpoint.

To complete real payments, replace `buildPaymentProof()` with a Coinbase CDP SDK
call or an EIP-3009 `receiveWithAuthorization` transaction on Base mainnet.

---

## Signal reference

| Signal      | Definition                                                      |
|-------------|-----------------------------------------------------------------|
| `trend`     | Trend state: `^+` bullish, `^-` weak bullish, `v^` crossover, `v-` bearish, `v+` weak bearish, `^v` bearish crossover |
| `trend_cnt` | Weeks in current specific trend state (persistence)            |
| `mt_cnt`    | Weeks in current major trend category (maturity)               |
| `rsi`       | Relative strength vs. benchmark. Baseline 100. >100 = outperforming |
| `rsi_updn`  | Weekly RSI direction: `+` improving, `-` weakening             |
| `vol_tag`   | Volume signal: `**`/`*` high (accumulation), `!!`/`!` low (distribution) |
| `x13wk`     | ST-IM expected 13-week forward return (mean)                   |
| `x13wksd`   | ST-IM 13-week return standard deviation                        |
| `prob13wk`  | Probability of exceeding 13-week base-period mean return (2.19%), STIM Select ranking field |

**Stock Trends RSI is not the Wilder RSI oscillator.** It is a relative-strength
measure comparing the instrument's return against a benchmark (typically S&P 500)
over a 13-week period. The baseline is 100, not 50.
