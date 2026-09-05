# Request Lifecycle — Stock Trends API

## Purpose

This document defines the exact lifecycle of an API request, including:

* authentication
* pricing
* request validation
* payment enforcement
* logging

Ordering is load-bearing, not incidental. Payment *enforcement* — verification
and settlement, the steps that can move money — is deliberately positioned after
every rejection the system can reach without doing paid work, so that no
deterministic client-input failure or route-miss condition can move money. Steps
6 through 9 exist for that reason and must not be reordered.

Challenge *issuance* is a separate half of the x402 protocol and sits earlier.
A `402` challenge quotes a price and describes a resource; it verifies nothing,
settles nothing and executes nothing. For a recognized fixed-price payable route
presented with no payment authorization or proof, the challenge is issued at step
4A, before application-input validation, so that a machine probing the canonical
resource URL receives the payment and Bazaar contract instead of an input error.
See "Challenge Issuance Is Not Settlement" below.

Resource discovery still occurs primarily outside this paid execution lifecycle.
Anonymous agents use `/.well-known/x402` to discover payment-governed resources,
canonical safe requests, runtime-supported rails, and STC pricing-rule references
without executing a resource. See `x402-discovery.md`.

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
* `GET /v1/stocktrends/strategies`
* `GET /v1/stocktrends/strategies/{strategy_id}`
* `GET /v1/stocktrends/portfolios/{port_id}/strategy`
* `GET /v1/selections/stim-select/outcomes/summary`
* `GET /v1/intelligence/discovery`
* `GET /v1/intelligence/editorial/latest/preview`

Current paid machine-payment Intelligence Artifact routes include:

* `GET /v1/intelligence/guidance/latest`
* `GET /v1/intelligence/guidance/{artifact_id}`
* `GET /v1/intelligence/research/latest`
* `GET /v1/intelligence/research/{artifact_id}`

The `/v1/intelligence/*` Public Intelligence Artifact Bridge routes are
read-only artifact-serving routes. They consume only exported
`PublicArtifactEnvelope.v1` files referenced by `manifest.json` under
`ST_INTELLIGENCE_ARTIFACTS_DIR`; they do not call Agent graph nodes, Agent
services, generation code, or raw Agent filesystem internals. Discovery metadata
and editorial preview remain public/free. Guidance and research artifacts are
paid intelligence products served through the normal subscription, x402, and MPP
economic boundary.

Paid Intelligence Artifact routes also have a pre-payment availability boundary.
Before the API issues a subscription/x402/MPP payment challenge or accepts a
machine-payment attempt, it confirms that the configured artifact store is
available and that the requested guidance or research artifact exists, validates,
and is serveable. If the store is missing or unreadable, the request returns
`503` with `intelligence_artifact_store_unavailable`. If a matching artifact is
absent or fails existing schema/hash/publication validation, the request fails
closed according to the store convention, typically `404` with
`intelligence_artifact_not_found`. These unavailable responses do not advertise
subscription, x402, or MPP methods and do not create paid economics records.
Only available artifacts proceed to the normal payment lifecycle.

Initial active STC rules for paid guidance and research artifacts are documented
in `docs/operations/intelligence_pricing_rules.sql`:

* `intelligence_guidance_latest` -> 0.25 STC
* `intelligence_guidance_by_id` -> 0.25 STC
* `intelligence_research_latest` -> 0.50 STC
* `intelligence_research_by_id` -> 0.50 STC

The artifact store caches validated manifest snapshots while the manifest and
referenced artifact file signatures are unchanged, and reloads/revalidates on
store changes. Serveability is publication-status specific:

* discovery_metadata: `published` or `publish_ready`
* editorial_preview: `published` or `publish_ready`
* market_guidance: `published` or `product_grade`
* market_research_report: `published` or `product_grade`

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
    (total_realized_gain_loss / avg_investment)
    / ((total_weeks * 7) / 365.25)
    * 100
```

For the public summary implementation:

* `total_realized_gain_loss` comes from `SUM(stp_positions.gain_loss)`
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

Official Stock Trends strategy metadata is public/free provenance metadata. It
is sourced from:

* `Strategy`
* `StrategyCondition`
* `stp_ports.strategy_id`

The canonical mapping is:

```text
stp_ports.strategy_id
    = Strategy.StrategyId
    = StrategyCondition.StrategyId
```

Public strategy metadata exposes declared buy/sell rule rows and economic
assumptions only:

* `Strategy.Description`
* `Strategy.InvestmentAmt`
* `Strategy.TransactionCostPct`
* `Strategy.StopLossPct`
* `Strategy.StopLossMinimum`
* `StrategyCondition.BuySell`
* `StrategyCondition.LeftSide`
* `StrategyCondition.Operator`
* `StrategyCondition.RightSide`
* `StrategyCondition.sell_trigger`

`StrategyCondition.BuySell = 'B'` means a buy-condition row.
`StrategyCondition.BuySell = 'S'` means a sell-condition row.

Strategy conditions are exposed as legacy metadata for provenance and
verification. They are not executable query endpoints. Public strategy metadata
must not evaluate conditions against current market data and must not return
current matching stocks, current buy candidates, current sell candidates, or
current live holdings.

Do not make arbitrary strategy child paths public/free. In particular, these
remain protected unless intentionally reclassified later:

* `/v1/stocktrends/strategies/{strategy_id}/matches`
* `/v1/stocktrends/strategies/{strategy_id}/current`
* `/v1/stocktrends/portfolios/{port_id}/strategy/current`
* `/v1/stocktrends/portfolios/{port_id}/strategy/matches`

Public ST-IM Select signal outcome summary is public/free aggregate evidence.
It summarizes mature historical observations meeting the ST-IM Select
signal-selection rule:

* `stweekly.st_returnmeans.x4wk1 > 0`
* `stweekly.st_returnmeans.x13wk1 > 2.19`
* `stweekly.st_returnmeans.x40wk1 > 6.45`
* `stweekly.st_data.price >= 2`
* `stweekly.st_data.volume > 1000`
* `stweekly.st_data.fpr_chg13 IS NOT NULL`

The endpoint uses the canonical join:

```text
stweekly.st_data.weekdate = stweekly.st_returnmeans.weekdate
AND stweekly.st_data.exchange = stweekly.st_returnmeans.exchange
AND stweekly.st_data.symbol = stweekly.st_returnmeans.symbol
```

The legacy `outcomes` response uses `stweekly.st_data.fpr_chg13` as the realized
13-week forward return. Default no-date responses also expose multi-horizon
historical evidence for `stweekly.st_data.fpr_chg4`, `fpr_chg13`, and
`fpr_chg40`. The endpoint does not reconstruct forward returns from future price
joins. It is not limited to published reports and does not return current
selections, current matching symbols, current candidates, or individual
historical symbols.

When `start_date` and `end_date` are both omitted, the endpoint applies a
trailing 10-year window ending at the latest mature outcome date and returns
`filters.default_window_applied: true` with the applied dates. If either date is
supplied, the endpoint preserves the caller's date range and returns
`filters.default_window_applied: false`.

The default no-date summary is served from the persistent historical summary
table `stweekly.stim_select_outcome_summary`. The API reads this table only; it
does not create, populate, or refresh the table during request handling. Missing
table or missing summary rows return `503` with
`error: outcome_summary_not_available` and `refresh_required: true`.

Refresh may run manually, monthly, weekly, on demand, or after major data
updates with:

```text
python -m maintenance.refresh_stim_select_outcome_summary_cache
```

The table creation SQL is documented in:

```text
docs/operations/stim_select_outcome_summary_table.sql
```

Supported seeded no-date rows are:

* `exchange = NULL`, `limit_rank = NULL`
* `exchange = NULL`, `limit_rank = 10`

Other no-date `limit_rank` or exchange combinations require explicit date
filters or a custom summary refresh. Default responses expose `generated_at`
and `source_latest_mature_weekdate` in
provenance. `generated_at` is when the summary row was produced.
`source_latest_mature_weekdate` is the latest historical signal weekdate
included by the mature-outcome source query.

Explicit date-window requests may still execute the live historical aggregate.
The endpoint is historical evidence for the ST-IM Select signal-selection rule,
not current live selections.

Only this exact path is public/free:

* `/v1/selections/stim-select/outcomes/summary`

Do not make arbitrary ST-IM Select outcome child paths public/free. In
particular, these remain protected unless intentionally reclassified later:

* `/v1/selections/stim-select/outcomes`
* `/v1/selections/stim-select/outcomes/current`
* `/v1/selections/stim-select/outcomes/symbols`

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

### 4. Payment Rail Selection / Classification — No Collection

Based on request context, the rail that *would* be used is resolved:

#### A. Subscription Path

* no payment headers
* STC deducted from plan allocation

#### B. x402 Path

* payment headers present
* per-request payment validation

#### C. MPP Path

* active session
* STC consumed within session

This step classifies only. No payment is collected, no facilitator is
contacted, and no MPP authorization is opened here.

Rail selection happens in middleware, which runs *before* routing. At this
point the system does not yet know that the body parses or that the request is
answerable. Enforcing payment here is what allowed unservable requests to
settle; enforcement is therefore deferred to step 10 and published as a one-shot
gate on the request for the endpoint boundary to invoke.

---

### 4A. x402 Challenge Issuance — Unpaid Probes Only

A challenge is not a settlement, and this step is the whole of that distinction.

It applies to a request that satisfies all of the following:

* agent-pay enforcement is active and the request is payment-required;
* the resolved rail is x402;
* the request presents **no** payment authorization or proof — neither an x402
  proof header (`X-Payment`, `PAYMENT-SIGNATURE`, `Authorization: x402 …`) nor
  any `x-stocktrends-payment-*` material header. Naming a rail in
  `x-stocktrends-payment-method` declares intent, not payment, and does not
  count as payment-bearing;
* the path and method resolve to a real `APIRoute`, established from the
  application's own routers (`api/route_recognition.py`), never from a
  hand-written table;
* the route is classified early-challenge eligible (see below).

When all of those hold, the system issues the `402` challenge and stops. It
performs no payment verification, no settlement, no MPP authorization, no
endpoint execution and no database, service or artifact-store access. Its whole
payment action is the pure challenge builder, which composes the requirements,
the canonical resource URL and the Bazaar extension from the static endpoint
registry.

No deferred payment gate is published for such a request: nothing would ever
invoke it, and publishing one would make the finaliser read the challenge as a
pre-gate rejection instead of the live `pending` a challenge is.

**Eligibility classification.** Implemented in `payments/challenge.py` as
`EarlyChallengeClass`; every governed route resolves to exactly one class, and
every exclusion is named rather than implied.

* `fixed_price` — **eligible**. An exact `EndpointPaymentPolicy` governs the
  path and method, enables the x402 rail, and names a pricing rule, so the
  quoted amount is fully determined by `(path, method)` without a single
  request value. All currently governed non-Intelligence routes are in this
  class: prices, indicators, ST-IM, selections, published selections, STWR
  reports, breadth, leadership, market regime, screener, decision and portfolio.
* `availability_gated` — **excluded**. Paid Intelligence artifact routes confirm
  the artifact store is reachable and the artifact exists *before* any payment
  challenge (step 5), and answer `503`/`404` when it is not. The system does not
  quote a price for an intelligence product it cannot serve.
* `parameterized_resource` — **excluded**. The route template carries path
  parameters, so there is no bare canonical URL for a machine to probe and the
  rationale does not apply.
* `no_endpoint_policy` — **excluded**. Only prefix governance applies
  (`/v1/stim`), so no pricing rule fixes an amount. Challenging on prefix alone
  would turn middleware into a "paid-looking URL → `402`" mechanism.
* `no_x402_rail` / `no_pricing_rule` — **excluded**, fail-closed.
* `unrecognized_route` — **excluded**. Route recognition says the dispatcher
  would answer `404` or `405`, and it must.

A request that does present payment material skips this step entirely and takes
the full path below: structural validation, semantic validation, the deferred
gate, then the endpoint.

---

### 5. Intelligence Artifact Availability Boundary

A separate, pre-existing gate that applies only to the paid
`/v1/intelligence/*` artifact routes.

When the configured artifact store is unavailable — or the requested published
artifact does not exist — the request fails **before** the normal payment
lifecycle, with no payment challenge and no settlement.

This is deliberately fail-closed: the system does not quote a price for, or
collect payment against, an intelligence product it cannot serve.

It is distinct from the request-only semantic validation in step 9:

* the availability boundary is **store/data dependent** — it asks whether the
  artifact exists and is serveable;
* semantic validation is **request-only** — it asks whether the caller's input
  is well formed.

Both run before payment, for different reasons. Neither replaces the other, and
store access must never be moved into a semantic validator.

---

### 6. FastAPI Route Matching

The path and method are matched against the registered route surface.

A route miss or method miss is resolved here, before any payment step. A typo
under a paid path prefix must never settle.

---

### 7. JSON / Body Parsing

The request body is decoded.

Malformed JSON is rejected here, before payment.

---

### 8. Query / Path / Pydantic Structural Validation

FastAPI validates declared query, path and body parameters against their types
and constraints.

Constraint violations (`limit=0`, a body failing its model) are rejected here
with the framework's `422`, before payment.

---

### 9. Request-Only Semantic Validation

Structural validity is not the same as answerability. `symbol_exchange=IBM` is
a well-formed string that names no instrument; an empty
`EvaluateSymbolRequest` body satisfies its model but identifies nothing.

Endpoints with such rules register a pre-payment semantic validator, which runs
here on the already-validated request values and raises the endpoint's existing
`HTTPException` when the request is unanswerable.

Constraints on this step:

* it must be **request-only** — decidable from the submitted values alone;
* it must be synchronous and side-effect free: no database, no service, no
  network, no mutable application state;
* it must reuse the same helper the endpoint itself uses, so there is one
  definition of validity rather than a pre-payment copy that can drift.

Rejections that require querying or executing paid work — a symbol that does
not exist, an unavailable weekdate, an empty candidate set, an ambiguous
symbol-only lookup — deliberately do **not** run here. They remain after
payment and remain chargeable, because discovering them consumed the paid
service.

---

### 10. Deferred Payment Enforcement

The one-shot gate published in step 4 is invoked here, at the endpoint call
boundary — after every rejection above, and before any paid work.

System validates:

* sufficient STC (subscription)
  OR
* valid payment (x402 / MPP)

Outcomes:

* success → proceed
* failure → `402 Payment Required`

For x402, an artifact presenting less than the quoted amount is rejected before
the facilitator is contacted. That minimum is enforced by the enforcement path
itself and is not conditional on the optional payment-header validation flag.

---

### 11. Endpoint / Service Execution

* data fetched
* response generated

Everything from this point on is paid work, and its failures are chargeable.

---

### 12. MPP Finalization

Where applicable, the MPP authorization opened at step 10 is resolved —
captured on success, or compensated.

---

### 13. Metering + Logging

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

### 14. Response Returned

Includes:

* requested data
* payment headers (if applicable)
* request ID for tracking

---

## Challenge Issuance Is Not Settlement

The rule that decides between an input error and a payment challenge is
**payment-bearing state**, not input validity.

**An unpaid probe of a recognized fixed-price payable route receives the
challenge, whatever its inputs say.** Issuing it commits nothing and costs
nothing, and it is the only way a machine discovering the resource at its
canonical URL can see that the resource is payable, what it costs, which rails
it accepts, and which inputs it requires. The challenge describes the required
inputs even though the probe supplied none of them.

**A payment-bearing request completes structural and semantic validation before
anything capable of moving money runs.** A deterministically invalid one returns
its existing `400`/`422` and settles nothing, authorizes nothing and executes
nothing. Pricing context headers remain present on that error, so the price is
still discoverable once the request is corrected.

So the same incomplete request diverges on one axis only:

```text
unpaid + incomplete           -> 402 challenge, nothing verified or settled
payment-bearing + incomplete  -> 400/422,       nothing verified or settled
payment-bearing + valid       -> verify/settle once, endpoint executes once
```

**Why validation-before-settlement was necessary but not sufficient.** The
earlier remediation correctly established that no deterministic client-input
failure may cause settlement, and achieved it by moving the whole payment step
behind validation. But "the payment step" bundles two different things: the half
that can move money, and the half that merely quotes a price. Moving the
quoting half behind validation had no economic benefit and a real external cost —
a canonical probe of `/v1/prices/history` was answered `400 missing_required_param`
before any payment contract existed, so an x402 indexer saw no `402`, no payment
requirements and no Bazaar metadata, and recorded the resource as not payable.
The `402` was still a correct *execution-time* answer; it was simply never
reachable at the URL machines actually probe. PR3 keeps the settlement half
exactly where it was and moves only the issuance half.

The `402` challenge is still not a substitute for the input-schema sources.
Agents should read `/.well-known/x402`, `/v1/ai/tools` or the canonical OpenAPI
document for input schemas, and `/v1/pricing/catalog` for current pricing, then
construct a serviceable request before paying.

Accounting consequence: a request denied before the payment gate — route miss,
structural validation failure, or semantic rejection — records
`payment_status = rejected` on every rail. It is terminal and non-consuming, and
is excluded from the billing runbook's `presented`/`covered` usage queries. A
challenge, by contrast, records `pending`, because the agent may still pay for
it. That holds whichever step issued the challenge: an early challenge and a
gate-issued challenge are the same protocol event and are recorded identically —
`pending`, `billed_amount_usd = 0`, no payment reference, no collected amount.

---

## Payment Status Definitions

| Status            | Meaning                    |
| ----------------- | -------------------------- |
| covered           | subscription covered usage |
| presented         | billable agent payment     |
| failed_validation | invalid payment attempt    |
| rejected          | request denied             |

`rejected` is terminal and non-consuming: the request was denied before payment
execution, so no rail was contacted and nothing was served. It covers a route
miss, a structural validation failure, and a request-only semantic rejection,
on every rail.

It is deliberately outside the billing runbook's `payment_status IN
('presented', 'covered')` usage queries, and is grouped with
`failed_validation` in that runbook's failed-payment diagnostics.

Do not confuse it with `pending`, which means a challenge was issued and the
request may still be paid for.

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
