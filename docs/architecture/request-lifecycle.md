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
* `GET /v1/stocktrends/portfolios/{port_id}/summary`
* `GET /v1/stocktrends/portfolios/{port_id}/positions/history`

Official Stock Trends portfolio returns history is sourced from
`stp_returnslog`, the canonical portfolio performance history. Do not
reconstruct portfolio returns from `stp_positions`, which is a holdings/audit
trail source rather than the public performance-history source.

Official Stock Trends historical closed-position records are sourced from
`stp_positions`, filtered to closed rows only:

* `sell_trigger <> ''`

Rows where `sell_trigger = ''` are current live holdings and must remain
protected. Do not make arbitrary `/positions/*` child paths public; only
`/positions/history` is public/free.

Current public response mapping:

* `stp_returnslog.weekdate` -> `returns[].weekdate`
* `stp_returnslog.buys` -> `returns[].buys`
* `stp_returnslog.sells` -> `returns[].sells`
* `stp_returnslog.held` -> `returns[].held`
* `stp_returnslog.net_proceeds` -> `returns[].net_proceeds`
* `stp_returnslog.realizedgain` -> `returns[].realized_gain`
* `stp_returnslog.cum_realizedgain` -> `returns[].cumulative_realized_gain`
* `stp_returnslog.totalvaluation` -> `returns[].total_valuation`
* `stp_returnslog.unrealizedgain` -> `returns[].unrealized_gain`
* `stp_returnslog.cum_totalgain` -> `returns[].cumulative_total_gain`
* `stp_returnslog.tsxindex` -> `returns[].tsx_index`
* `stp_returnslog.spindex` -> `returns[].sp_index`

Current public closed-position mapping:

* `stp_positions.position_id` -> `positions[].position_id`
* `stp_positions.symbol` -> `positions[].symbol`
* `stp_positions.exchange` -> `positions[].exchange`
* `stp_positions.name` -> `positions[].name`
* `stp_positions.date_in` -> `positions[].date_in`
* `stp_positions.price_in` -> `positions[].price_in`
* `stp_positions.qty` -> `positions[].qty`
* `stp_positions.trcost_in` -> `positions[].transaction_cost_in`
* `stp_positions.cost_adjs` -> `positions[].cost_adjustments`
* `stp_positions.total_cost` -> `positions[].total_cost`
* `stp_positions.stop_loss` -> `positions[].stop_loss`
* `stp_positions.date_out` -> `positions[].date_out`
* `stp_positions.weeks_held` -> `positions[].weeks_held`
* `stp_positions.sell_trigger` -> `positions[].sell_trigger`
* `stp_positions.price_out` -> `positions[].price_out`
* `stp_positions.trcost_out` -> `positions[].transaction_cost_out`
* `stp_positions.sell_adjs` -> `positions[].sell_adjustments`
* `stp_positions.total_proceeds` -> `positions[].total_proceeds`
* `stp_positions.gain_loss` -> `positions[].gain_loss`
* `stp_positions.gl_percent` -> `positions[].gain_loss_percent`
* `stp_positions.weekdate` -> `positions[].weekdate`

Do not expose `stp_positions.last_update` in the public closed-position
response.

Official Stock Trends portfolio public history summary is also public/free.
It summarizes:

* active portfolio metadata from `stp_ports WHERE port_id = :port_id AND status = 1`
* public return-history aggregates from `stp_returnslog`
* closed-position aggregates from `stp_positions` filtered to:
  * `sell_trigger IS NOT NULL`
  * `sell_trigger <> ''`

The summary ROI block uses the canonical Stock Trends average-investment method:

```text
avg_investment = avg_net_cost * avg_positions

annualized_roi_percent =
    (total_gain_loss / avg_investment)
    / ((total_weeks * 7) / 365.25)
    * 100
```

For the public summary implementation:

* `total_gain_loss` comes from `SUM(stp_positions.gain_loss)`
* `avg_net_cost` comes from `AVG(stp_positions.total_cost)`
* `avg_positions` is derived from closed position-weeks over elapsed weeks
* `total_weeks` is the elapsed closed-position period from earliest `date_in`
  to latest `date_out` in the filtered closed-position set
* ROI uses the same closed-position filter and `date_out` filters as the
  closed-position summary
* `annualized_roi_percent` is null when `avg_investment` or `total_weeks` is
  zero or null

Current live holdings are excluded from the summary. Do not make arbitrary
`/summary/*` child paths public/free.

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
