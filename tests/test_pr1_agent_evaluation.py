"""
PR1 — neutral decision-before-purchase guidance, and the evidence map.

What these tests are for
------------------------
The guidance added in this PR has to do two opposite things at once: explain
enough that an autonomous client can form an informed view, while never telling
it what to conclude. Those pull in opposite directions, so both edges are pinned
here.

A denylist alone cannot prove neutrality — it can only catch the phrasings
somebody thought of. Every denylist assertion below is therefore paired with a
positive assertion naming the exact approved semantics, and the denylist itself
carries a positive control proving it can fail.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

# Module stubs for sqlalchemy/db/etc. are provided by tests/conftest.py.
import main
from discovery.endpoint_metadata import (
    build_compact_endpoint_preview,
    build_endpoint_preview,
)
from discovery.provenance import EVIDENCE_FAMILIES, evidence_map
from discovery.service_meta import (
    SERVICE_AUGMENTATION_ROLE,
    SERVICE_EVALUATION_GUIDANCE,
    SERVICE_EVALUATION_GUIDANCE_POINTER,
    SERVICE_EVALUATION_GUIDANCE_SOURCE,
    SERVICE_EVALUATION_GUIDANCE_SUMMARY,
    SERVICE_OPENAPI_GUIDANCE,
)
from discovery.x402_discovery import build_x402_discovery
from routers.ai import ai_context, ai_tools


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def context():
    return ai_context()


@pytest.fixture(scope="module")
def tools():
    return ai_tools()


@pytest.fixture(scope="module")
def static_manifest():
    with open("static/tools.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def llms_txt():
    with open("static/llms.txt", encoding="utf-8") as handle:
        return handle.read()


# ===========================================================================
# 1. One canonical definition, referenced rather than copied
# ===========================================================================

def test_canonical_guidance_is_defined_once_and_referenced_elsewhere(context, tools):
    """
    The full statement lives on exactly one surface.

    Duplicating it across five surfaces is how they drift apart: an edit to one
    copy silently leaves the others stating something else. Every other surface
    carries a shorter form plus a pointer back to the canonical source.
    """
    assert context["acquisition_evaluation_guidance"]["guidance"] == SERVICE_EVALUATION_GUIDANCE

    # The full text appears nowhere else.
    assert SERVICE_EVALUATION_GUIDANCE not in json.dumps(tools)
    assert SERVICE_EVALUATION_GUIDANCE not in SERVICE_OPENAPI_GUIDANCE

    # And everything that references it says where the full statement lives.
    assert (
        context["acquisition_evaluation_guidance"]["canonical_source"]
        == SERVICE_EVALUATION_GUIDANCE_SOURCE
    )
    assert (
        tools["acquisition_evaluation_guidance"]["canonical_source"]
        == SERVICE_EVALUATION_GUIDANCE_SOURCE
    )


def test_openapi_guidance_carries_the_evaluation_clause(client):
    main.v1.openapi_schema = None
    schema = client.get("/v1/openapi.json").json()
    guidance = schema["info"]["x-guidance"]

    assert SERVICE_EVALUATION_GUIDANCE_SUMMARY in guidance
    # The word band the pre-existing guidance test enforces still has to hold:
    # the clause is an addition to a budgeted string, not a licence to bloat it.
    assert 75 <= len(guidance.split()) <= 250


def test_ai_tools_carries_the_summary_form(tools):
    assert tools["acquisition_evaluation_guidance"]["guidance"] == SERVICE_EVALUATION_GUIDANCE_SUMMARY


def test_canonical_x402_discovery_carries_the_service_level_reference():
    """
    `/.well-known/x402` is the canonical payable-resource discovery surface, so a
    client reading it is by definition deciding whether to acquire something.
    The evaluation procedure belongs there once, at service level.
    """
    manifest = build_x402_discovery()
    block = manifest["acquisition_evaluation"]

    assert block["guidance"] == SERVICE_EVALUATION_GUIDANCE_SUMMARY
    assert block["canonical_source"] == SERVICE_EVALUATION_GUIDANCE_SOURCE
    assert set(block["inspect_before_deciding"]) == {
        "what_is_offered",
        "how_to_interpret_it",
        "what_evidence_exists",
        "what_it_costs",
    }
    # The canonical surface names itself as where payable resources are found.
    assert any(
        url.endswith("/.well-known/x402")
        for url in block["inspect_before_deciding"]["what_is_offered"]
    )


def test_evaluation_guidance_is_not_repeated_on_every_payable_resource():
    """
    Service-level, not per-resource.

    Repeating the guidance on each of the manifest's resources would inflate it
    and give one sentence many places to drift apart. The manifest states it
    once; the resource entries carry only their own contract.
    """
    manifest = build_x402_discovery()
    assert manifest["resources"], "manifest must describe payable resources"
    for resource in manifest["resources"]:
        assert "evaluation_guidance" not in resource
        assert "acquisition_evaluation" not in resource


def test_the_402_challenge_preview_still_points_at_the_procedure():
    """
    The full preview is what production emits as `stocktrends_preview`. It is the
    last surface before money moves, and it is the only one an agent sees if it
    skipped discovery entirely, so it keeps a short pointer.
    """
    preview = build_endpoint_preview("/v1/breadth/sector/history")
    assert preview["evaluation_guidance"] == SERVICE_EVALUATION_GUIDANCE_POINTER
    assert preview["evaluation_guidance_source"] == SERVICE_EVALUATION_GUIDANCE_SOURCE


def test_compact_preview_defers_to_the_canonical_discovery_surface():
    """
    The compact builder exists to keep a challenge body small and is not on any
    production path — `middleware/metering.py` emits the full preview. With the
    procedure published at service level on `/.well-known/x402`, a per-resource
    copy here would be duplication for no reader.
    """
    compact = build_compact_endpoint_preview("/v1/breadth/sector/history")
    assert "evaluation_guidance" not in compact
    # It still tells a client where to go.
    assert compact["discovery"]["x402_discovery"].endswith("/.well-known/x402")
    assert compact["discovery"]["ai_context"].endswith("/v1/ai/context")


def test_static_artifacts_stay_semantically_aligned(static_manifest, llms_txt):
    """Static manifests are hand-maintained, so parity is asserted, not assumed."""
    assert "acquisition_evaluation_guidance" in llms_txt or "Deciding Whether To Acquire" in llms_txt
    for anchor in (
        "/pricing/catalog",
        "/selections/stim-select/outcomes/summary",
        "/meta/inference",
        "not evidence of usefulness",
    ):
        assert anchor in llms_txt, f"llms.txt lost the {anchor!r} evaluation anchor"

    assert static_manifest["tools"], "static manifest must not be empty"


# ===========================================================================
# 2. Conclusion neutrality — positive assertions first, denylist second
# ===========================================================================

# The exact semantics the Chief Architect approved. These are asserted
# positively so that "neutral" cannot be achieved by deleting the guidance: a
# surface stripped to meaningless generalities fails here.
APPROVED_SEMANTICS = {
    "inspect_before_deciding": r"before deciding",
    "what_is_offered": r"what is offered",
    "how_to_interpret": r"how it should be interpreted",
    "what_evidence": r"what evidence",
    "what_it_costs": r"what it costs",
    "category_separation": r"is not methodology, and methodology is not\s+evidence",
    "availability_is_not_usefulness": r"[Aa]vailability is not evidence of usefulness",
    "task_relevance": r"only the resources relevant to the task",
    "decide_from_inspection": r"on the material actually inspected",
}


@pytest.mark.parametrize("name,pattern", sorted(APPROVED_SEMANTICS.items()))
def test_canonical_guidance_states_each_approved_semantic(name, pattern):
    assert re.search(pattern, SERVICE_EVALUATION_GUIDANCE), (
        f"canonical guidance no longer states the approved {name!r} semantic"
    )


def test_summary_form_preserves_the_load_bearing_semantics():
    """The compact form may lose words, but not the rules that make it neutral."""
    lowered = SERVICE_EVALUATION_GUIDANCE_SUMMARY.lower()
    assert "first inspect" in lowered
    assert "separate categories" in lowered
    assert "do not infer usefulness from availability" in lowered
    assert "actually inspected" in lowered


# Phrasings that would turn evaluation guidance into persuasion. This is a
# regression heuristic, not a proof — see the positive control below.
PERSUASION_DENYLIST = (
    "you should buy",
    "you should purchase",
    "worth buying",
    "we recommend purchasing",
    "will improve your",
    "improves investment performance",
    "proven to add alpha",
    "adds alpha",
    "generates alpha",
    "will outperform",
    "outperforms alternatives",
    "guaranteed return",
    "proves an investment edge",
    "buying additional endpoints",
)


def _guidance_corpus(context, tools) -> str:
    return " ".join(
        [
            SERVICE_EVALUATION_GUIDANCE,
            SERVICE_EVALUATION_GUIDANCE_SUMMARY,
            SERVICE_EVALUATION_GUIDANCE_POINTER,
            SERVICE_AUGMENTATION_ROLE,
            SERVICE_OPENAPI_GUIDANCE,
            json.dumps(context["acquisition_evaluation_guidance"]),
            json.dumps(context["augmentation_role"]),
            json.dumps(context["evidence"]),
            json.dumps(tools["acquisition_evaluation_guidance"]),
        ]
    ).lower()


def test_guidance_surfaces_carry_no_purchase_persuasion(context, tools):
    corpus = _guidance_corpus(context, tools)
    hits = [phrase for phrase in PERSUASION_DENYLIST if phrase in corpus]
    assert not hits, f"guidance acquired persuasive phrasing: {hits}"


def test_persuasion_denylist_has_a_positive_control():
    """
    An absence assertion that has never been shown to fail has not been shown to
    work. This drives the denylist over text that must trip it.
    """
    planted = (
        "Stock Trends is proven to add alpha and will improve your model, "
        "so you should purchase the premium endpoints."
    ).lower()
    hits = [phrase for phrase in PERSUASION_DENYLIST if phrase in planted]
    assert len(hits) >= 3, f"denylist failed to fire on planted persuasion: {hits}"


def test_guidance_tells_the_caller_the_decision_is_theirs(context):
    block = context["acquisition_evaluation_guidance"]
    assert "does not state a conclusion" in block["decision_is_the_caller's"]
    assert "does not recommend acquiring" in block["decision_is_the_caller's"]


def test_guidance_does_not_require_paid_execution_to_evaluate(context, tools):
    """
    Evaluating the service must not itself cost money, or the guidance would be
    instructing paid execution as a side effect of deciding whether to pay.
    """
    for block in (
        context["acquisition_evaluation_guidance"],
        tools["acquisition_evaluation_guidance"],
    ):
        assert "public and non-metered" in block["no_payment_required_to_evaluate"]

    named = json.dumps(tools["acquisition_evaluation_guidance"]["inspect_before_deciding"])
    assert "/.well-known/x402" in named, (
        "the guidance must name the canonical payable-resource discovery surface"
    )
    public_paths = {
        "/v1/ai/tools",
        "/v1/workflows",
        "/v1/meta/inference",
        "/v1/meta/stim",
        "/v1/meta/indicators",
        "/v1/selections/stim-select/outcomes/summary",
        "/v1/stocktrends/portfolios",
        "/v1/ai/proof/market-edge",
        "/v1/pricing/catalog",
        "/v1/cost-estimate",
    }
    for path in re.findall(r"/v1/[\w\-/{}]+", named):
        assert path in public_paths, f"guidance names non-public resource {path}"


def test_guidance_separates_capability_interpretation_evidence_and_pricing(tools):
    inspect = tools["acquisition_evaluation_guidance"]["inspect_before_deciding"]

    assert set(inspect) == {
        "what_is_offered",
        "how_to_interpret_it",
        "what_evidence_exists",
        "what_it_costs",
    }
    assert "/v1/ai/tools" in inspect["what_is_offered"]
    assert "/v1/meta/inference" in inspect["how_to_interpret_it"]
    assert "/v1/selections/stim-select/outcomes/summary" in inspect["what_evidence_exists"]
    assert "/v1/pricing/catalog" in inspect["what_it_costs"]

    # The categories must not be pooled: no resource may appear under two of them.
    seen: dict[str, str] = {}
    for category, paths in inspect.items():
        for path in paths:
            assert path not in seen, (
                f"{path} appears under both {seen[path]} and {category}; the "
                "category separation is what stops a description being read as evidence"
            )
            seen[path] = category


# ===========================================================================
# 3. Product-value explanation survives — augmentation role and evidence map
# ===========================================================================

def test_augmentation_role_is_published_and_describes_role_not_benefit(context):
    role = context["augmentation_role"]
    assert role["role"] == SERVICE_AUGMENTATION_ROLE

    lowered = SERVICE_AUGMENTATION_ROLE.lower()
    assert "augment an existing analytical process rather than to replace it" in lowered
    # It says what Stock Trends does not supply, which is the part that stops
    # "augments your process" drifting into "improves your results".
    assert role["does_not_supply"]
    assert any("objective" in item for item in role["does_not_supply"])
    assert "for the consumer to determine" in role["fit_assessment"]


def test_evidence_families_are_separate_and_each_carries_its_own_terms(context):
    families = context["evidence"]["families"]
    ids = [f["family_id"] for f in families]

    assert ids == [
        "historical_classification_provenance",
        "inference_outcome_evidence",
        "model_portfolio_and_strategy_records",
    ]
    for family in families:
        assert family["methodology"].strip()
        assert family["provenance"].strip()
        assert family["what_it_is"].strip()
        assert family["what_it_is_not"].strip()
        assert family["limitations"], f"{family['family_id']} lost its limitations"
        assert family["inspect_at"], f"{family['family_id']} names no resource"


def test_evidence_families_are_not_collapsed_into_one_performance_claim(context):
    rule = context["evidence"]["separation_rule"].lower()
    assert "different methodologies" in rule
    assert "do not combine them into a single performance, edge, or alpha claim" in rule


def test_classification_provenance_is_not_presented_as_a_result():
    family = next(
        f for f in EVIDENCE_FAMILIES
        if f["family_id"] == "historical_classification_provenance"
    )
    assert "not a performance result" in family["what_it_is_not"].lower()


def test_model_portfolio_evidence_declares_it_is_not_audited_performance():
    family = next(
        f for f in EVIDENCE_FAMILIES
        if f["family_id"] == "model_portfolio_and_strategy_records"
    )
    assert "not audited brokerage-account performance" in family["what_it_is_not"].lower()
    assert any(
        "not audited brokerage-account performance" in limit.lower()
        for limit in family["limitations"]
    )


def test_outcome_evidence_stays_aggregate_only():
    family = next(
        f for f in EVIDENCE_FAMILIES if f["family_id"] == "inference_outcome_evidence"
    )
    assert "not current selections" in family["what_it_is_not"].lower()
    assert any("aggregate only" in limit.lower() for limit in family["limitations"])


def test_illustrative_surface_is_not_labelled_as_evidence(context):
    surface = context["evidence"]["illustrative_structure_surface"]
    assert surface["endpoint"] == "/v1/ai/proof/market-edge"
    assert "not outcome or performance evidence" in surface["what_it_is_not"].lower()


def test_evidence_map_makes_no_repository_unsupported_numeric_claim():
    """
    The Developer Portal publishes figures (for example a mature-observation
    count) that no repository contract holds. Machine surfaces may only state
    numbers this repository can source, so those must not appear here.
    """
    corpus = json.dumps(evidence_map())
    for unsupported in ("156K", "156,000", "156000"):
        assert unsupported not in corpus

    # The two figures the repository does hold are still stated.
    assert "1980" in corpus
    assert "16M+" in corpus


# ===========================================================================
# 4. The advertised route that never existed
# ===========================================================================

def test_no_discovery_surface_advertises_the_nonexistent_dataset_manifest(
    context, tools, static_manifest, llms_txt, client
):
    """
    `/ai-dataset.json` was published as `dataset_manifest` but no route ever
    served it, so an agent following the pointer got a 404. A discovery link
    that does not resolve is worse than an absent one.
    """
    surfaces = {
        "/v1/ai/context": json.dumps(context),
        "/v1/ai/tools": json.dumps(tools),
        "static/tools.json": json.dumps(static_manifest),
        "static/llms.txt": llms_txt,
        "/": json.dumps(client.get("/").json()),
        "/.well-known/ai-plugin.json": json.dumps(
            client.get("/.well-known/ai-plugin.json").json()
        ),
    }
    for name, body in surfaces.items():
        assert "ai-dataset.json" not in body, (
            f"{name} advertises /ai-dataset.json, which no route serves"
        )


def test_the_dataset_manifest_route_really_is_absent(client):
    """The positive control for the test above: the pointer was genuinely dead."""
    assert client.get("/ai-dataset.json").status_code == 404


# ===========================================================================
# 5. Public evidence is reachable from root discovery
# ===========================================================================

def test_root_discovery_names_the_public_evidence_resources(client):
    body = client.get("/").json()

    assert body["evidence_map"] == "https://api.stocktrends.com/v1/ai/context"
    for path in (
        "/v1/selections/stim-select/outcomes/summary",
        "/v1/stocktrends/portfolios",
        "/v1/stocktrends/strategies",
    ):
        assert f"https://api.stocktrends.com{path}" in body["evidence"]


def test_root_lists_the_static_illustration_apart_from_evidence(client):
    """
    /v1/ai/proof/market-edge returns a static synthetic body. It shows a shape;
    it measures nothing, so it is not an evidence resource. This assertion was
    inverted in the reviewed implementation.
    """
    body = client.get("/").json()

    assert "/v1/ai/proof/market-edge" not in json.dumps(body["evidence"])
    assert body["illustrative_capability_example"] == (
        "https://api.stocktrends.com/v1/ai/proof/market-edge"
    )
