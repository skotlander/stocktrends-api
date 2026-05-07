from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import middleware.metering as metering_module
import pricing.classifier as classifier_module
from middleware.metering import MeteringMiddleware
from middleware.request_id import RequestIdMiddleware
from payments.enforcement import PaymentEnforcementResult
from payments.x402 import extract_x402_payment_context


_PATH = "/v1/market/regime/latest"
_REQUEST_ID = "req-standard-x402"
_PAYMENT_TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _compact_json_header(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _standard_x402_payment_header(*, amount: str = "150000") -> str:
    return _compact_json_header(
        {
            "x402Version": 2,
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": _PAYMENT_TOKEN,
            "payload": {
                "authorization": {
                    "from": "0xbuyer",
                    "to": "0xseller",
                    "value": amount,
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0xnonce",
                },
                "signature": "0xsig",
            },
        }
    )


def _build_metered_app() -> FastAPI:
    app = FastAPI()

    @app.get(_PATH)
    async def market_regime_latest(request: Request):
        return {"ok": True, "request_id": request.state.request_id}

    app.add_middleware(MeteringMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app


def _econ(payment_status: str = "settled") -> dict:
    return {
        "request_id": _REQUEST_ID,
        "customer_id": None,
        "api_key_id": None,
        "pricing_rule_id": "market_regime_latest",
        "unit_price_usd": Decimal("0.15"),
        "billed_amount_usd": Decimal("0.15"),
        "stc_cost": Decimal("0.15"),
        "payment_required": 1,
        "payment_rail": "x402",
        "payment_status": payment_status,
        "payment_method": "x402",
        "payment_network": "eip155:8453",
        "payment_token": _PAYMENT_TOKEN,
        "payment_amount_native": 150000.0,
        "payment_amount_usd": Decimal("0.15"),
        "payment_reference": "x402-auth-ref",
        "session_id": None,
        "payment_channel_id": None,
        "agent_id": None,
        "agent_type": None,
        "agent_vendor": None,
        "agent_version": None,
        "request_purpose": None,
    }


def test_standard_x402_retry_without_stocktrends_method_logs_settled(monkeypatch):
    economics_rows: list[dict] = []

    monkeypatch.setattr(metering_module, "ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "ENFORCE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "VALIDATE_AGENT_PAY_HEADERS", True)
    monkeypatch.setattr(classifier_module, "ENABLE_AGENT_PAY", True)

    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda *_args, **_kwargs: (Decimal("0.15"), Decimal("0.15"), Decimal("0.15")),
    )
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        metering_module,
        "log_api_request_economics",
        lambda econ: economics_rows.append(dict(econ)),
    )

    def fake_enforce_payment_rail(**kwargs):
        assert kwargs["payment_rail"] == "x402"
        assert kwargs["validation_valid"] is True
        assert kwargs["validated_payment_amount_native"] == Decimal("150000")
        return PaymentEnforcementResult(
            outcome="proceed",
            payment_reference="x402-auth-ref",
            payment_network="eip155:8453",
            payment_token=_PAYMENT_TOKEN,
            payment_amount_native=Decimal("150000"),
            payment_response={
                "success": True,
                "transaction": "0x3ed456f52d6a6c330534e7544b4bb6d1c14af770ee08097ef3b012b4d27c3189",
            },
        )

    monkeypatch.setattr(metering_module, "enforce_payment_rail", fake_enforce_payment_rail)

    headers = {
        "X-Request-Id": _REQUEST_ID,
        "X-Payment": _standard_x402_payment_header(),
    }
    assert "X-StockTrends-Payment-Method" not in headers

    with TestClient(_build_metered_app()) as client:
        response = client.get(_PATH, headers=headers)

    assert response.status_code == 200
    assert "payment-response" in response.headers

    assert len(economics_rows) == 1
    row = economics_rows[0]
    assert row["request_id"] == _REQUEST_ID
    assert row["payment_rail"] == "x402"
    assert row["payment_method"] == "x402"
    assert row["payment_status"] == "settled"
    assert row["payment_reference"] == "x402-auth-ref"
    assert row["payment_amount_native"] == 150000.0
    assert row["payment_amount_usd"] == Decimal("0.15")


def test_x402_amount_extraction_reads_canonical_payload_authorization_value():
    result = extract_x402_payment_context(
        {"x-payment": _standard_x402_payment_header(amount="150000")}
    )

    assert result.valid is True
    assert result.payment_amount_native == Decimal("150000")


def test_log_api_request_economics_updates_existing_pending_row(monkeypatch):
    import metering.logger as logger_module

    calls: list[dict] = []

    class FakeConnection:
        def execute(self, _statement, params):
            calls.append(dict(params))
            return SimpleNamespace(rowcount=1)

    class FakeBegin:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc, traceback):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    monkeypatch.setattr(logger_module, "get_metering_engine", lambda: FakeEngine())

    logger_module.log_api_request_economics(_econ("settled"))

    assert len(calls) == 1
    assert calls[0]["request_id"] == _REQUEST_ID
    assert calls[0]["payment_status"] == "settled"


def test_log_api_request_economics_does_not_update_non_x402_rows(monkeypatch):
    import metering.logger as logger_module

    statements: list[object] = []

    class FakeConnection:
        def execute(self, statement, _params):
            statements.append(statement)
            return SimpleNamespace(rowcount=1)

    class FakeBegin:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc, traceback):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    econ = _econ("captured")
    econ["payment_rail"] = "mpp"
    econ["payment_method"] = "mpp"

    monkeypatch.setattr(logger_module, "get_metering_engine", lambda: FakeEngine())

    logger_module.log_api_request_economics(econ)

    assert statements == [logger_module.INSERT_REQUEST_ECONOMICS_SQL]
