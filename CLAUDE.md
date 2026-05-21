# CLAUDE.md — Stock Trends API

## Purpose

This repository powers the Stock Trends API and agent monetization system.

This is a **production financial system**, not a simple API.

It must preserve:
- pricing correctness (STC)
- payment rail integrity
- complete logging/metering
- system stability
- architectural consistency with `/docs` and `AGENTS.md`

---

## DOCUMENT HIERARCHY (CRITICAL)

Understand the separation of concerns:

- `/docs` → system truth (strategy, architecture, operations)
- `AGENTS.md` → non-negotiable system rules
- `CLAUDE.md` → execution behavior

### Rule:

If anything is unclear:

1. Check `/docs`
2. Then `AGENTS.md`
3. Then proceed

Never override documented system design.

---

## MANDATORY FIRST STEP

Before making ANY changes:

1. Read:
   - `AGENTS.md`
   - Relevant `/docs` files (architecture, lifecycle, pricing if applicable)

2. Confirm:
   - pricing model = STC only
   - no rail-specific pricing logic
   - logging is mandatory

3. If unclear → ASK QUESTIONS before proceeding

---
## Stock Trends Semantic Contract

Before modifying any Stock Trends-related endpoint descriptions, AI discovery metadata, endpoint previews, tool manifests, OpenAPI descriptions, or documentation, read:

`docs/STOCK_TRENDS_SEMANTIC_CONTRACT.md`

Also read:

`docs/STOCK_TRENDS_COGNITION_ARCHITECTURE.md`

ST-IM is the current baseline inference provider, not the final intelligence layer.
Preserve future Causal AI compatibility by using provider-agnostic inference
concepts where practical: inference provider, forecast horizon, probability
distribution, confidence measure, evidence, uncertainty, explanation, signal
source, reasoning interpretation, and auditability.

Do not infer meanings for Stock Trends acronyms, indicators, fields, or statistical outputs.

Use the definitions in that file as the authoritative source.

---

## BIDIRECTIONAL ALIGNMENT (REQUIRED)

Before coding:

1. State what you think the task is
2. List assumptions
3. Identify affected system layers:
   - pricing
   - payment
   - middleware
   - logging

4. Ask clarifying questions if anything is ambiguous

Do NOT proceed until alignment is confirmed.

---

## PLAN MODE (MANDATORY)

For ANY non-trivial task (3+ steps, debugging, architecture, payments):

1. Produce a structured plan BEFORE implementation
2. Wait for confirmation before coding

Plan MUST include:
- objective
- exact layers impacted
- step-by-step execution
- risks
- validation strategy

If something breaks or deviates:
→ STOP and re-plan

---

## EXECUTION RULES

- Make minimal, targeted changes
- Preserve production behavior
- Do NOT rewrite large sections unnecessarily

STRICT separation:

- pricing (STC)
- payment rails
- enforcement logic
- logging/metering

Never mix these concerns.

---

## CORE SYSTEM INVARIANTS (NON-NEGOTIABLE)

Derived from `AGENTS.md`:

### 1. STC is the ONLY pricing source
- all endpoints resolve to STC
- no pricing in payment adapters
- no rail-specific pricing

### 2. Payment rails are interchangeable (ALL ACTIVE)

- subscription
- x402
- MPP (production)
- STOK (future incentive layer)

### 3. Logging is mandatory

Every request MUST produce:
- stc_cost
- pricing_rule_id
- payment_rail
- payment_status

No logging = invalid system state

---

## SUBSYSTEM ISOLATION (CRITICAL)

The system has distinct layers:

1. pricing resolution
2. payment translation
3. payment verification
4. enforcement
5. logging/metering

### Rules:

- NEVER combine layers
- NEVER embed pricing in payment logic
- NEVER embed payment logic in routes
- NEVER bypass logging

If a bug exists:
→ fix ONLY the responsible layer

---

## REQUEST LIFECYCLE AWARENESS

All changes must respect the lifecycle:

1. authentication
2. pricing resolution (STC)
3. payment path selection
4. payment enforcement
5. endpoint execution
6. logging/metering
7. response

Reference:
→ `/docs/architecture/request-lifecycle.md`

Any violation of this flow is a bug.

---

## PAYMENT MODEL AWARENESS (UPDATED)

The system supports MULTIPLE ACTIVE payment paths:

### Subscription
- STC deducted from account balance

### x402 (per-request)
- payment validated per request
- uses challenge/verify flow

### MPP (session-based, PRODUCTION)
- STC deducted from session balance
- may NOT follow x402 challenge flow
- requires session tracking and state consistency

### Key Rule

Do NOT assume:
- all payments are request-based
- all payments use headers like x402

MPP requires different reasoning.

---

## VERIFICATION BEFORE COMPLETION (MANDATORY)

Never mark work complete without proof:

### Must verify:

- correct STC cost applied
- correct payment rail selected
- correct enforcement behavior

### MUST validate ALL ACTIVE RAILS when impacted:

- subscription path
- x402 path
- MPP session path

### Always validate:

- `/v1/` responds
- `/v1/docs` loads
- `/v1/openapi.json` loads

Ask:
"Would this pass a production audit?"

---

## DEBUGGING DISCIPLINE (MANDATORY)

Always identify the failure layer FIRST:

1. nginx
2. service startup
3. FastAPI app
4. pricing
5. middleware
6. payment translation
7. payment verification
8. logging

### Additional Rule:

Identify WHICH PAYMENT RAIL is involved:

- subscription
- x402
- MPP

Fix:
→ the correct rail + correct layer

---

## PAYMENT ARCHITECTURE RULES

- Pricing must be resolved BEFORE payment logic
- Payment rails must consume STC
- Payment adapters must be modular

Never:
- hardcode pricing
- assume x402-only system
- treat MPP like x402
- collapse multi-rail logic into shortcuts

---

## LOGGING DISCIPLINE (FIRST-CLASS)

Every request must be:

- logged once
- logged accurately
- aligned with actual behavior

Logging must reflect:
- real enforcement outcome
- real payment state
- correct rail (subscription | x402 | mpp)

---

## DEMAND ELEGANCE (MANDATORY CHECKPOINT)

For any non-trivial change:

Ask:
"Is this aligned with the system architecture in /docs?"

Avoid:
- hacks
- duplication
- bypassing lifecycle

---

## AUTONOMOUS DEBUGGING

When given a bug:

- inspect logs
- inspect request lifecycle
- identify payment rail
- trace execution path

Fix:
→ root cause at correct layer

---

## SELF-IMPROVEMENT LOOP

After any failure:

Record in:
tasks/lessons.md

Include:
- problem
- root cause
- prevention rule

Focus especially on:
- payment bugs
- logging gaps
- pricing inconsistencies

---

## CONTEXT DISCIPLINE

- Avoid long sessions
- Do not rely on stale context
- Re-check `/docs` when unsure

---

## OUTPUT REQUIREMENTS

Always provide:

1. diagnosis
2. files changed
3. exact code changes
4. reasoning
5. validation steps
6. risks

---

## DO NOT

- break payment enforcement
- remove logging
- hardcode pricing
- assume a single payment rail
- treat MPP as x402
- introduce breaking API changes

---

## DEFINITION OF DONE

A task is complete ONLY when:

- STC pricing is correct
- ALL payment rails behave correctly
- logging is complete
- lifecycle is preserved
- validation confirms real behavior

---

## FINAL PRINCIPLE

This system is:

- a pricing engine (STC)
- a multi-rail payment system
- an agent monetization layer

It must behave like a **financial system**, not a typical API.

Priority:
1. pricing correctness
2. payment integrity
3. logging completeness
4. system stability
