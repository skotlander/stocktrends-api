"""
Tests for build_x402_requirements() and build_x402_challenge() Bazaar/x402 V2 compliance.

PaymentRequired V2 structure (payments.ts canonical):
  {
    x402Version: 2,
    resource: {url, description, mimeType},   # top-level ResourceInfo
    accepts: [{scheme, network, amount, asset, payTo, maxTimeoutSeconds, extra}],
    extensions: {bazaar: {info, schema}}
  }

Validates:
  - decoded header has top-level resource.url (full URL when env var set)
  - decoded header has accepts[0].amount (not maxAmountRequired)
  - accepts[0] has no maxAmountRequired field
  - mimeType/description live on resource object, not inside accepts entries
  - extensions.bazaar is present with correct info/schema
  - GET/HEAD/DELETE: query-param style Bazaar info
  - POST/PUT/PATCH:  body style with bodyType/body declared
  - facilitator _extract_single_requirement returns accepts[0] which contains amount
  - response body resource aligns with header resource.url
  - fallback: bare path when X402_API_BASE_URL is unset
"""
from __future__ import annotations

import base64
import json
from decimal import Decimal

import pytest

import payments.x402 as x402_module
from payments.x402 import (
    build_x402_requirements,
    build_x402_challenge,
    _extract_single_requirement,
)


_PATH = "/v1/market/regime/latest"
_AMOUNT = Decimal("0.0005")
_BASE_URL = "https://api.stocktrends.com"
_FULL_URL = f"{_BASE_URL}{_PATH}"


@pytest.fixture()
def reqs(monkeypatch):
    monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
    return build_x402_requirements(path=_PATH, amount_usd=_AMOUNT, method="GET")


@pytest.fixture()
def reqs_no_base_url(monkeypatch):
    monkeypatch.setattr(x402_module, "X402_API_BASE_URL", "")
    return build_x402_requirements(path=_PATH, amount_usd=_AMOUNT, method="GET")


# ---------------------------------------------------------------------------
# Top-level PaymentRequired shape
# ---------------------------------------------------------------------------

class TestTopLevelShape:
    def test_x402Version(self, reqs):
        assert reqs["x402Version"] == 2

    def test_resource_is_dict(self, reqs):
        assert isinstance(reqs["resource"], dict)

    def test_resource_url_is_full_url(self, reqs):
        assert reqs["resource"]["url"] == _FULL_URL

    def test_resource_url_fallback_to_path(self, reqs_no_base_url):
        assert reqs_no_base_url["resource"]["url"] == _PATH

    def test_resource_mimeType(self, reqs):
        assert reqs["resource"]["mimeType"] == "application/json"

    def test_resource_description_present(self, reqs):
        assert "description" in reqs["resource"]

    def test_accepts_is_list_of_one(self, reqs):
        assert isinstance(reqs["accepts"], list) and len(reqs["accepts"]) == 1

    def test_extensions_present(self, reqs):
        assert "extensions" in reqs

    def test_extensions_bazaar_present(self, reqs):
        assert "bazaar" in reqs["extensions"]


# ---------------------------------------------------------------------------
# accepts[0] — V2 PaymentRequirements
# ---------------------------------------------------------------------------

class TestAcceptsEntry:
    def test_amount_key_present(self, reqs):
        assert "amount" in reqs["accepts"][0]

    def test_maxAmountRequired_absent(self, reqs):
        assert "maxAmountRequired" not in reqs["accepts"][0]

    def test_amount_value(self, reqs):
        # 0.0005 USD * 10^6 = 500 atomic USDC units
        assert reqs["accepts"][0]["amount"] == "500"

    def test_method_absent_from_accepts(self, reqs):
        # method belongs in extensions.bazaar.info.input, not in accepts
        assert "method" not in reqs["accepts"][0]

    def test_resource_absent_from_accepts(self, reqs):
        # V2: resource identity is top-level, not inside accepts entries
        assert "resource" not in reqs["accepts"][0]

    def test_mimeType_absent_from_accepts(self, reqs):
        # mimeType belongs on top-level resource object
        assert "mimeType" not in reqs["accepts"][0]

    def test_description_absent_from_accepts(self, reqs):
        # description belongs on top-level resource object
        assert "description" not in reqs["accepts"][0]

    def test_required_fields_present(self, reqs):
        entry = reqs["accepts"][0]
        for field in ("scheme", "network", "amount", "asset", "payTo",
                      "maxTimeoutSeconds", "extra"):
            assert field in entry, f"missing field: {field}"


# ---------------------------------------------------------------------------
# extensions.bazaar — GET/HEAD/DELETE (query-param style)
# ---------------------------------------------------------------------------

class TestExtensionsBazaarGet:
    def test_info_input_type_is_http(self, reqs):
        assert reqs["extensions"]["bazaar"]["info"]["input"]["type"] == "http"

    def test_info_input_method_get(self, reqs):
        assert reqs["extensions"]["bazaar"]["info"]["input"]["method"] == "GET"

    def test_info_input_no_bodyType_for_get(self, reqs):
        assert "bodyType" not in reqs["extensions"]["bazaar"]["info"]["input"]

    def test_schema_present(self, reqs):
        assert "schema" in reqs["extensions"]["bazaar"]

    def test_schema_json_schema_keyword(self, reqs):
        assert reqs["extensions"]["bazaar"]["schema"]["$schema"] == \
            "https://json-schema.org/draft/2020-12/schema"

    def test_schema_input_required(self, reqs):
        assert "input" in reqs["extensions"]["bazaar"]["schema"]["required"]

    def test_schema_required_fields_get(self, reqs):
        required = reqs["extensions"]["bazaar"]["schema"]["properties"]["input"]["required"]
        assert "type" in required
        assert "method" in required
        assert "bodyType" not in required

    def test_schema_method_enum_matches(self, reqs):
        enum = reqs["extensions"]["bazaar"]["schema"]["properties"]["input"]["properties"]["method"]["enum"]
        assert "GET" in enum

    def test_head_uses_query_style(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        r = build_x402_requirements(path=_PATH, amount_usd=_AMOUNT, method="HEAD")
        assert "bodyType" not in r["extensions"]["bazaar"]["info"]["input"]
        assert r["extensions"]["bazaar"]["info"]["input"]["method"] == "HEAD"

    def test_delete_uses_query_style(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        r = build_x402_requirements(path=_PATH, amount_usd=_AMOUNT, method="DELETE")
        assert "bodyType" not in r["extensions"]["bazaar"]["info"]["input"]


# ---------------------------------------------------------------------------
# extensions.bazaar — POST/PUT/PATCH (body style)
# ---------------------------------------------------------------------------

class TestExtensionsBazaarPost:
    @pytest.fixture()
    def post_reqs(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        return build_x402_requirements(path=_PATH, amount_usd=_AMOUNT, method="POST")

    def test_info_input_has_bodyType(self, post_reqs):
        assert post_reqs["extensions"]["bazaar"]["info"]["input"]["bodyType"] == "json"

    def test_info_input_has_body(self, post_reqs):
        assert "body" in post_reqs["extensions"]["bazaar"]["info"]["input"]

    def test_schema_required_includes_bodyType(self, post_reqs):
        required = post_reqs["extensions"]["bazaar"]["schema"]["properties"]["input"]["required"]
        assert "bodyType" in required
        assert "body" in required

    def test_put_uses_body_style(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        r = build_x402_requirements(path=_PATH, amount_usd=_AMOUNT, method="PUT")
        assert r["extensions"]["bazaar"]["info"]["input"]["bodyType"] == "json"

    def test_patch_uses_body_style(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        r = build_x402_requirements(path=_PATH, amount_usd=_AMOUNT, method="PATCH")
        assert r["extensions"]["bazaar"]["info"]["input"]["bodyType"] == "json"


# ---------------------------------------------------------------------------
# Facilitator path: _extract_single_requirement returns accepts[0] with amount
# ---------------------------------------------------------------------------

class TestFacilitatorRequirementExtraction:
    def test_returns_accepts_entry(self, reqs):
        extracted = _extract_single_requirement(reqs)
        assert extracted["scheme"] == "exact"

    def test_extracted_has_amount(self, reqs):
        extracted = _extract_single_requirement(reqs)
        assert "amount" in extracted

    def test_extracted_amount_value(self, reqs):
        extracted = _extract_single_requirement(reqs)
        assert extracted["amount"] == "500"

    def test_extracted_has_no_maxAmountRequired(self, reqs):
        extracted = _extract_single_requirement(reqs)
        assert "maxAmountRequired" not in extracted


# ---------------------------------------------------------------------------
# Round-trip: PAYMENT-REQUIRED header decodes to correct V2 shape
# ---------------------------------------------------------------------------

class TestPaymentRequiredHeader:
    def _decode(self, header_b64: str) -> dict:
        return json.loads(base64.b64decode(header_b64).decode("utf-8"))

    def test_header_has_top_level_resource_url(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        _, hdr = build_x402_challenge(path=_PATH, amount_usd=_AMOUNT, method="GET")
        decoded = self._decode(hdr)
        assert decoded["resource"]["url"] == _FULL_URL

    def test_header_has_amount_not_maxAmountRequired(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        _, hdr = build_x402_challenge(path=_PATH, amount_usd=_AMOUNT, method="GET")
        decoded = self._decode(hdr)
        assert "amount" in decoded["accepts"][0]
        assert "maxAmountRequired" not in decoded["accepts"][0]

    def test_header_has_extensions_bazaar(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        _, hdr = build_x402_challenge(path=_PATH, amount_usd=_AMOUNT, method="GET")
        decoded = self._decode(hdr)
        assert decoded["extensions"]["bazaar"]["info"]["input"]["type"] == "http"

    def test_header_resource_fallback_no_base_url(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", "")
        _, hdr = build_x402_challenge(path=_PATH, amount_usd=_AMOUNT, method="GET")
        decoded = self._decode(hdr)
        assert decoded["resource"]["url"] == _PATH

    def test_challenge_body_resource_matches_header_resource_url(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        body, hdr = build_x402_challenge(path=_PATH, amount_usd=_AMOUNT, method="GET")
        decoded = self._decode(hdr)
        # The JSON body's resource field must equal the header's resource.url
        assert body["resource"] == decoded["resource"]["url"]
        assert body["resource"] == _FULL_URL


# ---------------------------------------------------------------------------
# extensions.bazaar.info.output — required by Agentic Market Bazaar check
# ---------------------------------------------------------------------------

class TestExtensionsBazaarOutput:
    def test_info_has_output(self, reqs):
        assert "output" in reqs["extensions"]["bazaar"]["info"]

    def test_output_type_is_json(self, reqs):
        assert reqs["extensions"]["bazaar"]["info"]["output"]["type"] == "json"

    def test_output_example_present(self, reqs):
        assert "example" in reqs["extensions"]["bazaar"]["info"]["output"]

    def test_output_example_is_dict(self, reqs):
        assert isinstance(reqs["extensions"]["bazaar"]["info"]["output"]["example"], dict)

    def test_output_description_present(self, reqs):
        assert "description" in reqs["extensions"]["bazaar"]["info"]["output"]

    def test_schema_has_output_property(self, reqs):
        schema_props = reqs["extensions"]["bazaar"]["schema"]["properties"]
        assert "output" in schema_props

    def test_schema_output_type_is_object(self, reqs):
        output_schema = reqs["extensions"]["bazaar"]["schema"]["properties"]["output"]
        assert output_schema["type"] == "object"

    def test_schema_required_includes_input_and_output(self, reqs):
        required = reqs["extensions"]["bazaar"]["schema"]["required"]
        assert "input" in required
        assert "output" in required

    def test_post_info_also_has_output(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        r = build_x402_requirements(path=_PATH, amount_usd=_AMOUNT, method="POST")
        assert r["extensions"]["bazaar"]["info"]["output"]["type"] == "json"

    def test_decoded_header_has_output(self, monkeypatch):
        monkeypatch.setattr(x402_module, "X402_API_BASE_URL", _BASE_URL)
        _, hdr = build_x402_challenge(path=_PATH, amount_usd=_AMOUNT, method="GET")
        decoded = json.loads(base64.b64decode(hdr).decode("utf-8"))
        assert decoded["extensions"]["bazaar"]["info"]["output"]["type"] == "json"
        assert "example" in decoded["extensions"]["bazaar"]["info"]["output"]
