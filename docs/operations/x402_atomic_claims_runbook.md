# x402 atomic-claims activation

This is a prospective safety control. It does not backfill or alter historical
economics rows, and the legacy replay lookup remains in place for those rows.

1. Deploy this code with `X402_ATOMIC_CLAIMS_ENABLED=false` and confirm normal
   x402 challenges still work.
2. Apply `2026-10-08_x402_payment_claims.sql` through the approved database
   migration process. Confirm the application role has only the required
   SELECT/INSERT/UPDATE permissions on `x402_payment_claims`.
3. Drain every old payment-serving worker. A feature flag alone does not make a
   mixed old/new fleet safe.
4. Deploy this version to all workers, then set
   `X402_ATOMIC_CLAIMS_ENABLED=true` and restart only the drained fleet.
5. Verify a canary uses `x402:v1:<sha256>` economics references and inspect
   `settling`, `settled`, and `uncertain` claims without logging proof data.

Rollback after activation means setting `X402_SETTLEMENT_SUSPENDED=true` and
draining workers; it must never restore the old non-atomic settlement path.
Challenges and discovery remain available while settlement is suspended.
Operators reconcile `uncertain` claims using the safe receipt and
`last_error_code`; no automatic retry or refund is authorized.

Only before activation, after confirming there are no active claims, may the
table be dropped using the commented rollback statement in the SQL artifact.
