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
