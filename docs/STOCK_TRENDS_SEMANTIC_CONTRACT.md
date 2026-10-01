# Stock Trends Semantic Contract

## Purpose

This document is the authoritative source for all Stock Trends terminology, indicator definitions, and statistical outputs used in the Stock Trends API.

All API descriptions, previews, AI tool manifests, and documentation must adhere strictly to the definitions in this file.

This document exists to prevent ambiguity, inference, or incorrect interpretation of Stock Trends concepts by developers, AI agents, and automated systems.

---

## Core Principle

Stock Trends is **not a raw price data system**.

It is a **structured market intelligence framework** built on:

- trend classification
- relative performance measurement
- volume signal interpretation
- trend maturity tracking
- statistical forward return modeling (STIM)

All outputs are designed for **decision-making**, not raw data retrieval.

---

## Cognition Architecture Alignment

This semantic contract operates within the broader Stock Trends cognition architecture:

`docs/STOCK_TRENDS_COGNITION_ARCHITECTURE.md`

ST-IM is the current baseline inference provider, not the final intelligence layer.
Future Causal AI providers must be able to integrate into the same reasoning runtime
without requiring discovery metadata, OpenAPI descriptions, x402/MPP previews, or MCP
tooling to be redesigned around ST-IM-only assumptions.

Where practical, agent-facing surfaces should use provider-agnostic inference concepts:

- inference provider
- forecast horizon
- probability distribution
- confidence measure
- evidence
- uncertainty
- explanation
- signal source
- reasoning interpretation
- auditability

---

## Indicator Definitions

### trend

**Definition:**
Categorical classification of a security’s price trend based on the Stock Trends moving average framework.

**Values:**
- `^+` → Bullish
- `^-` → Weak Bullish
- `v^` → Bullish Crossover
- `v-` → Bearish
- `v+` → Weak Bearish
- `^v` → Bearish Crossover

**Interpretation:**
Represents the structural relationship between price and key moving averages.

---

## Canonical Market-State Aggregates

Canonical market-state and market-breadth style aggregates use the canonical
reporting-equity population: `exchange IN ('A', 'N', 'Q', 'T')` and `type IN
('CS', 'UN')`. A/N/Q/T are the canonical Stock Trends reporting exchanges for
market aggregates. Other valid computed exchange data may exist in `st_data`,
but its existence does not imply inclusion in canonical market context. This
definition applies to market aggregates only; it does not change explicit
security-level endpoint exchange eligibility or every explicitly CS-only
security screen in the API.

The classified directional population contains exactly six trend states:

- Bullish: `^+`, `^-`, `v^`
- Bearish: `^v`, `v+`, `v-`

Exact neutral states are `--` and `=`. Null, empty, and any other trend state
are unknown or unclassified. Neutral and unclassified observations remain
transparent in aggregate outputs, but are excluded from the directional
denominator.

For canonical market-state aggregates:

- `classified_count = bullish_count + bearish_count`
- `bullish_pct = bullish_count / classified_count`
- `bearish_pct = bearish_count / classified_count`
- `regime_score = (bullish_count - bearish_count) / classified_count`

For a nonzero classified population, `bullish_pct + bearish_pct = 1`.

`regime_score` and `avg_mt_cnt` use the classified CS+UN population only.
`avg_rsi` uses that same classified CS+UN population, with only non-null,
finite RSI values less than or equal to `10000` included in the average. This
is an owner-defined conservative data-quality ceiling, not a mathematical upper
bound of the indicator: values greater than `10000` are treated as erroneous
RSI observations. There is no lower-bound RSI validity rule. Neutral and
unknown rows are excluded from both aggregate maturity and aggregate RSI. An
invalid RSI excludes only that RSI observation from RSI-derived aggregates; it
does not remove the security from classified counts, breadth, maturity, trend
persistence, taxonomy population, or taxonomy coverage.

---

## Market Epoch v1

**Market Epoch** is a frozen unsupervised K=3 market-state classifier based on
aggregate weekly Stock Trends features. It is contextual market state. It is not a trade signal, not a forward-return forecast, and not causal AI.

The frozen v1 labels are:

- `BROAD_BULLISH` / Broad Bullish
- `BEARISH_MATURITY` / Bearish Maturity
- `BULLISH_MATURITY` / Bullish Maturity

### Epoch fields

- `epoch_id` — persisted descriptive state identifier.
- `epoch_name` — persisted human-readable descriptive state name.
- `raw_cluster_id` — persisted frozen classifier cluster identifier; API consumers
  must not reinterpret or remap it.
- `weeks_in_epoch` — consecutive official weekly persistence of the current
  assigned Epoch.
- `changed_this_week` — persisted `0` or `1` flag indicating whether the assigned
  Epoch differs from the immediately previous official weekly Epoch.
- `assigned_distance` — distance to the assigned frozen centroid in standardized
  feature space.
- `second_nearest_distance` — distance to the second-nearest frozen centroid in
  standardized feature space.
- `separation_margin` — `second_nearest_distance - assigned_distance`.

`separation_margin` is not a probability and not forecast confidence. The
descriptive word “Maturity” does not imply that a reversal must follow.

---

### trend_cnt

**Definition:**
Number of consecutive weeks the security has maintained its current `trend` classification.

**Interpretation:**
Measures **trend persistence**.

Higher values indicate a more established trend.

---

### mt_cnt (trend maturity)

**Definition:**
Number of weeks the security has remained within its broader trend category:

- Bullish category: `^+`, `^-`, `v^`
- Bearish category: `v-`, `v+`, `^v`

**Interpretation:**
Represents **trend maturity** rather than short-term movement.

Used to distinguish:
- early-stage trends
- mature trends
- potential exhaustion conditions

---

### rsi (Relative Strength Indicator)

**Definition:**
Relative performance of the security versus its benchmark (typically S&P 500) over a 13-week period.

**Scale:**
- Baseline = 100

**Interpretation:**
- `> 100` → Outperformance
- `< 100` → Underperformance

---

### rsi_updn

**Definition:**
Directional indicator of weekly relative performance versus benchmark.

**Values:**
- `+` → Outperformed this week
- `-` → Underperformed this week

---

### vol_tag (Unusual Volume Indicator)

**Definition:**
Categorical indicator of abnormal trading volume relative to historical norms.

**Values:**
- `**`, `*` → High volume
- `!!`, `!` → Low volume
- empty → Normal volume

**Interpretation:**
Used to detect:
- accumulation/distribution
- conviction in price movement
- potential inflection points

---

### weekdate

**Definition:**
The week-ending date associated with the observation.

---

### symbol_exchange

**Definition:**
Canonical identifier combining ticker symbol and exchange.

Example: IBM-N


---

## STIM — Stock Trends Inference Model

### Definition

**STIM = Stock Trends Inference Model**

STIM is a statistical model that produces:

- forward return expectations
- statistical distributions of expected returns

across multiple time horizons.

---

### Critical Clarification

STIM is:

- NOT "Stock Trends Intermediate Momentum"
- NOT "Stock Trends Indicator Model"
- NOT a momentum indicator
- NOT derived from simple price changes

STIM is a **probabilistic forward-looking model**.

---

### Time Horizons

STIM produces outputs for:

- 4-week horizon
- 13-week horizon
- 40-week horizon

---

### STIM Fields

Each horizon includes:

#### Example (4-week):

- `x4wk1` → lower bound / percentile estimate
- `x4wk2` → upper bound / percentile estimate
- `x4wk` → expected return (mean)
- `x4wksd` → standard deviation

Equivalent fields exist for:

- `x13wk`, `x13wksd`, etc.
- `x40wk`, `x40wksd`, etc.

---

### Interpretation

STIM outputs represent:

- expected return distributions
- risk (via standard deviation)
- probabilistic forward performance

They are intended for:

- ranking opportunities
- portfolio construction
- probabilistic decision-making

ST-IM begins from the assumption that markets are noisy, uncertain, and partly
random. It does not claim deterministic prediction, price certainty, or
guaranteed outperformance.

The Stock Trends classification system converts raw weekly market behavior into
structured, repeatable factor states. These states create meaningful historical
populations of observations from which forward-return distributions can be
estimated and compared.

ST-IM uses probability, confidence intervals, normal-distribution assumptions,
and central-limit-theorem intuition to reason about expected forward-return
tendencies across historical populations. The objective is not to eliminate
randomness, but to measure uncertainty and improve decision-making under
uncertainty.

ST-IM probabilities must be interpreted as conditional historical tendencies,
not guarantees, not price targets, and not direct buy/sell commands.

ST-IM is especially valuable in portfolio workflows, repeated decision-making,
ranking, screening, allocation review, regime-aware interpretation, and
market-structure analysis.

Limitations must remain explicit: regime shifts, non-stationarity, sample-size
weakness, tail events, liquidity shocks, news shocks, and uncertainty in
individual-stock outcomes.

---

## Allowed Terminology

The following phrasing is approved and should be reused:

> "Stock Trends Inference Model (ST-IM) outputs: forward return expectations and statistical distributions across 4-week, 13-week, and 40-week horizons."

---

## Forbidden Terminology

The following must NEVER appear in the codebase:

- "Stock Trends Intermediate Momentum"
- "Stock Trends Indicator Model"
- "momentum values" (when referring to STIM)
- describing STIM as a momentum indicator
- describing Stock Trends as raw price data

---

## Agent and Developer Rules

When modifying or generating:

- API descriptions
- endpoint previews
- `/v1/ai/tools`
- documentation
- OpenAPI specs

You MUST:

1. Use definitions from this document
2. NOT infer or guess meanings
3. NOT expand acronyms unless defined here
4. NOT introduce alternative terminology

---

## Relationship Between Indicators

Stock Trends signals are designed to work together:

- `trend` → structural direction
- `trend_cnt` → persistence
- `mt_cnt` → maturity
- `rsi` → relative performance
- `vol_tag` → volume context
- `STIM` → forward probabilistic expectation

Together, they provide a **multi-dimensional view of market behavior**.

---

## STIM Select — Stock Trends Inference Model Select

### Definition

**STIM Select = Stock Trends Inference Model Select**

STIM Select stocks are securities whose Stock Trends indicator combinations satisfy the following
statistical criteria across all three ST-IM forward return horizons:

> The lower bound of the mean return confidence interval exceeds the base-period mean random return
> for all three horizons simultaneously.

### Base-Period Mean Random Returns (Thresholds)

| Horizon | Base-Period Mean Random Return |
|---------|-------------------------------|
| 4-week  | 0%                            |
| 13-week | 2.19%                         |
| 40-week | 6.45%                         |

In ST-IM field terms:
- `x4wk1 > 0.00` (lower confidence bound of 4-week expected return exceeds 0%)
- `x13wk1 > 2.19` (lower confidence bound of 13-week expected return exceeds 2.19%)
- `x40wk1 > 6.45` (lower confidence bound of 40-week expected return exceeds 6.45%)

### Ranking

STIM Select stocks are ranked in descending order of:

**Primary ranking metric: `prob13wk`** — probability of exceeding the 13-week base-period mean
random return (2.19%), assuming a normal distribution of returns.

### Publication Threshold

- `prob13wk >= 55%` (minimum 55% probability of exceeding the 13-week base mean)

### Distribution Assumption

- Normal distribution

### Key Field

- `prob13wk` — probability of exceeding the 13-week base-period mean return (primary ranking field)

### API Endpoints

**Base selection endpoints** (return all `st_select` records ranked by `prob13wk DESC`; no threshold filter applied):
- `GET /v1/selections/latest` — latest `st_select` stock list ordered by `prob13wk DESC`
- `GET /v1/selections/history` — historical `st_select` records for a symbol or date range

**Published STIM Select endpoints** (enforce all three ST-IM lower-bound thresholds plus `prob13wk >= 55%`):
- `GET /v1/selections/published/latest` — published STIM Select list with x4wk1 > 0%, x13wk1 > 2.19%, x40wk1 > 6.45%, and `prob13wk >= 55%`
- `GET /v1/selections/published/history` — historical published STIM Select records

### Forbidden Terminology

STIM Select must NOT be described as:
- "generic stock selections"
- "stock picks"
- "screener results"
- any description that omits the ST-IM probability and confidence interval criteria

---

## External References (Optional)

For additional human-readable context:

- https://stocktrends.com/learn/stock-trends-guides/quick-reference
- https://stocktrends.com/learn/stock-trends-guides/bullish-trends
- https://stocktrends.com/learn/stock-trends-guides/bearish-trends
- https://stocktrends.com/learn/stock-trends-guides/trend-counters
- https://stocktrends.com/learn/stock-trends-guides/relative-strength-indicator
- https://stocktrends.com/learn/stock-trends-guides/unusual-volume-indicator
- https://stocktrends.com/learn/stock-trends-handbook/chapter-6-using-stock-trends-systematically

These are **supplementary only**. This file remains the authoritative source for API semantics.
