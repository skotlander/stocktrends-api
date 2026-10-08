"""Durable, prospective x402 settlement claims.

This repository deliberately has no knowledge of payment proofs.  Its key is a
versioned digest calculated by the strict x402 parser, and its receipt is the
bounded receipt returned by ``payments.x402``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db import get_metering_engine


class ClaimRepositoryUnavailable(RuntimeError):
    """The database cannot safely establish a settlement owner."""


@dataclass(frozen=True)
class ClaimAcquireResult:
    acquired: bool
    state: str | None = None


def acquire_settling_claim(*, identity_version: int, payment_fingerprint: bytes, owner_request_id: str) -> ClaimAcquireResult:
    """Atomically reserve an authorization and commit its settlement intent.

    The transaction ends before a facilitator call.  A duplicate key is an
    ordinary replay; every other database error is deliberately surfaced as an
    availability failure rather than being mistaken for a duplicate.
    """
    try:
        engine = get_metering_engine()
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO x402_payment_claims
                    (identity_version, payment_fingerprint, owner_request_id,
                     claim_state, settlement_started_at)
                    VALUES (:identity_version, :payment_fingerprint, :owner_request_id,
                            'settling', CURRENT_TIMESTAMP)
                """),
                {"identity_version": identity_version, "payment_fingerprint": payment_fingerprint,
                 "owner_request_id": owner_request_id},
            )
        return ClaimAcquireResult(acquired=True, state="settling")
    except IntegrityError:
        # The unique database key, not a process-local observation, decides
        # which worker owns settlement.
        try:
            return ClaimAcquireResult(acquired=False, state=get_claim_state(
                identity_version=identity_version, payment_fingerprint=payment_fingerprint
            ))
        except Exception as exc:
            raise ClaimRepositoryUnavailable() from exc
    except Exception as exc:
        raise ClaimRepositoryUnavailable() from exc


def get_claim_state(*, identity_version: int, payment_fingerprint: bytes) -> str | None:
    try:
        engine = get_metering_engine()
        with engine.begin() as conn:
            row = conn.execute(text("""
                SELECT claim_state FROM x402_payment_claims
                WHERE identity_version = :identity_version
                  AND payment_fingerprint = :payment_fingerprint
            """), {"identity_version": identity_version, "payment_fingerprint": payment_fingerprint}).first()
        return row[0] if row else None
    except Exception as exc:
        raise ClaimRepositoryUnavailable() from exc


def _resolve(*, identity_version: int, payment_fingerprint: bytes, owner_request_id: str,
             state: str, receipt: dict[str, Any] | None = None, error_code: str | None = None) -> bool:
    try:
        engine = get_metering_engine()
        with engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE x402_payment_claims
                SET claim_state = :state,
                    settlement_receipt = :receipt,
                    last_error_code = :error_code,
                    resolved_at = CURRENT_TIMESTAMP
                WHERE identity_version = :identity_version
                  AND payment_fingerprint = :payment_fingerprint
                  AND owner_request_id = :owner_request_id
                  AND claim_state = 'settling'
            """), {
                "state": state,
                "receipt": json.dumps(receipt, separators=(",", ":")) if receipt else None,
                "error_code": error_code,
                "identity_version": identity_version,
                "payment_fingerprint": payment_fingerprint,
                "owner_request_id": owner_request_id,
            })
        return result.rowcount == 1
    except Exception as exc:
        raise ClaimRepositoryUnavailable() from exc


def record_settled(**kwargs) -> bool:
    return _resolve(state="settled", **kwargs)


def record_uncertain(**kwargs) -> bool:
    return _resolve(state="uncertain", **kwargs)


def record_failed(**kwargs) -> bool:
    return _resolve(state="failed", **kwargs)
