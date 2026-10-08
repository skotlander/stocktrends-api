from __future__ import annotations

import pytest

import payments.x402_claims as claims


class MysqlError:
    def __init__(self, errno):
        self.errno = errno
        self.args = (errno, "database error")


class FakeIntegrityError(Exception):
    def __init__(self, errno):
        self.orig = MysqlError(errno)


def _integrity_error(errno):
    return FakeIntegrityError(errno)


def test_duplicate_key_returns_existing_claim(monkeypatch):
    monkeypatch.setattr(claims, "IntegrityError", FakeIntegrityError)
    monkeypatch.setattr(claims, "get_metering_engine", lambda: (_ for _ in ()).throw(_integrity_error(1062)))
    monkeypatch.setattr(claims, "get_claim_state", lambda **_kwargs: "settling")
    result = claims.acquire_settling_claim(identity_version=1, payment_fingerprint=b"x" * 32, owner_request_id="request")
    assert result == claims.ClaimAcquireResult(False, "settling")


def test_non_duplicate_integrity_error_fails_closed(monkeypatch):
    monkeypatch.setattr(claims, "IntegrityError", FakeIntegrityError)
    monkeypatch.setattr(claims, "get_metering_engine", lambda: (_ for _ in ()).throw(_integrity_error(1452)))
    with pytest.raises(claims.ClaimRepositoryUnavailable):
        claims.acquire_settling_claim(identity_version=1, payment_fingerprint=b"x" * 32, owner_request_id="request")


def test_duplicate_without_claim_fails_closed(monkeypatch):
    monkeypatch.setattr(claims, "IntegrityError", FakeIntegrityError)
    monkeypatch.setattr(claims, "get_metering_engine", lambda: (_ for _ in ()).throw(_integrity_error(1062)))
    monkeypatch.setattr(claims, "get_claim_state", lambda **_kwargs: None)
    with pytest.raises(claims.ClaimRepositoryUnavailable):
        claims.acquire_settling_claim(identity_version=1, payment_fingerprint=b"x" * 32, owner_request_id="request")
