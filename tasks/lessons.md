# Lessons

Per `CLAUDE.md`, failures — especially payment bugs, logging gaps and pricing
inconsistencies — are recorded here with their root cause and the rule that
prevents a recurrence.

---

## 2026-08-28 — An economic minimum was switchable by a validation flag

**Problem.** With `VALIDATE_AGENT_PAY_HEADERS=false` and x402 enforcement
active, an artifact presenting less than the quoted price reached the
facilitator and settled. The request was underpaid and served.

**Root cause.** The amount-sufficiency comparison lived only in
`validate_x402_payment()`, and the gate called that function only when
`should_validate_agent_pay` was true — a condition that includes the
`VALIDATE_AGENT_PAY_HEADERS` flag. `enforce_x402_payment()` extracted the
presented amount but never compared it to anything, so with validation off there
was no comparison anywhere on the path. Production ran with the flag on, which
is why the hazard was latent rather than active.

**Fix.** One shared rule, `x402_insufficient_amount_detail()` in
`payments/x402.py`, applied at both points: optionally by
`validate_x402_payment()`, and unconditionally by `enforce_x402_payment()`
before any facilitator contact.

**Prevention rule.** A configuration flag may govern optional *validation*
behaviour. It must never be the only thing standing between a request and an
economic outcome. If a rule decides whether money may move, the enforcement
path must apply it itself, whatever the flag says — and from the same definition
the validation path uses, so the two cannot drift.

**Corollary on scope.** Only the amount rule was made non-optional. Whether an
artifact *decodes* remains the facilitator's judgement, because that is a
protocol question rather than a locally decidable economic one. Underpayment is
decidable against a price we quoted; validity of a signed payload is not.

---

## 2026-08-28 — Deterministic input errors were charged for

**Problem.** A request that could never be served — `symbol_exchange=IBM`, an
empty `evaluate-symbol` body, `bias=sideways` — settled x402 or opened an MPP
authorization before the endpoint rejected it.

**Root cause.** Payment enforcement ran ahead of the validation that would
reject the request. PR 2 moved the gate behind FastAPI's own validation; the
remainder was semantic validation living inside endpoint bodies, which run after
the gate by construction.

**Fix.** A registered pre-payment semantic validator per endpoint
(`api/routing.py`), invoked on the already-solved values immediately before the
gate. Each validator calls the *same* helper the endpoint calls, so there is one
definition of validity rather than a pre-gate copy that can drift.

**Prevention rule.** When deciding where a rejection belongs, ask whether it is
knowable *from the request alone, before paid execution* — not whether it is
"deterministic". A symbol that does not exist in the database produces the same
answer every time and is still a paid answer, because discovering it required
the query the caller is being charged for.

**Prevention mechanism.** `tests/test_semantic_validation_boundary.py` asserts
that the audited sets A and B exactly cover the runtime-derived
payment-governed route surface. Pricing a new endpoint therefore fails the suite
until somebody records which class its rejections fall into. Closing the
architectural class is the deliverable; fixing the measured cases is not.

**Knock-on to watch for.** Moving validation ahead of the gate changes response
*precedence*: an unpaid, unservable request now returns its input error rather
than a 402 challenge. That is intended and contract-visible. It broke
`tests/test_402_preview.py`, which used a parameterless paid path purely as a
vehicle for inspecting challenge shape; those requests now name a real
instrument. A test that reaches a paid endpoint incidentally must send a request
that endpoint could actually serve.

---

## 2026-08-28 — A pre-gate rejection was reported as billable usage

**Problem.** A semantically invalid MPP request recorded
`payment_status = "presented"`. No authorization was opened, nothing was
captured, and `billed_amount_usd` was 0 — but the billing runbook's usage
queries select `payment_status IN ('presented', 'covered')` and aggregate
`COUNT(*)` and `SUM(stc_cost)` over the result. A request that never reached
the control plane therefore entered reported customer usage and STC
consumption.

**Root cause.** The finaliser derived `payment_status` per rail. The MPP branch
fell through to `presented` whenever no capture outcome had been recorded, a
branch commented "Enforcement disabled or pre-control-plane path" — which
silently absorbed a state it was never written for. Once PR 2 and PR 3 moved
rejections ahead of the gate, that state became common rather than exotic. The
same defect applied to framework-invalid requests and to route misses under a
paid prefix.

**Fix.** The finaliser now derives a `pre_gate_rejection` state from the gate
itself — published, never invoked, boundary intact, error response — and
records the canonical `rejected` status for it on every rail, ahead of any
rail-specific derivation.

**Prevention rule.** A payment status is not a label; it is a predicate that
billing and usage reporting select on. Before adding or defaulting one, check
which runbook queries would now match. A fall-through `else` in status
derivation is where this goes wrong, because it inherits every state nobody
thought about.

**Prevention rule (corollary).** Fix the status at the point it is written, not
by narrowing the query that reads it. A query edited to exclude a wrong status
leaves the wrong value in the table for every other consumer.

**Prevention mechanism.** `test_33_*` asserts pre-gate rejections against the
runbook's predicate itself, transcribed into the test, rather than against a
hand-written status list — so the two cannot drift apart silently.

---

## 2026-08-28 — A purity guard that could not fail

**Problem.** `test_44e` asserted that semantic validators do no data or service
work by scanning each validator's source for forbidden tokens. Two independently
authored mutations — `get_engine()` inside the shared screener helper, and
`configured_intelligence_artifact_store()` inside a registered validator — both
performed unpaid data access, and both passed the entire suite including that
test.

**Root cause.** The scan read only the validator function's own text. Every
validator is deliberately a thin adapter that delegates to a shared helper, so
anything one call away was invisible; and the token list could never be complete.
The test asserted a property of the source, while the architectural claim is
about behaviour.

**Fix.** `test_46` poisons the data and service entry points on the governed
router surface and drives a representative Class 1 invalid request at every
route in set A through the real HTTP boundary, asserting no sentinel is touched.
The textual scan is retained, renamed, and documented as a secondary net.

**Prevention rule.** When a test asserts an absence, ask what would have to be
true for it to fail, and then make that happen on purpose. A guard that has
never been shown to fail has not been shown to work. Pair every absence
assertion with a positive control — `test_46c` proves the poison is on the path
the paid work actually uses, so an absence cannot be produced by patching the
wrong names.

---

## 2026-09-04 — A paid endpoint's default was a 48 MB payload

**Problem.** An external agent settled x402 for `GET /v1/breadth/sector/history`
with an empty query string and received HTTP 200 carrying 47,651,791 bytes in
8.47 seconds. The response contained nothing saying a limit had been applied, so
the caller could not tell a complete result from a truncated one.

**Root cause.** Two independent omissions that only matter together. The route
declared `limit` with a default of 200000 and applied no date bounding at all, so
a request with no query string asked for the entire multi-decade series. The
response envelope echoed `start` and `end` — both `None` — and nothing else, so
the applied ceiling was invisible.

The discovery surfaces did not catch it, because they were describing it
faithfully: `safe_default: 200000` was published in the endpoint registry and in
OpenAPI. Every layer agreed, and every layer was wrong together.

**Fix.** A shared bounds table in `utils/history_bounds.py` that the routers and
the discovery registry both read, a default trailing window applied only when the
caller supplied neither `start` nor `end`, and an additive `applied_bounds` block
reporting window, limit, their sources, the maximum, the row count, and whether
truncation occurred. Truncation is detected by requesting one row beyond the
limit rather than inferred from a row count.

**Prevention rule.** A default is part of the paid contract. When pricing a
route that returns a collection, ask what a request with no query string
actually costs the caller to receive — not whether the parameter has an upper
bound. `ge=1, le=500000` bounded the parameter and left the response unbounded
in practice.

**Prevention rule (corollary).** "Discovery matches runtime" is necessary and not
sufficient. Three surfaces agreeing on a number proves only that they were
transcribed from each other. The bound is now derived from one table by all
three, so the question that remains is whether the number is right — which is a
question a person has to answer.

**Prevention mechanism.** `test_pr1_history_bounds.py` holds an audit table of
every payment-governed history route with a recorded verdict, enrolled from the
policy provider rather than from a list in the test. Pricing a new history
endpoint fails the suite until somebody records which class it falls into.

---

## 2026-09-04 — Two aggregates disagreed about what "all exchanges" meant

**Problem.** `/v1/breadth/sector/history` with no `exchange` filter returned
several rows for the same `(weekdate, sector_code)` — one per exchange — and the
projection did not carry `ss.exchange`, so they reached the caller as unlabelled
duplicates that could not be told apart or recombined. `/v1/breadth/sector/latest`
aggregated across exchanges correctly for the same nominal question.

**Root cause.** A performance fast path. `st_sector_summary` is aggregated per
`(weekdate, sector, exchange, type)`, which answers a single-exchange request
exactly. The predicate that selected it checked `group_level`, `cs_only`,
`include_unknown`, `min_price` and `min_volume` — every condition that would
change the *shape* of the aggregate, and not the one that changed its
*population*.

**Fix.** `_use_sector_summary()` now also requires an explicit exchange. An
all-exchange request falls through to the raw `st_data` aggregation the `/latest`
endpoint already used, which computes COUNT/SUM/AVG/MAX once over the whole
population. Recombining the stored per-exchange rows was rejected: a weighted
merge is only exact if each stored average was computed over the same row count
as `total`, and the summary table does not record that.

**Prevention rule.** When a pre-aggregated table stands in for a live
aggregation, the guard must cover the grouping keys the stored rows are
partitioned by, not only the filters the caller can vary. A summary row answers
a question; check that it is the question being asked.

**Prevention rule (corollary).** Two endpoints that name the same quantity must
compute it the same way. `/latest` and `/history` disagreeing about
"all-exchange sector breadth" was invisible because neither was checked against
the other; they are now asserted to build the same aggregate expressions.

---

## Non-blocking follow-ups (recorded, not actioned)

Raised by the independent review of PR #97 and deliberately left out of the
amendment to keep it narrow. None affects settlement ordering or accounting
correctness.

* **Validator receives a mutable `values` mapping.** A registered validator
  could in principle mutate the values the endpoint is then called with. No
  validator does. Introducing `MappingProxyType` here would change the seam for
  a hazard nothing currently exercises.
* **Duplicated identifier/exchange helpers across router modules.**
  `_norm_exchange` and the symbol/exchange resolver are near-identical in
  several routers. Consolidating them is a cross-router refactor with its own
  regression surface, unrelated to payment ordering.
* **Ungoverned ST-IM Select outcome validation.** Those routes are not currently
  payment-governed, so they fall outside the audited surface. If they are ever
  priced, `test_44` fails until they are classified.
* **PR 2 runtime-backstop behavioural-test polish.** The fail-closed backstop is
  covered; the review suggested stronger behavioural assertions around it.
* **MPP capture-failure uncertain reservation.** Known and explicitly out of
  scope: `authorize = 1`, capture attempted and failed, `void = 0`,
  `billed = 0`. Needs idempotency/uncertain-outcome design; do not blindly void
  after a failed capture.

---

## 2026-09-05 — A discovery contract tested the builder, not the response

**Problem.** PR2's advertised-example probeability suite called
`build_x402_requirements` directly, read the method, path and input example out
of that object, and then separately issued a request. Every assertion passed
while proving nothing about what a consumer actually receives: the builder was
compared with itself, and runtime enforcement — the code that composes the real
`PAYMENT-REQUIRED` header and the 402 body — was never in the measurement.

**Root cause.** The suite reached for the most convenient source of the metadata
rather than the authoritative one. A challenge is not metadata until enforcement
has selected the mode, built the requirements, base64-encoded the header, and
composed the body. Anything short of that is a fixture standing in for the
system under test.

**Fix.** The contract now seeds one unpaid request per governed resource, asserts
402, decodes the real `PAYMENT-REQUIRED` header, checks it against the body's
canonical `payment_required` block, rebuilds the request from that decoded
metadata alone, and replays it. Both `X-StockTrends-Challenge-Mode` modes are
exercised and must agree. Builder-level tests remain in
`tests/test_x402_requirements.py` as the lower-level contract.

**Prevention rule.** When a contract is about what a client receives, measure the
response. A test may use a builder to construct an input, never as the source of
the output it is checking. If corrupting the code between the builder and the
response would not fail the test, the test is not testing the contract.

**Corollary on negative tests.** The same suite had asserted that malformed
*unpaid* requests return 400/422 rather than a challenge. That is current
behaviour, not an invariant — unpaid challenge issuance is a separate design
question. The durable economic invariant is about *payment-bearing* requests:
a deterministically invalid one must not reach verification, settlement, MPP
authorization or capture, or paid execution. Negative tests now present a real
payment artifact on each rail, so a regression would move money rather than
merely change a status code. Do not freeze a status a future PR may legitimately
revisit; freeze the thing that costs money if it breaks.
