"""Opt-in real MySQL/InnoDB concurrency proof for x402 claim acquisition.

Run only against the exact disposable database ``stocktrends_x402_claims_test``:
X402_MYSQL_CONCURRENCY_TEST_URL=mysql+mysqlconnector://.../stocktrends_x402_claims_test
X402_MYSQL_CONCURRENCY_TEST_ACK=I_UNDERSTAND_THIS_DROPS_TEST_TABLES
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, inspect, text

import payments.x402_claims as claims


@pytest.mark.integration
def test_innodb_allows_exactly_one_settlement_claim(monkeypatch):
    url = os.getenv("X402_MYSQL_CONCURRENCY_TEST_URL")
    ack = os.getenv("X402_MYSQL_CONCURRENCY_TEST_ACK")
    if not url or ack != "I_UNDERSTAND_THIS_DROPS_TEST_TABLES":
        pytest.skip("requires explicitly acknowledged disposable MySQL URL")
    # Delayed because ordinary unit-test environments intentionally stub
    # SQLAlchemy and never configure this opt-in integration test.
    from sqlalchemy.engine.url import make_url
    parsed = make_url(url)
    if not parsed.drivername.startswith("mysql") or parsed.database != "stocktrends_x402_claims_test":
        pytest.skip("requires the exact disposable stocktrends_x402_claims_test MySQL database")

    engine = create_engine(url, pool_pre_ping=True)
    monkeypatch.setattr(claims, "get_metering_engine", lambda: engine)
    created = False
    with engine.begin() as conn:
        if inspect(conn).has_table("x402_payment_claims"):
            pytest.fail("refusing to modify an existing x402_payment_claims table")
        conn.execute(text("""CREATE TABLE x402_payment_claims (
            identity_version TINYINT UNSIGNED NOT NULL, payment_fingerprint BINARY(32) NOT NULL,
            owner_request_id VARCHAR(64) NOT NULL,
            claim_state ENUM('claimed','settling','settled','uncertain','failed') NOT NULL,
            settlement_receipt JSON NULL, last_error_code VARCHAR(64) NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            settlement_started_at TIMESTAMP NULL DEFAULT NULL, resolved_at TIMESTAMP NULL DEFAULT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (identity_version, payment_fingerprint)
        ) ENGINE=InnoDB"""))
        created = True

    try:
        def acquire(owner):
            return claims.acquire_settling_claim(identity_version=1, payment_fingerprint=b"x" * 32, owner_request_id=owner)

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(acquire, ("request-a", "request-b")))
        assert sum(result.acquired for result in outcomes) == 1
        assert {result.state for result in outcomes} == {"settling"}
        assert claims.get_claim_state(identity_version=1, payment_fingerprint=b"x" * 32) == "settling"
    finally:
        if created:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE x402_payment_claims"))
