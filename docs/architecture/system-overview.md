# System Overview — Stock Trends API

## Purpose

This document defines the high-level architecture of the Stock Trends API as a:

* multi-rail payment system
* unified pricing engine (STC)
* agent-native data access platform

---

## Core Principle

All API access is priced in:

→ **STC (Stock Trends Credits)**

All payment methods convert value → STC.

---

## System Layers

### 1. API Layer (FastAPI)

Handles:

* request routing
* authentication (API keys)
* endpoint execution

Examples:

* `/v1/stim/latest`
* `/v1/stim/history`

---

### 2. Pricing Layer (STC Engine)

Defines:

* cost per endpoint
* pricing rules

Example:

| Endpoint      | STC Cost |
| ------------- | -------- |
| /stim/latest  | 1 STC    |
| /stim/history | 5 STC    |

---

### 3. Payment Layer (Multi-Rail)

Handles value transfer via:

* **Subscription**

  * prepaid STC allocation
* **x402**

  * per-request micropayment
* **MPP**

  * session-based streaming payments
* **STOK (future)**

  * discount / incentive layer

---

### 4. Enforcement Layer

Ensures:

* request is authorized
* sufficient STC coverage exists

Actions:

* allow request
* reject with `402 Payment Required`
* route to appropriate payment adapter

---

### 5. Metering Layer

Logs every request in:

→ `api_request_economics`

Fields include:

* `stc_cost`
* `pricing_rule_id`
* `payment_rail`
* `payment_status`
* `billed_amount_usd`

---

## Payment Rail Abstraction

Each rail implements:

* `authorize()`
* `settle()`
* `report()`

This ensures:

→ pricing logic remains independent of payment method

---

## Data Flow Summary

1. Request received
2. API key validated
3. STC cost determined
4. Payment rail selected
5. Payment validated or STC deducted
6. Response returned
7. Request logged

---

## Design Constraints

* Pricing must NEVER depend on payment rail
* Logging is mandatory for every request
* Payment logic must be modular and replaceable

---

## Strategic Outcome

The API becomes:

→ a **programmable financial layer for data access**

→ enabling AI agents, developers, and applications to:

* discover pricing
* pay dynamically
* optimize usage
