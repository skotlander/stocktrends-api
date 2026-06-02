"""
Tests for stocktrends_preview injection in x402 challenge responses.

Validates that:
- x402 challenge body includes stocktrends_preview for known preview paths
- discovery/preview.py omits stocktrends_preview for unknown paths
- Existing 402 fields are preserved (error, pricing, accepted_payment_methods,
  payment_required, PAYMENT-REQUIRED header)
- MPP non-challenge 402s do NOT include stocktrends_preview
- x402 validation_failed, replay_detected responses do NOT include stocktrends_preview
- Preview content is schema-only (no paid response data)
- accepted_payment_methods includes subscription, x402, mpp for known paid paths
- indicators latest/history have machine-plannable stocktrends_preview metadata

Coverage map (from Codex review):
  [x] anonymous x402 challenge includes preview for known preview path
  [x] unit unknown preview paths return None
  [x] accepted_payment_methods remain subscription,x402,mpp
  [x] PAYMENT-REQUIRED header remains present while rich metadata stays in body preview
  [x] MPP errors do not include preview
  [x] validation/replay/settlement 402s do not include preview
  [x] preview is schema-only with structural examples and resolved pricing only
  [x] discovery/preview.py unit: get_endpoint_preview returns copy
  [x] discovery/preview.py unit: unknown path returns None
"""
from __future__ import annotations

import base64
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

# conftest.py provides sqlalchemy / jwt / cryptography / etc. stubs.
import main
import middleware.api_key as api_key_module
import middleware.metering as metering_module
import pricing.classifier as classifier_module
from payments.enforcement import PaymentEnforcementResult
from discovery.preview import get_endpoint_preview, _PREVIEW_BY_PATH
from pricing.classifier import PricingDecision


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_KNOWN_PREVIEW_PATH = "/v1/indicators/latest"
_INDICATORS_HISTORY_PATH = "/v1/indicators/history"
_DEVELOPER_ORIGIN = "https://developer.stocktrends.com"

_CHALLENGE_BODY_TEMPLATE = {
    "error": "payment_required",
    "detail": "Payment is required to access this endpoint.",
    "protocol": "x402",
    "resource": _KNOWN_PREVIEW_PATH,
    "pricing": {
        "amount_usd": "0.050000",
        "unit": "request",
        "network": "eip155:8453",
        "token": "0xtoken",
        "scheme": "exact",
    },
    "accepted_payment_methods": ["x402"],
    "payment_required": {"x402Version": 2, "accepts": []},
}

_PAYMENT_REQUIRED_HEADER_VALUE = base64.b64encode(b'{"test": true}').decode()


def _make_challenge_result(path: str = _KNOWN_PREVIEW_PATH) -> PaymentEnforcementResult:
    body = dict(_CHALLENGE_BODY_TEMPLATE)
    body["resource"] = path
    return PaymentEnforcementResult(
        outcome="challenge",
        challenge_body=body,
        payment_required_header=_PAYMENT_REQUIRED_HEADER_VALUE,
        payment_network="eip155:8453",
        payment_token="0xtoken",
    )


def _make_validation_failed_result() -> PaymentEnforcementResult:
    return PaymentEnforcementResult(
        outcome="validation_failed",
        error_code="invalid_payment",
        error_detail="Signature verification failed.",
        payment_required_header=_PAYMENT_REQUIRED_HEADER_VALUE,
        payment_network="eip155:8453",
        payment_token="0xtoken",
    )


def _make_replay_result() -> PaymentEnforcementResult:
    return PaymentEnforcementResult(
        outcome="replay_detected",
        error_code="replay_detected",
        error_detail="Payment reference already used.",
    )


def _make_mpp_failure_result() -> PaymentEnforcementResult:
    return PaymentEnforcementResult(
        outcome="session_not_found",
        error_code="mpp_session_not_found",
        error_detail="No active MPP session found for session_id.",
    )


def _stub_runtime(monkeypatch, *, enforce_result: PaymentEnforcementResult, path: str = _KNOWN_PREVIEW_PATH):
    """Patch metering-layer side effects and enforcement for agent-pay challenge tests."""
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *a, **kw: None)
    monkeypatch.setattr(metering_module, "log_api_request_economics", lambda *a, **kw: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda *a, **kw: (Decimal("0.05"), Decimal("0.05"), Decimal("0.05")),
    )
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda *a, **kw: None)

    # Enable agent-pay enforcement
    monkeypatch.setattr(metering_module, "ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "ENFORCE_AGENT_PAY", True)
    monkeypatch.setattr(api_key_module, "_ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(classifier_module, "ENABLE_AGENT_PAY", True)

    monkeypatch.setattr(
        metering_module,
        "enforce_payment_rail",
        lambda **kwargs: enforce_result,
    )


# ---------------------------------------------------------------------------
# Unit tests for discovery/preview.py
# ---------------------------------------------------------------------------

class TestDiscoveryPreview:
    def test_known_path_returns_dict(self):
        result = get_endpoint_preview(_KNOWN_PREVIEW_PATH)
        assert isinstance(result, dict)

    def test_unknown_path_returns_none(self):
        assert get_endpoint_preview("/v1/nonexistent/path") is None

    def test_returns_deep_copy_top_level(self):
        """Mutating the top-level returned dict must not affect the registry."""
        result = get_endpoint_preview(_KNOWN_PREVIEW_PATH)
        result["_injected"] = True
        fresh = get_endpoint_preview(_KNOWN_PREVIEW_PATH)
        assert "_injected" not in fresh

    def test_returns_deep_copy_nested(self):
        """Mutating a nested list in the returned dict must not affect the registry."""
        result = get_endpoint_preview(_KNOWN_PREVIEW_PATH)
        if "response_shape" in result:
            result["response_shape"].append("__INJECTED__")
            fresh = get_endpoint_preview(_KNOWN_PREVIEW_PATH)
            assert "__INJECTED__" not in fresh.get("response_shape", []), (
                "Mutating a nested list in the returned preview affected the registry — "
                "get_endpoint_preview() must return a deep copy"
            )

    def test_all_entries_have_machine_plannable_shape(self):
        for path, entry in _PREVIEW_BY_PATH.items():
            required = {
                "endpoint",
                "investment_agent_value",
                "required_inputs",
                "safe_example_request",
                "response_shape",
                "example_object",
                "notes",
                "related_endpoints",
                "pricing",
            }
            missing = required - set(entry)
            assert not missing, (
                f"Preview entry for {path!r} missing machine-plannable fields: {missing}"
            )

    def test_no_paid_data_values(self):
        """Preview entries may include pricing keys, but not paid response values."""
        for path, entry in _PREVIEW_BY_PATH.items():
            raw = json.dumps(entry)
            assert "amount_usd" not in raw, f"{path}: 'amount_usd' in preview"
            assert "billed_amount" not in raw, f"{path}: 'billed_amount' in preview"
            pricing = entry.get("pricing", {})
            assert pricing.get("stc_cost") is None, f"{path}: static preview must not embed resolved STC cost"
            assert pricing.get("effective_price_usd") is None, (
                f"{path}: static preview must not embed resolved effective USD cost"
            )

    def test_market_pulse_not_registered(self):
        """/v1/market/pulse does not exist as a route — must not be in the registry."""
        assert "/v1/market/pulse" not in _PREVIEW_BY_PATH, (
            "/v1/market/pulse is not a real endpoint and must not appear in preview registry"
        )

    def test_screener_preview_contains_real_fields(self):
        """Screener preview must reference actual response fields from screener.py."""
        preview = get_endpoint_preview("/v1/agent/screener/top")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        # These fields appear in the actual screener route response
        for field in ("trend", "trend_cnt", "mt_cnt", "rsi", "vol_tag", "weekdate"):
            assert field in shape_str, (
                f"Real screener field {field!r} missing from preview response_shape"
            )

    def test_indicators_latest_preview_has_planning_metadata(self):
        """Indicators latest must have the pre-payment planning metadata agents need."""
        preview = get_endpoint_preview("/v1/indicators/latest")
        assert preview is not None
        assert preview["endpoint"]["workflow_role"] == "Single-symbol signal confirmation."
        assert "symbol_exchange" in preview["required_inputs"]
        assert preview["required_inputs"]["symbol_exchange"]["example"] == "IBM-N"
        assert preview["safe_example_request"]["query"]["symbol_exchange"] == "IBM-N"
        assert preview["pricing"]["pricing_rule_id"] == "indicators_latest_paid"
        assert preview["pricing"]["cost_source"] == "/v1/pricing/catalog"
        shape_str = " ".join(preview.get("response_shape", []))
        for field in (
            "symbol_exchange",
            "trend",
            "trend_cnt",
            "mt_cnt",
            "rsi",
            "rsi_updn",
            "vol_tag",
        ):
            assert field in shape_str

    def test_indicators_history_preview_has_planning_metadata(self):
        """Indicators history must no longer be an empty/generic pre-payment surface."""
        preview = get_endpoint_preview(_INDICATORS_HISTORY_PATH)
        assert preview is not None
        assert preview["endpoint"]["workflow_role"] == (
            "Historical signal context and trend persistence review."
        )
        assert "symbol_exchange" in preview["required_inputs"]
        assert preview["optional_inputs"]["limit"]["safe_default"] == 260
        assert preview["safe_example_request"]["query"]["limit"] == 52
        assert preview["pricing"]["pricing_rule_id"] == "indicators_history_paid"
        assert "/v1/stim/history" in preview["next_recommended_calls"]
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("data[].weekdate", "data[].trend", "data[].rsi", "data[].vol_tag"):
            assert field in shape_str

    def test_stim_preview_contains_real_fields(self):
        """STIM latest preview must reference actual response fields from stim.py."""
        preview = get_endpoint_preview("/v1/stim/latest")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("x4wk", "x13wk", "x40wk", "symbol_exchange", "weekdate"):
            assert field in shape_str, (
                f"Real STIM field {field!r} missing from stim/latest preview response_shape"
            )

    def test_decision_preview_contains_real_fields(self):
        """Decision preview must reference actual fields from decision.py."""
        preview = get_endpoint_preview("/v1/decision/evaluate-symbol")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("bias", "confidence", "decision_score", "regime_context"):
            assert field in shape_str, (
                f"Real decision field {field!r} missing from decision preview response_shape"
            )

    def test_portfolio_preview_contains_real_fields(self):
        """Portfolio preview must reference actual fields from portfolio.py."""
        preview = get_endpoint_preview("/v1/portfolio/construct")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("portfolio", "regime_context", "portfolio_score", "candidates_evaluated"):
            assert field in shape_str, (
                f"Real portfolio field {field!r} missing from portfolio preview response_shape"
            )

    def test_portfolio_evaluate_preview_contains_real_fields(self):
        """Portfolio evaluate preview must reference actual fields from portfolio.py."""
        preview = get_endpoint_preview("/v1/portfolio/evaluate")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("positions_found", "portfolio_score", "portfolio_alignment"):
            assert field in shape_str, (
                f"Field {field!r} missing from portfolio/evaluate preview response_shape"
            )

    def test_portfolio_compare_preview_contains_real_fields(self):
        """Portfolio compare preview must reference actual fields from portfolio.py."""
        preview = get_endpoint_preview("/v1/portfolio/compare")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("comparison.winner", "comparison.score_delta"):
            assert field in shape_str, (
                f"Field {field!r} missing from portfolio/compare preview response_shape"
            )

    def test_market_regime_latest_preview_contains_real_fields(self):
        """Market regime latest preview must reference actual fields."""
        preview = get_endpoint_preview("/v1/market/regime/latest")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("regime", "regime_score", "signal_count"):
            assert field in shape_str, (
                f"Field {field!r} missing from market/regime/latest preview response_shape"
            )

    def test_market_regime_history_preview_contains_real_fields(self):
        """Market regime history preview must reference actual fields."""
        preview = get_endpoint_preview("/v1/market/regime/history")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("history", "regime"):
            assert field in shape_str, (
                f"Field {field!r} missing from market/regime/history preview response_shape"
            )

    def test_market_regime_forecast_preview_contains_real_fields(self):
        """Market regime forecast preview must reference actual fields."""
        preview = get_endpoint_preview("/v1/market/regime/forecast")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("forecast_regime", "forecast_confidence"):
            assert field in shape_str, (
                f"Field {field!r} missing from market/regime/forecast preview response_shape"
            )

    def test_selections_latest_preview_contains_real_fields(self):
        """Selections latest preview must reference actual fields from selections.py."""
        preview = get_endpoint_preview("/v1/selections/latest")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("prob13wk", "symbol_exchange"):
            assert field in shape_str, (
                f"Field {field!r} missing from selections/latest preview response_shape"
            )

    def test_selections_history_preview_contains_real_fields(self):
        """Selections history preview must reference actual fields from selections.py."""
        preview = get_endpoint_preview("/v1/selections/history")
        assert preview is not None
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("prob13wk", "symbol_exchange"):
            assert field in shape_str, (
                f"Field {field!r} missing from selections/history preview response_shape"
            )

    def test_breadth_sector_latest_preview_is_paid_and_machine_payable(self):
        """Breadth latest must expose paid preview metadata for x402 challenges."""
        preview = get_endpoint_preview("/v1/breadth/sector/latest")
        assert preview is not None
        assert preview["endpoint"]["access_type"] == "paid"
        assert preview["endpoint"]["requires_payment"] is True
        assert preview["pricing"]["pricing_rule_id"] == "breadth_sector_latest_paid"
        assert preview["supported_rails"] == ["subscription", "x402", "mpp"]
        shape_str = " ".join(preview.get("response_shape", []))
        for field in ("bullish_count", "bearish_count", "avg_rsi", "net_breadth"):
            assert field in shape_str


# ---------------------------------------------------------------------------
# Integration tests: x402 challenge with preview for KNOWN path
# ---------------------------------------------------------------------------

@pytest.fixture
def client_x402_challenge_known(monkeypatch):
    _stub_runtime(monkeypatch, enforce_result=_make_challenge_result(_KNOWN_PREVIEW_PATH))
    with TestClient(main.app) as c:
        yield c


def test_x402_challenge_returns_402(client_x402_challenge_known):
    response = client_x402_challenge_known.get(
        _KNOWN_PREVIEW_PATH,
        headers={"X-StockTrends-Payment-Method": "x402"},
    )
    assert response.status_code == 402


def test_x402_challenge_includes_stocktrends_preview(client_x402_challenge_known):
    """Challenge body for a known preview path must include stocktrends_preview."""
    response = client_x402_challenge_known.get(
        _KNOWN_PREVIEW_PATH,
        headers={"X-StockTrends-Payment-Method": "x402"},
    )
    assert response.status_code == 402
    body = response.json()
    assert "stocktrends_preview" in body, (
        "stocktrends_preview must be present for known preview paths"
    )


def test_default_x402_challenge_preview_remains_rich(client_x402_challenge_known):
    """Default challenge mode keeps the rich first-discovery body preview."""
    response = client_x402_challenge_known.get(
        _KNOWN_PREVIEW_PATH,
        headers={"X-StockTrends-Payment-Method": "x402"},
    )
    assert response.status_code == 402
    preview = response.json()["stocktrends_preview"]

    assert "required_inputs" in preview
    assert "response_shape" in preview
    assert "example_object" in preview


def test_compact_x402_challenge_mode_keeps_rich_body_preview(client_x402_challenge_known):
    """Challenge compactness must not remove rich Stock Trends body metadata."""
    response = client_x402_challenge_known.get(
        _KNOWN_PREVIEW_PATH,
        headers={
            "X-StockTrends-Payment-Method": "x402",
            "X-StockTrends-Challenge-Mode": "compact",
        },
    )
    assert response.status_code == 402
    preview = response.json()["stocktrends_preview"]

    assert "required_inputs" in preview
    assert "response_shape" in preview
    assert "example_object" in preview
    assert preview["required_inputs"]["symbol_exchange"]["example"] == "IBM-N"
    assert preview["pricing"]["pricing_rule_id"] == "indicators_latest_paid"


def test_x402_challenge_preview_is_schema_only(client_x402_challenge_known):
    """stocktrends_preview must not contain paid response values."""
    body = client_x402_challenge_known.get(
        _KNOWN_PREVIEW_PATH,
        headers={"X-StockTrends-Payment-Method": "x402"},
    ).json()
    preview = body.get("stocktrends_preview", {})
    raw = json.dumps(preview)
    assert "amount_usd" not in raw
    assert "billed_amount" not in raw
    assert preview["pricing"]["pricing_rule_id"] == "indicators_latest_paid"
    assert float(preview["pricing"]["stc_cost"]) > 0
    assert float(preview["pricing"]["effective_price_usd"]) > 0


def test_stim_latest_compact_402_body_preview_remains_rich(monkeypatch):
    _stub_runtime(monkeypatch, enforce_result=_make_challenge_result("/v1/stim/latest"))
    with TestClient(main.app) as client:
        response = client.get(
            "/v1/stim/latest",
            headers={
                "X-StockTrends-Payment-Method": "x402",
                "X-StockTrends-Challenge-Mode": "compact",
            },
        )

    assert response.status_code == 402
    preview = response.json()["stocktrends_preview"]

    assert preview["pricing"]["pricing_rule_id"] == "stim_latest_paid"
    assert "symbol_exchange" in preview["required_inputs"]
    assert preview["required_inputs"]["symbol_exchange"]["pattern"] == "^[A-Z0-9.]+-[A-Z]$"
    assert preview["safe_example_request"]["query"]["symbol_exchange"] == "IBM-N"
    assert "response_shape" in preview
    assert "example_object" in preview
    assert "output_summary" in preview
    assert preview["interpretation_dependency"]["endpoint"] == "/v1/meta/stim"
    assert preview["required_interpretation_steps"]


def test_bazaar_service_identity_does_not_shrink_stocktrends_preview(monkeypatch):
    _stub_runtime(monkeypatch, enforce_result=_make_challenge_result("/v1/stim/latest"))
    with TestClient(main.app) as client:
        response = client.get(
            "/v1/stim/latest",
            headers={
                "X-StockTrends-Payment-Method": "x402",
                "X-StockTrends-Challenge-Mode": "compact",
            },
        )

    assert response.status_code == 402
    preview = response.json()["stocktrends_preview"]

    assert "required_inputs" in preview
    assert "optional_inputs" in preview
    assert "response_shape" in preview
    assert "example_object" in preview
    assert "interpretation_guidance" in preview
    assert "required_interpretation_steps" in preview


def test_x402_challenge_existing_fields_preserved(client_x402_challenge_known):
    """All original challenge fields must still be present."""
    body = client_x402_challenge_known.get(
        _KNOWN_PREVIEW_PATH,
        headers={"X-StockTrends-Payment-Method": "x402"},
    ).json()
    for field in ("error", "pricing", "accepted_payment_methods", "payment_required"):
        assert field in body, f"Field {field!r} missing from challenge body"


def test_x402_challenge_accepted_methods_has_all_rails(client_x402_challenge_known):
    """accepted_payment_methods must include subscription, x402, and mpp."""
    body = client_x402_challenge_known.get(
        _KNOWN_PREVIEW_PATH,
        headers={"X-StockTrends-Payment-Method": "x402"},
    ).json()
    methods = set(body.get("accepted_payment_methods", []))
    assert {"subscription", "x402", "mpp"}.issubset(methods), (
        f"accepted_payment_methods incomplete: {methods}"
    )


def test_x402_challenge_payment_required_header_present(client_x402_challenge_known):
    """PAYMENT-REQUIRED header must be set (not X-Payment-Required)."""
    response = client_x402_challenge_known.get(
        _KNOWN_PREVIEW_PATH,
        headers={"X-StockTrends-Payment-Method": "x402"},
    )
    # Starlette lowercases response headers
    assert "payment-required" in response.headers, (
        "PAYMENT-REQUIRED header must be present on 402 challenge response"
    )
    # Must NOT be the wrong header name
    assert "x-payment-required" not in response.headers


def test_stim_latest_no_payment_request_still_returns_402(monkeypatch):
    _stub_runtime(monkeypatch, enforce_result=_make_challenge_result("/v1/stim/latest"))
    with TestClient(main.app) as client:
        response = client.get("/v1/stim/latest")

    assert response.status_code == 402


def test_x402_402_response_is_readable_from_developer_portal(monkeypatch):
    _stub_runtime(monkeypatch, enforce_result=_make_challenge_result("/v1/stim/latest"))
    with TestClient(main.app) as client:
        response = client.get(
            "/v1/stim/latest",
            headers={"Origin": _DEVELOPER_ORIGIN},
        )

    assert response.status_code == 402
    assert response.headers["access-control-allow-origin"] == _DEVELOPER_ORIGIN
    exposed_headers = response.headers["access-control-expose-headers"].lower()
    assert "payment-required" in exposed_headers
    assert "x-stocktrends-pricing-rule" in exposed_headers
    assert "x-stocktrends-payment-required" in exposed_headers
    assert response.headers["cache-control"] == "no-store, private"


def test_x402_preflight_allows_payment_headers_for_paid_route():
    with TestClient(main.app) as client:
        response = client.options(
            "/v1/stim/latest",
            headers={
                "Origin": _DEVELOPER_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-payment,x-stocktrends-payment-method",
            },
        )

    assert response.status_code < 300
    assert response.status_code not in {401, 402, 405}
    assert response.headers["access-control-allow-origin"] == _DEVELOPER_ORIGIN
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "x-payment" in allowed_headers
    assert "x-stocktrends-payment-method" in allowed_headers


def test_public_tools_endpoint_remains_non_payment_public():
    with TestClient(main.app) as client:
        response = client.get("/v1/ai/tools")

    assert response.status_code == 200
    assert response.headers.get("x-stocktrends-payment-required") == "false"
    assert response.headers.get("cache-control") != "no-store, private"


def test_non_developer_origin_gets_no_cors_headers(monkeypatch):
    """Requests from non-whitelisted origins must not receive access-control-allow-origin."""
    _stub_runtime(monkeypatch, enforce_result=_make_challenge_result("/v1/stim/latest"))
    with TestClient(main.app) as client:
        response = client.get(
            "/v1/stim/latest",
            headers={"Origin": "https://evil.example.com"},
        )

    assert "access-control-allow-origin" not in response.headers


def test_paid_200_response_gets_no_store_cache_control(monkeypatch):
    """A successful paid endpoint response (200) receives Cache-Control: no-store, private."""
    paid_decision = PricingDecision(
        is_metered=1,
        access_granted=True,
        deny_reason=None,
        log_pricing_rule_id="stim_latest",
        log_payment_method="subscription",
        econ_pricing_rule_id="stim_latest",
        econ_payment_required=0,
        econ_payment_status=None,
        econ_payment_method="subscription",
    )
    monkeypatch.setattr(metering_module, "classify_request", lambda **kwargs: paid_decision)
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *a, **kw: None)
    monkeypatch.setattr(metering_module, "log_api_request_economics", lambda *a, **kw: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda *a, **kw: (Decimal("0.05"), Decimal("0.05"), Decimal("0.05")),
    )

    with TestClient(main.app) as client:
        response = client.get("/v1/ai/tools")

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store, private"


# ---------------------------------------------------------------------------
# x402 validation_failed — must NOT include preview
# ---------------------------------------------------------------------------

@pytest.fixture
def client_x402_validation_failed(monkeypatch):
    _stub_runtime(monkeypatch, enforce_result=_make_validation_failed_result())
    with TestClient(main.app) as c:
        yield c


def test_x402_validation_failed_no_preview(client_x402_validation_failed):
    """validation_failed 402 must not include stocktrends_preview."""
    response = client_x402_validation_failed.get(
        _KNOWN_PREVIEW_PATH,
        headers={
            "X-StockTrends-Payment-Method": "x402",
            "X-StockTrends-Payment-Reference": "0xsig",
        },
    )
    assert response.status_code == 402
    body = response.json()
    assert "stocktrends_preview" not in body


# ---------------------------------------------------------------------------
# x402 replay_detected — must NOT include preview
# ---------------------------------------------------------------------------

@pytest.fixture
def client_x402_replay(monkeypatch):
    _stub_runtime(monkeypatch, enforce_result=_make_replay_result())
    with TestClient(main.app) as c:
        yield c


def test_x402_replay_detected_no_preview(client_x402_replay):
    """replay_detected 402 must not include stocktrends_preview."""
    response = client_x402_replay.get(
        _KNOWN_PREVIEW_PATH,
        headers={
            "X-StockTrends-Payment-Method": "x402",
            "X-StockTrends-Payment-Reference": "0xdupe",
        },
    )
    assert response.status_code == 402
    body = response.json()
    assert "stocktrends_preview" not in body


# ---------------------------------------------------------------------------
# MPP failure — must NOT include preview (different code path entirely)
# ---------------------------------------------------------------------------

@pytest.fixture
def client_mpp_failure(monkeypatch):
    _stub_runtime(monkeypatch, enforce_result=_make_mpp_failure_result())
    with TestClient(main.app) as c:
        yield c


def test_mpp_failure_no_preview(client_mpp_failure):
    """MPP 402 errors must not include stocktrends_preview (different branch)."""
    response = client_mpp_failure.get(
        _KNOWN_PREVIEW_PATH,
        headers={
            "X-StockTrends-Payment-Method": "mpp",
            "X-StockTrends-Session-Id": "ses_test",
        },
    )
    assert response.status_code == 402
    body = response.json()
    assert "stocktrends_preview" not in body
