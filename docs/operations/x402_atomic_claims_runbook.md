# x402 atomic-claims activation

This is a prospective safety control. It does not backfill or alter historical
economics rows, and the legacy replay lookup remains in place for those rows.

1. Deploy this reviewed code with `X402_ATOMIC_CLAIMS_ENABLED=false` and
   `X402_SETTLEMENT_SUSPENDED=false`; confirm normal x402 challenges still work.
2. Apply `2026-10-08_x402_payment_claims.sql` through the approved database
   migration process. Confirm the application role has only the required
   SELECT/INSERT/UPDATE permissions on `x402_payment_claims`.
3. Set `X402_SETTLEMENT_SUSPENDED=true` for the entire payment-serving fleet,
   then restart or replace workers. Verify every serving worker logs
   `settlement_suspended=True`; do not assume an environment update changes a
   running worker.
4. While all workers remain suspended, set `X402_ATOMIC_CLAIMS_ENABLED=true`
   everywhere and restart or replace every affected worker. Verify every
   serving worker logs `atomic_claims_enabled=True` and can access the claim
   table.
5. Only after those fleet-wide checks, remove settlement suspension and restart
   or replace all payment-serving workers. An unsuspended claims-disabled
   worker must never coexist with an unsuspended claims-enabled worker.
6. Verify a canary uses `x402:v1:<sha256>` economics references and inspect
   `settling`, `settled`, and `uncertain` claims without logging proof data.

Rollback after activation means setting `X402_SETTLEMENT_SUSPENDED=true`
fleet-wide, restarting or replacing every worker, and verifying every worker
is suspended before changing any other claim configuration. It must never
restore the old non-atomic settlement path. Challenges and discovery remain
available while settlement is suspended.
Operators reconcile `uncertain` claims using the safe receipt and
`last_error_code`; no automatic retry or refund is authorized.

Before production activation, run the real InnoDB concurrency proof only
against an isolated disposable database: set
`X402_MYSQL_CONCURRENCY_TEST_URL` to the exact disposable
`stocktrends_x402_claims_test` MySQL database and set
`X402_MYSQL_CONCURRENCY_TEST_ACK=I_UNDERSTAND_THIS_DROPS_TEST_TABLES`, then
run `python -m pytest tests/integration/test_x402_claims_mysql_concurrency.py`.
The test creates and drops only `x402_payment_claims` in that disposable DB.

Only before activation, after confirming there are no active claims, may the
table be dropped using the commented rollback statement in the SQL artifact.
