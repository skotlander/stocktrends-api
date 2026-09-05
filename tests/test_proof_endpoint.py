"""
Tests for GET /v1/ai/proof/market-edge — free proof-of-value endpoint.

Coverage:
1.  200 without any API key (public/unauthenticated)
2.  200 with a bogus API key (no auth lookup — path bypasses ApiKeyMiddleware)
3.  Endpoint registered in ApiKeyMiddleware.public_paths source
4.  Endpoint registered in pricing.classifier.NON_METERED_PATHS
5.  Classifier returns free decision (is_metered=0, econ_payment_required=0)
6.  Cache-Control header present on response
7.  log_api_request_event IS called (request log preserved)
8.  log_api_request_economics is NOT called (no billing record)
9.  Required top-level keys present in response
10. market_snapshot explicitly labeled as synthetic / non-live
11. All symbols match the impossible SAMPLE_XX / SYNTH_ pattern (positive check)
12. /v1/openapi.json still loads (regression)
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

# conftest.py provides sqlalchemy / jwt / cryptography / etc. stubs.
import main
import middleware.api_key as api_key_module
import middleware.metering as metering_module
from middleware.metering import ResolvedPrice
from pricing.classifier import NON_METERED_PATHS, classify_request

# Symbols must match this pattern: clearly impossible ticker-like identifiers.
_SYNTHETIC_SYMBOL_RE = re.compile(r"^(SAMPLE_[A-Z]\d+|SYNTH_\w+)$")


def _stub_runtime(monkeypatch):
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *a, **kw: None)
    monkeypatch.setattr(metering_module, "log_api_request_economics", lambda *a, **kw: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_request_pricing",
        lambda *a, **kw: ResolvedPrice.priced(Decimal("0"), Decimal("0")),
    )
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda *a, **kw: None)


@pytest.fixture
def client(monkeypatch):
    _stub_runtime(monkeypatch)
    with TestClient(main.app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1–2. Public access
# ---------------------------------------------------------------------------

def test_proof_endpoint_returns_200_no_api_key(client):
    """GET without any auth header must return 200."""
    response = client.get("/v1/ai/proof/market-edge")
    assert response.status_code == 200


def test_proof_endpoint_returns_200_bogus_api_key(client):
    """Bogus key must NOT cause a DB lookup — path is public, returns 200."""
    response = client.get(
        "/v1/ai/proof/market-edge",
        headers={"X-API-Key": "bogus-key-should-never-hit-db"},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 3–4. Registration
# ---------------------------------------------------------------------------

def test_proof_endpoint_in_api_key_middleware_source():
    """Path must appear in middleware/api_key.py public_paths source."""
    import pathlib
    source = pathlib.Path("middleware/api_key.py").read_text(encoding="utf-8")
    assert '"/v1/ai/proof/market-edge"' in source


def test_proof_endpoint_in_non_metered_paths():
    """/v1/ai/proof/market-edge must be in NON_METERED_PATHS."""
    assert "/v1/ai/proof/market-edge" in NON_METERED_PATHS


# ---------------------------------------------------------------------------
# 5. Classifier free decision
# ---------------------------------------------------------------------------

def test_classify_request_free_decision_no_auth():
    """Classifier must return is_metered=0 and access_granted=True."""
    decision = classify_request(
        path="/v1/ai/proof/market-edge",
        has_paid_auth=False,
        payment_method_header=None,
        plan_code=None,
        agent_identifier=None,
    )
    assert decision.is_metered == 0
    assert decision.access_granted is True
    assert decision.econ_payment_required == 0
    assert decision.log_pricing_rule_id == "default_free"


def test_classify_request_free_even_with_paid_auth():
    """Providing a paid auth context must NOT make this endpoint metered."""
    decision = classify_request(
        path="/v1/ai/proof/market-edge",
        has_paid_auth=True,
        payment_method_header=None,
        plan_code="pro",
        agent_identifier=None,
    )
    assert decision.is_metered == 0
    assert decision.econ_payment_required == 0
    # econ_pricing_rule_id must be None so economics log is never written
    assert decision.econ_pricing_rule_id is None


# ---------------------------------------------------------------------------
# 6. Cache-Control header
# ---------------------------------------------------------------------------

def test_proof_endpoint_has_cache_control_header(client):
    """Response must set Cache-Control with max-age."""
    response = client.get("/v1/ai/proof/market-edge")
    assert response.status_code == 200
    cc = response.headers.get("cache-control", "")
    assert "public" in cc
    assert "max-age=" in cc


# ---------------------------------------------------------------------------
# 7–8. Logging behaviour
# ---------------------------------------------------------------------------

def test_proof_endpoint_request_log_is_called(monkeypatch):
    """log_api_request_event must be called (request log preserved)."""
    call_count = {"n": 0}

    def _spy(*a, **kw):
        call_count["n"] += 1

    monkeypatch.setattr(metering_module, "log_api_request_event", _spy)
    monkeypatch.setattr(metering_module, "log_api_request_economics", lambda *a, **kw: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_request_pricing",
        lambda *a, **kw: ResolvedPrice.priced(Decimal("0"), Decimal("0")),
    )
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda *a, **kw: None)

    with TestClient(main.app) as c:
        c.get("/v1/ai/proof/market-edge")

    assert call_count["n"] >= 1, "log_api_request_event was not called"


def test_proof_endpoint_economics_log_not_called(monkeypatch):
    """log_api_request_economics must NOT be called for non-metered path."""
    economics_calls = {"n": 0}

    def _fail(*a, **kw):
        economics_calls["n"] += 1

    monkeypatch.setattr(metering_module, "log_api_request_economics", _fail)
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_request_pricing",
        lambda *a, **kw: ResolvedPrice.priced(Decimal("0"), Decimal("0")),
    )
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda *a, **kw: None)

    with TestClient(main.app) as c:
        c.get("/v1/ai/proof/market-edge")

    assert economics_calls["n"] == 0, (
        f"log_api_request_economics was called {economics_calls['n']} time(s); "
        "expected 0 for non-metered endpoint"
    )


# ---------------------------------------------------------------------------
# 9. Response structure
# ---------------------------------------------------------------------------

_REQUIRED_TOP_LEVEL_KEYS = {
    "endpoint", "version", "generated_at", "cache_policy",
    "agent_guidance", "value_proposition", "market_snapshot",
    "signal_highlights", "sample_workflow", "conversion_prompt",
}


def test_proof_endpoint_required_top_level_keys(client):
    """All required top-level keys must be present."""
    data = client.get("/v1/ai/proof/market-edge").json()
    missing = _REQUIRED_TOP_LEVEL_KEYS - data.keys()
    assert not missing, f"Missing keys: {missing}"


def test_proof_endpoint_correct_endpoint_field(client):
    data = client.get("/v1/ai/proof/market-edge").json()
    assert data["endpoint"] == "/v1/ai/proof/market-edge"


def test_proof_endpoint_generated_at_present(client):
    """generated_at must be a non-empty string (ISO-8601 timestamp)."""
    data = client.get("/v1/ai/proof/market-edge").json()
    assert isinstance(data["generated_at"], str)
    assert len(data["generated_at"]) > 10


def test_proof_endpoint_conversion_prompt_payment_methods(client):
    """conversion_prompt must list all three supported rails."""
    data = client.get("/v1/ai/proof/market-edge").json()
    methods = set(data["conversion_prompt"]["payment_methods"])
    assert methods == {"subscription", "x402", "mpp"}


# ---------------------------------------------------------------------------
# 10. market_snapshot labeled synthetic
# ---------------------------------------------------------------------------

def test_proof_endpoint_market_snapshot_labeled_synthetic(client):
    """market_snapshot must declare itself as synthetic / non-live."""
    data = client.get("/v1/ai/proof/market-edge").json()
    note = data["market_snapshot"]["note"].upper()
    # Must include at least one of these markers
    assert any(word in note for word in ("SYNTHETIC", "NOT LIVE", "NOT REAL", "SAMPLE")), (
        "market_snapshot.note must state that data is synthetic/non-live"
    )


def test_proof_endpoint_market_snapshot_as_of_synthetic(client):
    """market_snapshot.as_of must not look like a real date."""
    data = client.get("/v1/ai/proof/market-edge").json()
    as_of = data["market_snapshot"].get("as_of", "")
    # Must not be a numeric date string (YYYY-MM-DD or similar)
    import re
    assert not re.match(r"^\d{4}-\d{2}-\d{2}", as_of), (
        f"market_snapshot.as_of looks like a real date: {as_of!r}"
    )


# ---------------------------------------------------------------------------
# 11. Symbols are clearly impossible synthetic identifiers
# ---------------------------------------------------------------------------

def test_proof_endpoint_symbols_match_synthetic_pattern(client):
    """Every symbol in market_snapshot.instruments must match the SAMPLE_XX pattern."""
    data = client.get("/v1/ai/proof/market-edge").json()
    instruments = data["market_snapshot"]["instruments"]
    assert instruments, "market_snapshot.instruments must not be empty"
    for inst in instruments:
        sym = inst.get("symbol", "")
        assert _SYNTHETIC_SYMBOL_RE.match(sym), (
            f"Symbol {sym!r} does not match synthetic pattern SAMPLE_<letter><digit> "
            f"or SYNTH_<word> — use clearly impossible identifiers"
        )


def test_proof_endpoint_instruments_use_real_screener_fields(client):
    """Instrument rows must use the actual screener response field names."""
    data = client.get("/v1/ai/proof/market-edge").json()
    instruments = data["market_snapshot"]["instruments"]
    real_fields = {"trend", "trend_cnt", "mt_cnt", "rsi", "rsi_updn", "vol_tag", "rank"}
    for inst in instruments:
        present = real_fields & inst.keys()
        assert present, (
            f"Instrument row has none of the expected screener fields {real_fields}; "
            f"got {set(inst.keys())}"
        )


# ---------------------------------------------------------------------------
# Semantic correctness of instrument field values
# ---------------------------------------------------------------------------

_VALID_TREND_CODES = {"^+", "^-", "v^", "v-", "v+", "^v"}
_VALID_RSI_UPDN = {"+", "-"}
_VALID_VOL_TAGS = {"**", "*", "!!", "!", ""}


def test_proof_endpoint_instrument_trend_values_valid(client):
    """All trend values must be valid Stock Trends trend codes."""
    data = client.get("/v1/ai/proof/market-edge").json()
    instruments = data["market_snapshot"]["instruments"]
    for inst in instruments:
        trend = inst.get("trend", "__MISSING__")
        assert trend in _VALID_TREND_CODES, (
            f"Instrument {inst['symbol']!r} has invalid trend {trend!r}. "
            f"Must be one of {sorted(_VALID_TREND_CODES)}"
        )


def test_proof_endpoint_instrument_rsi_updn_values_valid(client):
    """All rsi_updn values must be '+' or '-' (not 'up'/'down'/'flat')."""
    data = client.get("/v1/ai/proof/market-edge").json()
    instruments = data["market_snapshot"]["instruments"]
    for inst in instruments:
        rsi_updn = inst.get("rsi_updn", "__MISSING__")
        assert rsi_updn in _VALID_RSI_UPDN, (
            f"Instrument {inst['symbol']!r} has invalid rsi_updn {rsi_updn!r}. "
            f"Must be one of {sorted(_VALID_RSI_UPDN)}"
        )


def test_proof_endpoint_instrument_vol_tag_values_valid(client):
    """All vol_tag values must be in the valid Stock Trends set."""
    data = client.get("/v1/ai/proof/market-edge").json()
    instruments = data["market_snapshot"]["instruments"]
    for inst in instruments:
        vol_tag = inst.get("vol_tag", "__MISSING__")
        assert vol_tag in _VALID_VOL_TAGS, (
            f"Instrument {inst['symbol']!r} has invalid vol_tag {vol_tag!r}. "
            f"Must be one of {_VALID_VOL_TAGS!r}"
        )


def test_proof_endpoint_instrument_rsi_in_baseline_range(client):
    """RSI values must use the 100-baseline scale, not the traditional 0-100 Wilder RSI."""
    data = client.get("/v1/ai/proof/market-edge").json()
    instruments = data["market_snapshot"]["instruments"]
    for inst in instruments:
        rsi = inst.get("rsi")
        assert rsi is not None, f"Instrument {inst['symbol']!r} missing rsi"
        assert 50 <= rsi <= 250, (
            f"Instrument {inst['symbol']!r} has rsi={rsi}, outside expected "
            "100-baseline range (50-250). Stock Trends RSI is not the traditional Wilder RSI."
        )


def test_proof_endpoint_instrument_trend_cnt_mt_cnt_positive(client):
    """trend_cnt and mt_cnt must be positive integers."""
    data = client.get("/v1/ai/proof/market-edge").json()
    instruments = data["market_snapshot"]["instruments"]
    for inst in instruments:
        symbol = inst["symbol"]
        trend_cnt = inst.get("trend_cnt")
        mt_cnt = inst.get("mt_cnt")
        assert isinstance(trend_cnt, int) and trend_cnt > 0, (
            f"Instrument {symbol!r} has invalid trend_cnt={trend_cnt!r} (must be a positive integer)"
        )
        assert isinstance(mt_cnt, int) and mt_cnt > 0, (
            f"Instrument {symbol!r} has invalid mt_cnt={mt_cnt!r} (must be a positive integer)"
        )


# ---------------------------------------------------------------------------
# 12. Regression: /v1/openapi.json still loads
# ---------------------------------------------------------------------------

def test_openapi_json_still_loads(client):
    """/v1/openapi.json must still return 200 after these changes."""
    response = client.get("/v1/openapi.json")
    assert response.status_code == 200
    data = response.json()
    assert "paths" in data
