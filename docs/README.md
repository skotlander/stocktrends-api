# Stock Trends API - Documentation Index

## Purpose

This `/docs` directory contains the **strategic, architectural, and operational documentation** for the Stock Trends API.

It is designed to support:

* human understanding
* AI agent reasoning
* consistent system evolution

---

## Guiding Principle

The Stock Trends API is not just an API.

It is:

-> a **pricing engine (STC)**
-> a **multi-rail payment system**
-> an **agent-native monetization platform**

---

## Documentation Structure

### Root Contracts

* `STOCK_TRENDS_SEMANTIC_CONTRACT.md`
  -> authoritative Stock Trends terminology and inference semantics

* `STOCK_TRENDS_COGNITION_ARCHITECTURE.md`
  -> provider-agnostic inference and cognition architecture

* `SIGNALS_SPEC.md`
  -> Stock Trends signal fields and provider-agnostic signal evidence rules

### `/strategy/`

Defines **what the system is and where it is going**

* `api-strategy.md`
  -> overall system vision and design principles

* `pricing-strategy.md`
  -> STC (Stock Trends Credits) pricing model

* `payment-rails.md`
  -> multi-rail architecture (subscription, x402, MPP)

* `stok-token-strategy.md`
  -> future token integration (STOK)

---

### `/architecture/`

Defines **how the system works**

* `system-overview.md`
  -> layered architecture and components

* `request-lifecycle.md`
  -> step-by-step request flow

---

### `/operations/`

Defines **how the system is run and audited**

* `stim_policy.md`
  -> access control and enforcement rules

* `billing_runbook.md`
  -> billing, reconciliation, and diagnostics

---

## Relationship to Other Files

### `AGENTS.md`

* Defines **strict system rules**
* Must always be followed by AI agents
* Covers:

  * pricing enforcement
  * payment architecture
  * logging requirements

---

### `CLAUDE.md`

* Defines **execution behavior**
* Loaded into every Claude Code session
* Focuses on:

  * alignment
  * constraints
  * workflow discipline

---

## How to Use This Documentation

### For Developers

* Start with:
  -> `/strategy/api-strategy.md`
* Then:
  -> `/architecture/system-overview.md`
* Use:
  -> `/operations/` for real-world workflows

---

### For AI Agents (Claude / Codex)

* Use `/docs` for:

  * system understanding
  * architectural reasoning
  * implementation planning

* Use `AGENTS.md` for:

  * hard constraints
  * non-negotiable rules

---

## Design Philosophy

### 1. Separation of Concerns

| Layer       | Purpose                 |
| ----------- | ----------------------- |
| `/docs`     | strategy + architecture |
| `AGENTS.md` | rules                   |
| `CLAUDE.md` | execution               |

---

### 2. STC-Centric System

All pricing resolves to:

-> **Stock Trends Credits (STC)**

---

### 3. Multi-Rail Future

The system is designed to support:

* subscription access
* per-request payments (x402)
* session payments (MPP)
* token-based incentives (STOK)

---

### 4. Agent-Native Design

The API is built for:

* AI agents discovering pricing
* AI agents making payments
* automated consumption at scale

---

## Evolution Strategy

This documentation will evolve as:

* STC pricing is refined
* MPP is implemented
* STOK integration is introduced
* AI agent workflows expand

---

## Key Rule

> If documentation conflicts with code:
>
> * verify against `AGENTS.md`
> * then update docs or code accordingly

---

## Final Note

This `/docs` directory is the **source of truth for system understanding**.

Keep it:

* structured
* minimal
* aligned with implementation

---
