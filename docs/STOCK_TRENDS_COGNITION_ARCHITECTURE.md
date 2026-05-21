# Stock Trends Cognition Architecture

## Purpose

This document defines the long-term intelligence architecture for the Stock Trends API and related agent-facing systems. Its purpose is to prevent architectural drift as the API evolves from data access and current ST-IM inference into a reusable financial reasoning runtime for Stock Trends applications, MCP integrations, and future Causal AI intelligence.

The API should not be understood merely as a set of endpoint wrappers. It is one distribution surface for a broader Stock Trends reasoning and cognition layer.

---

## Strategic Architecture

The intended long-term Stock Trends intelligence stack is:

```text
Raw Market Data
    ->
Stock Trends Indicators
    ->
Inference Providers
    -> ST-IM baseline provider
    -> Future Causal AI providers
    ->
Stock Trends Reasoning Runtime / Cognition Layer
    ->
API, MCP, x402/MPP, and Agent Interfaces
    ->
Applications: research tools, editorials, portfolio agents, market commentary, developer tools
```

Each layer has a distinct role.

### 1. Raw Market Data

The foundational historical and current market data used by Stock Trends.

### 2. Stock Trends Indicators

The proprietary indicator layer built from Stock Trends methodology, including trend classification, relative strength, breadth, leadership, volume, trend persistence, trend maturity, and related market-structure signals.

### 3. Inference Providers

Inference providers consume Stock Trends indicators and related evidence to produce forward-looking or explanatory intelligence. Provider-facing metadata should preserve:

- inference provider
- signal source
- forecast horizon
- probability distribution
- confidence measure
- evidence
- uncertainty
- explanation
- reasoning interpretation
- auditability

### 4. ST-IM: Current Baseline Provider

ST-IM, the Stock Trends Inference Model, is the existing baseline inference provider. It is historically established, explainable, and useful for continuity and comparison.

ST-IM should not be treated as the final or only intelligence layer. It is the current inference foundation and an important source of durable baseline evidence.

### 5. Future Causal AI Intelligence Core

Future Causal AI systems are expected to become advanced Stock Trends intelligence providers. The API architecture must remain flexible enough to support future causal outputs, including:

- causal graphs
- causal factor attribution
- intervention and counterfactual analysis
- probabilistic forecasts
- confidence intervals
- regime transition probabilities
- causal explanations of market leadership and breadth changes
- portfolio decision intelligence

### 6. Reasoning Runtime / Cognition Layer

The reasoning runtime interprets, validates, contextualizes, and communicates Stock Trends intelligence. It should provide:

- disciplined market reasoning
- semantic enforcement
- probabilistic interpretation
- validation and correction
- workflow orchestration
- auditability
- agent-ready structured outputs

Runtime services should consume validated artifacts and structured product payloads, not raw pipeline state dictionaries or graph nodes. FastAPI, MCP, CLI, and batch interfaces should call this service boundary rather than owning reasoning logic.

### 7. API, MCP, x402/MPP, and Agent Interfaces

The API, MCP tools, and payment-preview surfaces distribute selected reasoning and inference capabilities. They are not the core intelligence themselves.

API discovery, OpenAPI extensions, `/v1/ai/tools`, `/v1/ai/context`, x402 `stocktrends_preview`, MPP session metadata, and future MCP tools should use provider-agnostic inference concepts where practical.

### 8. Applications

Editorial articles, research briefs, portfolio commentary, market regime reports, agent tools, and developer-facing services are downstream applications of the shared reasoning runtime.

---

## Core Doctrine

### ST-IM Is The Baseline, Not The Ceiling

ST-IM is the current baseline inference provider. It should remain explainable and durable, but it must not become a hard-coded ceiling that prevents future Causal AI integration.

### Future Causal AI Is A First-Class Future Provider

The architecture assumes future Causal AI providers will plug into the same reasoning runtime without requiring a redesign of discovery, validation, metadata, MCP, or payment surfaces.

### Provider-Agnostic Inference Concepts Come First

Where practical, API and agent-facing surfaces should describe inference using provider-agnostic concepts:

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

Provider-specific fields, such as ST-IM `x13wk` or `prob13wk`, should be nested under provider profiles rather than becoming the universal cognition model.

### Semantic Discipline Is Mandatory

The system must preserve exact Stock Trends meanings. In particular:

- RSI is a Stock Trends relative strength measure with a baseline of 100.
- RSI above 100 indicates outperformance relative to the benchmark.
- RSI below 100 indicates underperformance relative to the benchmark.
- `rsi_updn` reflects weekly direction of relative strength.
- Stock Trends RSI must not be confused with a standard oscillator-style RSI.
- ST-IM probabilities are conditional historical tendencies, not guarantees, price targets, or direct buy/sell commands.

### Signals Beat Narratives

External macro or news context may frame research, but it must not override Stock Trends signals. The system may say that Stock Trends signals confirm, contradict, or complicate an external narrative. It must not imply that external news proves the Stock Trends signals.

### Probabilistic Framing

The system should avoid deterministic market claims. It should express uncertainty, confidence, relative probability, risk, evidence quality, and conditional interpretation.

### Validation And Auditability

Every meaningful reasoning workflow should preserve enough metadata to support review, debugging, and trust-building:

- source inputs
- signal summaries
- provider identity
- forecast horizons
- distribution assumptions
- validation results
- confidence labels
- uncertainty and limitations
- final status

---

## API Implementation Implications

### `/v1/meta/inference`

This endpoint is the provider-agnostic inference and cognition contract. It should describe reusable concepts and available inference providers without hard-coding the reasoning layer to ST-IM.

### `/v1/meta/stim`

This endpoint is the ST-IM provider profile. It should describe ST-IM-specific fields, assumptions, base-period means, probability interpretation, strengths, and limitations while pointing back to `/v1/meta/inference`.

### Discovery And OpenAPI

Discovery and schema surfaces should carry enough metadata for autonomous agents to preserve provider identity and interpretation rules:

- `/v1/ai/context`
- `/v1/ai/tools`
- `/v1/openapi.json`
- x402 `stocktrends_preview`
- MPP session metadata
- future MCP tool manifests

### Payment Surfaces

Payment rails are economic transport layers, not cognition layers. x402 and MPP metadata may expose compact reasoning previews, but pricing, payment verification, and metering must remain separate from inference semantics.

---

## MCP Strategy Alignment

Future MCP tools should expose selected capabilities of the reasoning runtime, not merely wrap raw endpoints.

Potential MCP tools may include:

- `interpret_market_regime`
- `interpret_sector_rotation`
- `explain_breadth_confirmation`
- `interpret_stim_output`
- `interpret_causal_ai_output`
- `validate_market_commentary`
- `critique_financial_analysis`
- `generate_portfolio_commentary`

MCP should be positioned as a developer and agent integration layer for Stock Trends cognition.

---

## Strategic Risk Test

A change is aligned if it strengthens one or more of:

- reasoning quality
- semantic correctness
- validation strength
- modular inference integration
- causal-readiness
- MCP-readiness
- observability
- trustworthiness
- reusable market cognition

A change is risky if it narrows the system into:

- a rigid ST-IM-only workflow
- a generic content generator
- a thin API wrapper
- a metadata shape that cannot represent causal evidence
- a reasoning interface that loses uncertainty, evidence, explanations, or auditability

---

## Summary

The Stock Trends API should evolve as part of a broader Stock Trends financial cognition platform.

ST-IM provides the established inference baseline. Future Causal AI providers are expected to become higher-value intelligence providers. The reasoning runtime should interpret and validate both. API, x402, MPP, OpenAPI, and MCP surfaces should expose selected capabilities while preserving provider-agnostic inference concepts and Stock Trends semantic discipline.
