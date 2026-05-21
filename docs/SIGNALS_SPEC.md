# Stock Trends Signals Spec

## Purpose

This document defines the signal layer consumed by Stock Trends inference providers and agent-facing reasoning surfaces. It complements `docs/STOCK_TRENDS_SEMANTIC_CONTRACT.md` and `docs/STOCK_TRENDS_COGNITION_ARCHITECTURE.md`.

The signal layer is provider-agnostic. ST-IM currently consumes these signals as the baseline inference provider, and future Causal AI providers should be able to consume the same signal concepts without redefining their meaning.

---

## Signal Layer Role

Stock Trends signals convert raw weekly market behavior into structured, repeatable states. These states support:

- symbol-level interpretation
- market-regime analysis
- breadth and leadership analysis
- historical population construction
- probabilistic inference
- future causal reasoning
- portfolio research workflows

Signals are evidence inputs. They are not deterministic predictions, price targets, or direct buy/sell commands.

---

## Core Signal Fields

### `trend`

Categorical Stock Trends trend classification.

Values:

- `^+` bullish
- `^-` weak bullish
- `v^` bullish crossover
- `v-` bearish
- `v+` weak bearish
- `^v` bearish crossover

### `trend_cnt`

Number of consecutive weeks the instrument has maintained the current `trend` classification.

Interpretation: trend persistence.

### `mt_cnt`

Number of weeks the instrument has remained in the current broader major trend category.

Bullish category:

- `^+`
- `^-`
- `v^`

Bearish category:

- `v-`
- `v+`
- `^v`

Interpretation: trend maturity.

### `rsi`

Stock Trends relative strength measure versus the benchmark, typically the S&P 500, over a 13-week period.

Baseline:

- `100`

Interpretation:

- `> 100`: outperformance versus benchmark
- `< 100`: underperformance versus benchmark

This is not the traditional Wilder RSI oscillator.

### `rsi_updn`

Weekly direction of relative strength versus benchmark.

Values:

- `+`: outperformed this week
- `-`: underperformed this week

### `vol_tag`

Categorical unusual volume context.

Values:

- `**`, `*`: high volume
- `!!`, `!`: low volume
- empty: normal volume

Interpretation: conviction, participation, accumulation/distribution context, or possible inflection evidence.

### `weekdate`

Week-ending date associated with the observation.

### `symbol_exchange`

Canonical instrument identifier combining ticker symbol and exchange, for example `IBM-N`.

---

## Relationship To Inference Providers

Inference providers should consume signal fields as structured evidence:

- `trend`: structural direction evidence
- `trend_cnt`: persistence evidence
- `mt_cnt`: maturity evidence
- `rsi`: benchmark-relative performance evidence
- `rsi_updn`: recent relative strength direction evidence
- `vol_tag`: volume conviction or anomaly evidence
- breadth and leadership endpoints: market-structure evidence

ST-IM uses Stock Trends classifications to form historical observation populations and estimate forward-return distributions.

Future Causal AI providers may use the same signal fields as causal factors, intervention variables, regime-transition evidence, or explanatory features.

---

## Agent Interpretation Rules

Agents must:

- preserve signal field meanings exactly
- distinguish signal evidence from inference output
- distinguish inference output from investment advice
- disclose uncertainty and limitations
- avoid treating any signal as a guaranteed outcome
- use `/v1/meta/inference` for provider-agnostic inference context
- use `/v1/meta/stim` for ST-IM-specific provider context

Agents must not:

- describe Stock Trends as raw price data
- confuse Stock Trends RSI with oscillator RSI
- convert signal states directly into buy/sell commands
- treat ST-IM as the final intelligence layer
- collapse future causal-provider semantics into ST-IM-only fields
