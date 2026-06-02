# Request Lifecycle — Stock Trends API

## Purpose

This document defines the exact lifecycle of an API request, including:

* authentication
* pricing
* payment enforcement
* logging

---

## Step-by-Step Lifecycle

### 0. Endpoint Access Classification

Before implementation, every endpoint must be classified as one of:

* public/free discovery
* protected authenticated
* paid machine-payment

That classification must agree across:

1. Payment Policy Provider
2. Pricing Classifier
3. API-Key Middleware

A zero-cost pricing rule does not override payment-policy enforcement. Public
endpoints must not be registered as payment-gated EndpointPaymentPolicy routes.

Current public/free Stock Trends portfolio endpoints include:

* `GET /v1/stocktrends/portfolios`
* `GET /v1/stocktrends/portfolios/{port_id}`
* `GET /v1/stocktrends/portfolios/{port_id}/returns`

Official Stock Trends portfolio returns history is sourced from
`stp_returnslog`, the canonical portfolio performance history. Do not
reconstruct portfolio returns from `stp_positions`, which is a holdings/audit
trail source rather than the public performance-history source.

Current public response mapping:

* `stp_returnslog.weekdate` -> `returns[].weekdate`
* `stp_returnslog.return_pct` -> `returns[].return_pct`
* `stp_returnslog.value` -> `returns[].value`

---

### 1. Request Received

Example:

```
GET /v1/stim/latest?symbol_exchange=IBM-N
```

Headers may include:

* API key
* payment headers (x402 / MPP)

---

### 2. Authentication Layer

Checks:

* API key validity
* subscription status
* plan entitlements

Outcomes:

* authenticated → proceed
* invalid → reject (401/403)

---

### 3. Pricing Resolution (STC)

System determines:

* endpoint pricing rule
* STC cost

Example:

```
/stim/latest → 1 STC
```

---

### 4. Payment Path Selection

Based on request context:

#### A. Subscription Path

* no payment headers
* STC deducted from plan allocation

#### B. x402 Path

* payment headers present
* per-request payment validation

#### C. MPP Path

* active session
* STC consumed within session

---

### 5. Payment Enforcement

System validates:

* sufficient STC (subscription)
  OR
* valid payment (x402 / MPP)

Outcomes:

* success → proceed
* failure → `402 Payment Required`

---

### 6. Endpoint Execution

* data fetched
* response generated

---

### 7. Metering + Logging

Record written to:

→ `api_request_economics`

Fields:

* request_id
* customer_id
* api_key_id
* stc_cost
* pricing_rule_id
* payment_rail
* payment_status
* billed_amount_usd

---

### 8. Response Returned

Includes:

* requested data
* payment headers (if applicable)
* request ID for tracking

---

## Payment Status Definitions

| Status            | Meaning                    |
| ----------------- | -------------------------- |
| covered           | subscription covered usage |
| presented         | billable agent payment     |
| failed_validation | invalid payment attempt    |
| rejected          | request denied             |

---

## Failure Scenarios

### Missing Payment

* no subscription
* no valid payment

→ `402 Payment Required`

---

### Invalid Payment Headers

→ `failed_validation` logged
→ request rejected

---

### Insufficient STC (future enforcement)

→ request rejected or throttled

---

## Observability

All requests must be traceable via:

* `request_id`
* `customer_id`
* `payment_status`

---

## Strategic Outcome

This lifecycle ensures:

* consistent pricing enforcement
* clean separation of concerns
* compatibility with future payment rails

---

## Key Principle

Every request must resolve to STC before execution
