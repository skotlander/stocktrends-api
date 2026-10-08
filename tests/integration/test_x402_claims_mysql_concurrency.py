"""Opt-in real MySQL/InnoDB concurrency proof for x402 claim acquisition.

Run only against the exact disposable database ``stocktrends_x402_claims_test``:
X402_MYSQL_CONCURRENCY_TEST_URL=mysql+mysqlconnector://.../stocktrends_x402_claims_test
X402_MYSQL_CONCURRENCY_TEST_ACK=I_UNDERSTAND_THIS_DROPS_TEST_TABLES
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
        migration = (Path(__file__).parents[2] / "docs" / "operations" / "2026-10-08_x402_payment_claims.sql").read_text(encoding="utf-8")
        create_statement = "CREATE TABLE " + migration.split("CREATE TABLE ", 1)[1].split(";", 1)[0]
        conn.execute(text(create_statement.replace("stocktrends_api_metering.", "")))
        created = True

    try:
        workers_per_identity = 4
        identities = [bytes([index]) * 32 for index in range(1, 4)]
        barriers = {fingerprint: threading.Barrier(workers_per_identity) for fingerprint in identities}
        settlement_calls: list[bytes] = []
        settlement_lock = threading.Lock()

        def acquire(fingerprint, owner):
            barriers[fingerprint].wait(timeout=10)
            result = claims.acquire_settling_claim(
                identity_version=1, payment_fingerprint=fingerprint, owner_request_id=owner
            )
            if result.acquired:
                with settlement_lock:
                    settlement_calls.append(fingerprint)
            return fingerprint, owner, result

        jobs = [(fingerprint, f"request-{identity}-{worker}") for identity, fingerprint in enumerate(identities) for worker in range(workers_per_identity)]
        with ThreadPoolExecutor(max_workers=len(jobs)) as workers:
            outcomes = list(workers.map(lambda job: acquire(*job), jobs))
        for fingerprint in identities:
            claims_for_identity = [result for current, _owner, result in outcomes if current == fingerprint]
            assert sum(result.acquired for result in claims_for_identity) == 1
            assert claims.get_claim_state(identity_version=1, payment_fingerprint=fingerprint) == "settling"
        assert len(settlement_calls) == len(identities)

        winner_fingerprint, winner_owner, _ = next(item for item in outcomes if item[2].acquired)
        assert claims.record_settled(identity_version=1, payment_fingerprint=winner_fingerprint, owner_request_id=winner_owner, receipt={"success": True})
        assert not claims.record_uncertain(identity_version=1, payment_fingerprint=winner_fingerprint, owner_request_id=winner_owner, error_code="late")
        assert not claims.record_failed(identity_version=1, payment_fingerprint=winner_fingerprint, owner_request_id="other-worker", error_code="late")
        assert claims.get_claim_state(identity_version=1, payment_fingerprint=winner_fingerprint) == "settled"
    finally:
        if created:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE x402_payment_claims"))
        engine.dispose()
