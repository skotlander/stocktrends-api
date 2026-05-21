# AGENTS.md — Stock Trends API

## Purpose

This repository powers the Stock Trends API and agent monetization system.

Primary goals:

* maintain a stable FastAPI production service
* support multiple payment rails for API access
* ensure accurate request logging and metering
* enable agent-native monetization (per-request and session payments)
* preserve backward compatibility for developers
* support future token-based incentives (STOK)
* align implementation with /docs strategy and architecture

---

## Core Principles

1. **Production stability first**
2. **Payment correctness is critical**
3. **Logging and metering are first-class**
4. **Support multiple payment rails (not just one)**
5. **Make minimal, targeted changes only**
6. **Pricing must be unified across all payment rails**
7. **All payment rails must resolve to a common pricing unit (STC)**
8. **Token integration must not break pricing consistency**

---

## Unified Pricing Layer (STC) — CRITICAL

The API uses a unified pricing system based on:

→ **Stock Trends Credits (STC)**

* 1 STC ≈ $1 USD (reference value, not a fixed peg)
* All endpoints must map to a fixed STC cost
* Pricing must NOT be duplicated across payment rails

### Rules

* Same endpoint → same STC cost across all payment methods
* Payment rails affect **how payment is made**, not **what is charged**
* Pricing must be centrally defined (e.g., pricing_rules or registry)

### DO

* resolve pricing before payment enforcement
* store STC cost in request context
* use STC as the only pricing source

### DO NOT

* hardcode prices in endpoints
* create rail-specific pricing logic
* compute pricing inside payment verification logic

---

## Payment Architecture (CRITICAL)

This API supports **multiple production payment rails**.

### Active / Production Rails

* **Subscription (Stripe)** → account-based access (monthly STC allocation)
* **x402** → per-request agent payments
* **MPP (Machine Payments Protocol)** → session-based payments

### Future / Planned Layer

* **STOK** → token-based discount and incentive layer

---

## Payment Model (IMPORTANT CONCEPT)

Payment handling is decomposed into layers:

1. **Pricing Resolution**

   * Determine STC cost for the request

2. **Access Policy**

   * Free, subscription, or paid endpoint

3. **Payment Requirement**

   * Is payment required?

4. **Payment Rail**

   * subscription | x402 | mpp | stok | none

5. **Payment Translation**

   * Convert STC → rail-specific amount

6. **Verification**

   * Validate payment based on selected rail

7. **Metering / Billing**

   * Record actual usage and cost

---

## Critical Rule

⚠️ **Do NOT couple the system to a single payment rail.**

All payment-related logic must remain:

* rail-aware
* modular
* extensible
* decoupled from pricing

---

## Payment Adapter Pattern

All payment rails must implement a translation layer:

```
STC → Payment Rail
```

### Examples

* subscription → decrement STC balance
* x402 → convert STC to USD and verify payment
* MPP → deduct STC from session balance
* STOK → apply discount to STC before payment

### Rules

* Payment adapters must NOT define pricing
* Payment adapters must consume STC
* Payment adapters must be interchangeable

⚠️ Never embed payment conversion logic directly in route handlers.

---

## Payment Rails Guidance

### Subscription

* Uses monthly STC allocation
* Must decrement STC per request
* Must enforce balance limits

---

### x402

* Per-request payment model
* Uses HTTP 402 challenge/verify flow
* Must derive payment amount from STC

Ensure:

* correct network (e.g., Base)
* correct token (e.g., USDC)
* explicit error handling and logging

---

### MPP (PRODUCTION)

* Session-based payment model
* Deduct STC from session balance
* May NOT follow x402 challenge flow

System must support:

* session lifecycle tracking
* concurrent session safety
* idempotent authorization and settlement
* accurate session-based logging

### MPP Design Constraints

* do NOT assume request-by-request settlement
* do NOT assume x402-style headers or flow
* do NOT collapse MPP into x402 logic
* maintain session integrity across requests

---

### STOK (Future Integration)

STOK is an **incentive and discount layer**, not a pricing unit.

### Rules

* STOK does NOT replace STC
* STOK provides discount (recommended max: 10–20%)
* All STOK usage must resolve to STC consumption

### Requirements

* prevent arbitrage vs USD pricing
* log token usage explicitly
* support token sinks (burn or reuse)

### DO NOT

* allow STOK to bypass pricing
* allow free access via token misuse
* tie pricing directly to token value

---

## Logging and Metering (FIRST-CLASS)

All API requests must be logged with sufficient detail to support:

* billing
* analytics
* debugging
* multi-rail support

### Required Fields

* pricing_rule_id
* stc_cost
* effective_price_usd
* payment_required
* payment_rail (subscription | x402 | mpp | stok | none)
* payment_status (not_required | presented | verified | failed)
* payment_method
* payment_token
* payment_amount_native
* payment_amount_usd
* discount_applied
* token_used
* session_id (MPP)
* agent_id or customer_id
* request_id
* billed_amount

---

## Logging Rules

* Log ALL requests (success and failure)
* Reflect actual enforcement behavior
* Avoid duplicate or missing records
* Do not remove logging without preserving signal

---

## API Architecture

Expected stack:

* FastAPI backend
* routes under `/v1/`
* middleware for:

  * pricing (STC resolution)
  * auth
  * payment enforcement
  * logging/metering

Deployment:

* AWS EC2 (Ubuntu)
* nginx reverse proxy
* systemd service

---

## Cognition Architecture

Before modifying inference semantics, AI discovery metadata, endpoint previews,
OpenAPI descriptions, x402/MPP metadata, MCP-facing descriptions, or
agent-facing reasoning guidance, read:

`docs/STOCK_TRENDS_COGNITION_ARCHITECTURE.md`

Rules:

* ST-IM is the current baseline inference provider, not the final intelligence layer
* future Causal AI is a first-class future provider
* use provider-agnostic inference concepts where practical
* preserve uncertainty, evidence, explanations, confidence, signal sources, and auditability
* do not hard-code semantic contracts or reasoning interfaces to ST-IM-only concepts when broader inference abstractions fit

---

## API Behavior Rules

When modifying endpoints:

* preserve response schema unless required
* preserve OpenAPI/docs functionality
* ensure headers reflect actual behavior

Critical endpoints:

* `/v1/`
* `/v1/docs`
* `/v1/openapi.json`

---

## Payment Headers

Headers must reflect actual enforcement and pricing:

Examples:

* x-stocktrends-pricing-rule
* x-stocktrends-payment-required
* x-stocktrends-stc-cost
* x-stocktrends-effective-price-usd
* x-stocktrends-accepted-payment-methods
* x-stocktrends-selected-payment-rail

### Rules

* headers must match real behavior
* do not misrepresent payment requirements
* do not omit pricing information

---

## Implementation Rules for Payment Code

### DO

* separate pricing, payment, and verification logic
* use centralized STC pricing
* ensure adapters consume STC
* ensure logging captures full context

### DO NOT

* hardcode pricing in routes
* assume only x402 exists
* mix verification with pricing logic
* collapse payment logic into simple flags

---

## Debugging Strategy

Always identify failure layer first:

1. nginx
2. service startup
3. FastAPI app startup
4. pricing resolution
5. middleware
6. payment translation
7. payment verification
8. logging/metering

Additionally:

* identify which payment rail is active
* isolate the failing rail before fixing

Fix the **lowest responsible layer only**

---

## Infrastructure-Sensitive Areas

Be careful when modifying:

* main.py (startup)
* pricing middleware
* payment enforcement logic
* logging/metering writes
* config/env loading
* static file handling
* OpenAPI/docs generation

---

## Validation Checklist

### API

* app starts cleanly
* `/v1/` responds
* `/v1/docs` loads
* `/v1/openapi.json` loads

### Pricing

* correct STC cost applied per endpoint
* pricing is consistent across rails

### Payment

* subscription decrements STC correctly
* x402 derives correct payment amount
* MPP deducts session balance correctly

### Logging

* request is logged
* pricing_rule_id is correct
* stc_cost is correct
* payment_rail is correct
* payment_status is correct
* session_id present for MPP

### Safety

* no secrets exposed
* no debug bypasses
* no regressions

---

## Output Requirements

For every task:

1. diagnosis
2. files changed
3. reason for each change
4. validation performed
5. remaining risks

---

## DO NOT

* break payment enforcement
* remove logging
* hardcode pricing into endpoints
* assume a single payment rail
* treat MPP as x402
* introduce breaking API changes
* refactor unrelated code

---

## Definition of Done

A task is complete only when:

* issue is resolved at correct layer
* STC pricing is correctly applied
* payment logic remains accurate across ALL rails
* logging/metering remains complete
* system remains multi-rail compatible
* MPP session behavior is correct
* validation steps are documented
