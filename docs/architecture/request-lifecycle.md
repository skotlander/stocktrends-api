# Request Lifecycle — Stock Trends API

## Purpose

This document defines the exact lifecycle of an API request, including:

* authentication
* pricing
* payment enforcement
* logging

---

## Step-by-Step Lifecycle

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
