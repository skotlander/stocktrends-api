"""
Chief Architect remediation of PR3, after inspection of the Codex-remediation
commit (a9c7e04).

Two findings, both of which reopened a door PR3 exists to close.

1. `Authorization: x402 …` was classified as x402 payment proof by the
   early-challenge guard, while `enforce_x402_payment` gates on
   `has_payment_signature` / `extract_payment_signature` — both of which read
   only the published `X402_PROOF_HEADERS`.  PR3 therefore carried two
   definitions of "has this caller paid".  A bare canonical probe carrying that
   header alone was treated as payment-bearing, skipped the challenge, and
   received `400 missing_required_param` — the exact discovery failure PR3 was
   written to remove — while enforcement would have called the very same
   request unpaid.

2. Public lifecycle guidance still contained old-architecture language: the
   `/v1/ai/context` usage guidance described the 402 preview as the final
   surface "for an otherwise-serviceable request", and `/v1/ai/tools` claimed
   an unpaid probe of "a payable resource" is always challenged — overstating a
   behaviour that availability-gated and parameterized resources deliberately do
   not have.  Both sat alongside the corrected PR3 statements, so the same
   surface contradicted itself.

As elsewhere, status codes are never the whole assertion: non-settlement is
measured against the facilitator and MPP spies, non-execution against a query
counter.
"""

from __future__ import annotations

import re

import pytest
from support.payment_harness import (
    AGENT_HEADERS,
    counting_engine,
    rows_engine,
    unpaid_headers,
)

import routers.prices as prices_router

_BARE = "/v1/prices/history"
_VALID = "/v1/prices/history?symbol_exchange=IBM-N"
_SEMANTIC_INVALID = "/v1/prices/history?symbol_exchange=IBM"
_STRUCTURAL_INVALID = "/v1/prices/history?symbol_exchange=IBM-N&limit=0"

_PRICE_ROW = {
    "weekdate": "2026-01-02", "exchange": "N", "symbol": "IBM", "type": "CS",
    "currency_code": "USD", "price": 100.0, "adj_close": 100.0,
    "pr_week_hi": 101.0, "pr_week_lo": 99.0, "volume": 1000, "trades": 10,
    "split_fact": 1.0, "pr_change": 0.5,
}


@pytest.fixture
def priced_engines(monkeypatch):
    monkeypatch.setattr(prices_router, "get_engine", lambda: rows_engine([_PRICE_ROW]))


def _assert_nothing_moved(harness) -> None:
    assert harness.verify_count == 0, "facilitator verify must not run"
    assert harness.settle_count == 0, "facilitator settle must not run"
    assert harness.mpp.authorize_count == 0, "MPP must not authorize"
    assert harness.mpp.capture_count == 0, "MPP must not capture"
    assert harness.mpp.void_count == 0, "MPP must not void"


# ===========================================================================
# 1 — the proof discriminator matches the artifact carriers enforcement accepts
# ===========================================================================

_AUTHORIZATION_HINT = {"Authorization": "x402 something"}


def test_01_authorization_x402_bare_probe_is_challenged(payment_harness, monkeypatch):
    """
    Requirement A: the reproduction, inverted.

    `Authorization: x402 …` carries no artifact the verify/settle path can
    consume, so the caller is unpaid and must receive the payment contract at
    the canonical URL rather than an input error.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    headers = dict(AGENT_HEADERS)
    headers.update(_AUTHORIZATION_HINT)

    response = payment_harness.client.get(_BARE, headers=headers)

    assert response.status_code == 402, (
        "an Authorization: x402 hint suppressed the challenge; the guard is "
        "accepting a carrier the enforcement path cannot consume"
    )
    assert response.json()["error"] == "payment_required"
    assert "payment-required" in response.headers
    assert response.json()["payment_required"]["x402Version"] == 2
    assert len(queries) == 0, "the paid endpoint executed for an unpaid probe"
    _assert_nothing_moved(payment_harness)


@pytest.mark.parametrize(
    ("label", "extra"),
    [
        ("network", {"X-StockTrends-Payment-Network": "eip155:8453"}),
        ("token", {"X-StockTrends-Payment-Token": "0xtoken"}),
        ("amount", {"X-StockTrends-Payment-Amount": "150000"}),
        ("reference", {"X-StockTrends-Payment-Reference": "ref"}),
        ("channel id", {"X-StockTrends-Payment-Channel-Id": "chan"}),
        ("method declaration", {"X-StockTrends-Payment-Method": "x402"}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_02_authorization_hint_plus_metadata_is_still_unpaid(
    payment_harness, priced_engines, label, extra
):
    """
    Requirement B: describing a payment does not become paying it in company.

    Neither carrier is an artifact; combining two non-artifacts does not make
    one, and the caller still needs the challenge.
    """
    headers = dict(AGENT_HEADERS)
    headers.update(_AUTHORIZATION_HINT)
    headers.update(extra)

    response = payment_harness.client.get(_BARE, headers=headers)

    assert response.status_code == 402, label
    assert response.json()["error"] == "payment_required", label
    assert "payment-required" in response.headers, label
    _assert_nothing_moved(payment_harness)


def test_02b_every_non_artifact_carrier_at_once_is_still_unpaid(
    payment_harness, priced_engines
):
    """The whole set of non-artifact signals together is still not payment."""
    headers = dict(AGENT_HEADERS)
    headers.update(_AUTHORIZATION_HINT)
    headers.update(
        {
            "X-StockTrends-Payment-Method": "x402",
            "X-StockTrends-Payment-Network": "eip155:8453",
            "X-StockTrends-Payment-Token": "0xtoken",
            "X-StockTrends-Payment-Amount": "150000",
            "X-StockTrends-Payment-Reference": "ref",
            "X-StockTrends-Payment-Channel-Id": "chan",
        }
    )

    response = payment_harness.client.get(_BARE, headers=headers)

    assert response.status_code == 402
    assert response.json()["error"] == "payment_required"
    _assert_nothing_moved(payment_harness)


@pytest.mark.parametrize("proof_header", ["X-Payment", "PAYMENT-SIGNATURE"])
@pytest.mark.parametrize(
    ("label", "url", "expected"),
    [
        ("no input", _BARE, 400),
        ("semantic invalid", _SEMANTIC_INVALID, 400),
        ("structural invalid", _STRUCTURAL_INVALID, 422),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_03_real_artifact_carriers_remain_payment_bearing(
    payment_harness, monkeypatch, proof_header, label, url, expected
):
    """
    Requirements C and D: the positive controls.

    Narrowing the guard must not have made everything unpaid.  Each published
    carrier still routes the request down the validated path, where an
    incomplete request receives its application error — and settles nothing.
    """
    engine, queries = counting_engine([_PRICE_ROW])
    monkeypatch.setattr(prices_router, "get_engine", lambda: engine)

    headers = dict(AGENT_HEADERS)
    headers[proof_header] = "an-artifact-the-facilitator-would-judge"

    response = payment_harness.client.get(url, headers=headers)

    assert response.status_code == expected, f"{proof_header}/{label}"
    assert len(queries) == 0, f"{proof_header}/{label}: the paid endpoint executed"
    _assert_nothing_moved(payment_harness)
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0


def test_04_guard_and_enforcement_accept_the_same_carrier_set():
    """
    Requirement E, at the predicate level.

    The early-challenge discriminator and the predicate `enforce_x402_payment`
    gates on must agree on every candidate carrier.  Any disagreement is two
    definitions of "paid", and the request that falls between them is exactly
    the one PR3 must challenge.
    """
    from payments.challenge import presents_x402_payment_proof
    from payments.x402 import extract_payment_signature, has_payment_signature
    from payments.x402_contract import X402_PROOF_HEADERS

    candidates: list[dict] = [
        {},
        {"X-StockTrends-Agent-Id": "agent"},
        {"Authorization": "x402 something"},
        {"authorization": "x402 something"},
        {"Authorization": "Bearer token"},
        {"X-StockTrends-Payment-Method": "x402"},
        {"X-StockTrends-Payment-Network": "eip155:8453"},
        {"X-StockTrends-Payment-Token": "0xtoken"},
        {"X-StockTrends-Payment-Amount": "150000"},
        {"X-StockTrends-Payment-Reference": "ref"},
        {"X-StockTrends-Payment-Channel-Id": "chan"},
    ]
    for header in X402_PROOF_HEADERS:
        candidates.append({header: "artifact"})
        candidates.append({header.lower(): "artifact"})
        candidates.append({header: "artifact", "Authorization": "x402 hint"})

    for headers in candidates:
        guard = presents_x402_payment_proof(headers)
        enforceable = has_payment_signature(headers)
        assert guard == enforceable, (
            f"{sorted(headers)}: the early-challenge guard says paid={guard} "
            f"while the enforcement predicate says paid={enforceable}; one "
            "request would be challenged and settled under two different "
            "definitions of payment"
        )
        # And the artifact the facilitator would actually receive exists exactly
        # when the guard says a payment was presented.
        assert (extract_payment_signature(headers) is not None) == guard, (
            f"{sorted(headers)}: the guard and the extracted artifact disagree"
        )

    assert presents_x402_payment_proof(None) is False
    assert has_payment_signature(None) is False


def test_04b_enforcement_treats_an_authorization_hint_as_unpaid():
    """
    Requirement E, at the behavioural level.

    Asserted against `enforce_x402_payment` itself rather than a predicate, so
    the claim survives a change in how enforcement decides.  A request carrying
    only the hint reaches the challenge branch — which is precisely why the
    early guard must not have called it paid.
    """
    from decimal import Decimal

    from payments.enforcement import enforce_x402_payment

    def _enforce(headers):
        return enforce_x402_payment(
            headers=headers,
            path=_BARE,
            method="GET",
            amount_usd=Decimal("0.15"),
            validation_valid=True,
            validation_error=None,
            validation_detail=None,
            validated_payment_reference=None,
            validated_payment_network=None,
            validated_payment_token=None,
            validated_payment_amount_native=None,
            replay_checker=lambda _reference: False,
        )

    hint_only = _enforce({"Authorization": "x402 something"})
    assert hint_only.outcome == "challenge", (
        "enforcement does not consume Authorization: x402 as an artifact, so "
        "the early-challenge guard must not treat it as payment either"
    )

    with_artifact = _enforce({"X-Payment": "an-artifact"})
    assert with_artifact.outcome != "challenge", (
        "a published proof carrier no longer reaches enforcement"
    )


def test_04c_widening_the_proof_set_requires_changing_the_published_contract():
    """
    Structural guard: one place defines what an artifact carrier is.

    The proof predicate must resolve `X402_PROOF_HEADERS` and nothing else, so a
    future widening cannot happen in the guard alone — it has to happen in the
    contract that discovery, OpenAPI, CORS and enforcement all read.
    """
    import inspect

    import payments.challenge as challenge_module
    import payments.x402 as x402_module

    proof_source = inspect.getsource(x402_module.has_x402_payment_proof)
    body = proof_source.split('"""')[-1]
    assert "has_payment_signature(" in body, (
        "has_x402_payment_proof no longer delegates to the enforceable "
        "signature predicate"
    )
    for carrier in ("authorization", "x-payment", "payment-signature"):
        assert carrier not in body.lower(), (
            f"has_x402_payment_proof names {carrier!r} directly instead of "
            "resolving the published X402_PROOF_HEADERS contract"
        )

    guard_source = inspect.getsource(challenge_module.presents_x402_payment_proof)
    guard_body = guard_source.split('"""')[-1]
    assert "has_x402_payment_proof(" in guard_body


# ===========================================================================
# 2 — public lifecycle guidance says one thing, everywhere
# ===========================================================================

#: Phrases that assert, or imply, that a challenge follows serviceability.
#: Each was live somewhere on a public surface before this remediation.
_STALE_LIFECYCLE_PHRASES = (
    "otherwise-serviceable",
    "after request validation",
    "before expecting an execution-time 402",
    "must be serviceable before a 402",
    "a request must be serviceable before",
    "already-serviceable request",
)


def _iter_strings(payload, path: str = ""):
    if isinstance(payload, str):
        yield path, payload
    elif isinstance(payload, dict):
        for key, value in payload.items():
            yield from _iter_strings(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from _iter_strings(value, f"{path}[{index}]")


_PUBLIC_LIFECYCLE_SURFACES = (
    "/v1/ai/context",
    "/v1/ai/tools",
    "/.well-known/ai-plugin.json",
)


@pytest.mark.parametrize("surface", _PUBLIC_LIFECYCLE_SURFACES)
def test_05_no_public_surface_carries_stale_lifecycle_language(
    payment_harness, surface
):
    """
    Asserted against the rendered response, not against module constants.

    A constant that is never served is harmless; what an agent reads is what
    the contract is.
    """
    payload = payment_harness.client.get(surface).json()

    offenders = [
        (path, text)
        for path, text in _iter_strings(payload)
        for phrase in _STALE_LIFECYCLE_PHRASES
        if phrase in text.lower()
    ]
    assert not offenders, (
        f"{surface} still serves old-architecture lifecycle language:\n  "
        + "\n  ".join(f"{path}: {text[:200]}" for path, text in offenders)
    )


@pytest.mark.parametrize("surface", _PUBLIC_LIFECYCLE_SURFACES)
def test_06_challenge_claims_name_the_class_they_apply_to(payment_harness, surface):
    """
    No unqualified universal about when a challenge is reachable.

    Availability-gated and parameterized resources are deliberate exceptions, so
    any statement about probing an unpaid resource has to say which class it
    describes.
    """
    payload = payment_harness.client.get(surface).json()

    unqualified = [
        (path, text)
        for path, text in _iter_strings(payload)
        if "unpaid probe" in text.lower() and "fixed-price" not in text.lower()
    ]
    assert not unqualified, (
        f"{surface} makes an unqualified claim about unpaid probes; "
        "availability-gated and parameterized resources are exceptions:\n  "
        + "\n  ".join(f"{path}: {text[:200]}" for path, text in unqualified)
    )


@pytest.mark.parametrize("surface", _PUBLIC_LIFECYCLE_SURFACES)
def test_07_serviceability_is_located_at_payment_not_at_the_challenge(
    payment_harness, surface
):
    """
    Where a statement ties serviceability to the 402, it must tie it to paying.

    "Construct a serviceable request before paying" is correct and remains.
    "Construct a serviceable request before receiving the challenge" is not, for
    an eligible fixed-price resource.
    """
    payload = payment_harness.client.get(surface).json()

    misplaced = []
    for path, text in _iter_strings(payload):
        low = text.lower()
        if "serviceable" not in low or "402" not in low:
            continue
        locates_at_payment = "before paying" in low or "when you pay" in low
        names_the_class = "fixed-price" in low
        if not (locates_at_payment or names_the_class):
            misplaced.append((path, text))

    assert not misplaced, (
        f"{surface} ties serviceability to the 402 without locating it at "
        "payment or naming the eligible class:\n  "
        + "\n  ".join(f"{path}: {text[:220]}" for path, text in misplaced)
    )


def test_08_ai_context_and_ai_tools_do_not_contradict_each_other(payment_harness):
    """
    The two guidance surfaces state one model.

    Both previously carried the corrected PR3 statement *and* an
    old-architecture statement beside it, so an agent reading either surface
    end-to-end was told two incompatible things.
    """
    for surface in ("/v1/ai/context", "/v1/ai/tools"):
        payload = payment_harness.client.get(surface).json()
        combined = " ".join(text for _path, text in _iter_strings(payload)).lower()

        assert "eligible recognized fixed-price" in combined, (
            f"{surface} never names the class the relaxed precondition applies to"
        )
        assert "availability-gated" in combined, (
            f"{surface} states the relaxed precondition without its exceptions"
        )
        assert "before paying" in combined, (
            f"{surface} no longer tells agents to make the request serviceable "
            "before paying"
        )


def test_09_static_narrative_surfaces_agree_with_the_served_ones():
    """
    `llms.txt` and the static tools manifest carry the same model.

    They are shipped files rather than rendered responses, so nothing else in
    this suite would catch them drifting back.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    llms = " ".join((repo_root / "static" / "llms.txt").read_text(encoding="utf-8").split())
    tools = (repo_root / "static" / "tools.json").read_text(encoding="utf-8")

    for phrase in _STALE_LIFECYCLE_PHRASES:
        assert phrase not in llms.lower(), f"llms.txt still says {phrase!r}"
        assert phrase not in tools.lower(), f"tools.json still says {phrase!r}"

    assert "not in order to be challenged" in llms, (
        "llms.txt no longer separates serviceable-before-paying from "
        "serviceable-before-being-challenged"
    )
    assert "eligible recognized fixed-price" in llms
    assert "eligible recognized fixed-price" in tools


# ===========================================================================
# The structured lifecycle contract is retained, not merely the prose
# ===========================================================================

@pytest.mark.parametrize(
    ("label", "method", "path", "expected_class", "requires_serviceable"),
    [
        ("fixed price", "GET", "/v1/prices/history", "fixed_price", False),
        (
            "availability gated",
            "GET",
            "/v1/intelligence/guidance/latest",
            "availability_gated",
            True,
        ),
        (
            "parameterized",
            "GET",
            "/v1/intelligence/guidance/{artifact_id}",
            "availability_gated",
            True,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_10_structured_lifecycle_distinctions_survive_the_prose_edit(
    payment_harness, label, method, path, expected_class, requires_serviceable
):
    """
    Correcting the prose must not have disturbed the machine-readable contract.

    The per-resource block is what a client actually acts on; the prose exists
    to agree with it.
    """
    schema = payment_harness.client.get("/v1/openapi.json").json()
    operation = schema["paths"][path.removeprefix("/v1")][method.lower()]
    openapi = operation["x-stocktrends-payment"]["challenge_lifecycle"]

    manifest = payment_harness.client.get("/.well-known/x402").json()
    discovery = next(
        resource["challenge_lifecycle"]
        for resource in manifest["resources"]
        if resource["method"] == method and resource["path"] == path
    )

    assert openapi == discovery, f"{label}: OpenAPI and x402 discovery disagree"
    assert openapi["challenge_class"] == expected_class, label
    assert openapi["serviceable_request_required_before_challenge"] is (
        requires_serviceable
    ), label
    assert openapi["serviceable_request_required_before_settlement"] is True, label


def test_11_the_corrected_prose_matches_observed_runtime(
    payment_harness, priced_engines
):
    """
    Closing the loop: the surfaces now say what the system does.

    A bare unpaid probe of the fixed-price resource is challenged; the
    availability-gated resource is not.  Both are what the guidance now claims.
    """
    fixed = payment_harness.client.get(_BARE, headers=unpaid_headers())
    assert fixed.status_code == 402
    assert fixed.json()["error"] == "payment_required"

    gated = payment_harness.client.get(
        "/v1/intelligence/guidance/latest", headers=unpaid_headers()
    )
    assert gated.status_code in {404, 503}, (
        "an availability-gated resource was challenged ahead of its gate"
    )
    _assert_nothing_moved(payment_harness)


def test_12_no_public_surface_promises_a_402_follows_validation(payment_harness):
    """
    The specific contradiction, as a phrase-level guard across every surface.

    Stated separately from the broader scan so a regression names itself.
    """
    pattern = re.compile(r"402[^.]{0,80}after\s+(request\s+)?validation", re.IGNORECASE)

    for surface in _PUBLIC_LIFECYCLE_SURFACES:
        raw = payment_harness.client.get(surface).text
        assert not pattern.search(raw), (
            f"{surface} still tells agents a 402 arrives after request validation"
        )
