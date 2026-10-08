-- Package 2A: apply manually through the normal production migration process.
-- Do not apply from an application worker.
CREATE TABLE stocktrends_api_metering.x402_payment_claims (
    identity_version TINYINT UNSIGNED NOT NULL,
    payment_fingerprint BINARY(32) NOT NULL,
    owner_request_id VARCHAR(64) NOT NULL,
    claim_state ENUM('claimed', 'settling', 'settled', 'uncertain', 'failed') NOT NULL,
    settlement_receipt JSON NULL,
    last_error_code VARCHAR(64) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settlement_started_at TIMESTAMP NULL DEFAULT NULL,
    resolved_at TIMESTAMP NULL DEFAULT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (identity_version, payment_fingerprint),
    KEY ix_x402_claim_owner_request (owner_request_id),
    KEY ix_x402_claim_state_updated (claim_state, updated_at)
) ENGINE=InnoDB;

-- Rollback only before enabling the feature and only after confirming no
-- settling/uncertain claims require reconciliation:
-- DROP TABLE stocktrends_api_metering.x402_payment_claims;
