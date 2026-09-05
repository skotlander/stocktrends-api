"""
PR1 — regressions for the contradictions the independent review found.

Every test here fails on the reviewed implementation (c490007). The point is not
that the fixes work; it is that the previous 1061 passing tests could not tell
the difference between a truthful machine contract and a plausible one.

Three classes of defect are covered:

* discovery advertising a parameter name the runtime does not accept;
* guidance claiming a response field that four endpoints did not return;
* a static synthetic illustration classified as empirical evidence.

Where a test asserts an absence, it carries a positive control.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.routing import APIRoute
from support.payment_harness import x402_headers

# Module stubs for sqlalchemy/db/etc. are provided by tests/conftest.py.
import main
import routers.market as market_router
from discovery.endpoint_metadata import build_endpoint_preview
from discovery.provenance import evidence_map
from discovery.x402_discovery import build_x402_discovery
from payments import policy_provider
from routers.ai import ai_context, ai_proof_market_edge, ai_tools
from routers.workflows import WORKFLOW_REGISTRY
from utils.history_bounds import HISTORY_ENDPOINT_BOUNDS, probe_limit

# ===========================================================================
# Finding 1 — canonical x402 input names must match the runtime contract
# ===========================================================================

def _runtime_request_parameters(schema: dict, v1_path: str) -> set[str]:
    """
    Parameter names FastAPI derived from the endpoint signature.

    This is the runtime side of the parity check. It comes from the function
    signature by way of FastAPI's own introspection, not from the discovery
    registry, so comparing the two is a real assertion rather than a restatement
    of one constant.

    Query *and* path parameters count. A templated route such as
    `/v1/intelligence/guidance/{artifact_id}` legitimately takes `artifact_id`
    in the path, and treating that as unaccepted would be a false mismatch.
    """
    operation = schema["paths"][v1_path]["get"]
    names = set()
    for param in operation.get("parameters", []):
        if "$ref" in param:
            continue  # shared agent/payment header refs, not endpoint inputs
        if param.get("in") in {"query", "path"}:
            names.add(param["name"])
    return names


@pytest.fixture(scope="module")
def openapi_schema():
    from fastapi.testclient import TestClient

    main.v1.openapi_schema = None
    with TestClient(main.app) as client:
        return client.get("/v1/openapi.json").json()


@pytest.fixture(scope="module")
def manifest():
    return build_x402_discovery()


def test_canonical_x402_inputs_are_accepted_by_the_runtime(manifest, openapi_schema):
    """
    Every input the manifest advertises for a GET resource must be a parameter
    the endpoint actually accepts.

    The reviewed implementation advertised `start` for market regime history
    while the endpoint accepts `start_date`, so an agent constructing the
    published request would have had its date filter silently ignored.
    """
    offenders: list[str] = []
    checked = 0
    for resource in manifest["resources"]:
        if resource["method"] != "GET":
            continue
        v1_path = resource["path"][len("/v1"):]
        if v1_path not in openapi_schema["paths"]:
            continue
        runtime = _runtime_request_parameters(openapi_schema, v1_path)
        advertised = set(resource["input_schema"].get("properties", {}))
        checked += 1
        unknown = advertised - runtime
        if unknown:
            offenders.append(f"{resource['path']}: advertises {sorted(unknown)}, runtime accepts {sorted(runtime)}")

    assert checked >= 15, f"parity check covered only {checked} GET resources"
    assert not offenders, "canonical discovery advertises inputs the runtime rejects:\n" + "\n".join(offenders)


def test_market_regime_history_advertises_start_date_never_start(manifest, openapi_schema):
    """The specific drift, pinned by name on every machine surface."""
    resource = next(
        r for r in manifest["resources"] if r["path"] == "/v1/market/regime/history"
    )
    advertised = set(resource["input_schema"]["properties"])
    assert "start_date" in advertised
    assert "start" not in advertised, "the stale `start` name is back in canonical discovery"

    runtime = _runtime_request_parameters(openapi_schema, "/market/regime/history")
    assert "start_date" in runtime and "start" not in runtime

    tool = next(t for t in ai_tools()["tools"] if t["endpoint"] == "/v1/market/regime/history")
    tool_inputs = set(tool["input_schema"]["properties"])
    assert "start_date" in tool_inputs and "start" not in tool_inputs


def test_safe_examples_only_use_parameters_the_runtime_accepts(manifest, openapi_schema):
    """A published example a client copies verbatim must be answerable."""
    offenders = []
    for resource in manifest["resources"]:
        example = resource.get("safe_example_request") or {}
        if example.get("method") != "GET":
            continue
        v1_path = resource["path"][len("/v1"):]
        if v1_path not in openapi_schema["paths"]:
            continue
        runtime = _runtime_request_parameters(openapi_schema, v1_path)
        unknown = set(example.get("query", {})) - runtime
        if unknown:
            offenders.append(f"{resource['path']}: example uses {sorted(unknown)}")
    assert not offenders, "safe examples use parameters the runtime rejects:\n" + "\n".join(offenders)


# ===========================================================================
# Finding 1 — regime history really does return only recent weeks
# ===========================================================================

class RecordingEngine:
    """Records statements and serves scripted rows; see test_pr1_history_bounds."""

    def __init__(self, responder):
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self._responder = responder

    def connect(self):
        return self

    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        bound = dict(params or {})
        self.executed.append((sql, bound))
        return _Result(self._responder(sql, bound))


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def engine_on(monkeypatch):
    def _install(module, responder):
        engine = RecordingEngine(responder)
        monkeypatch.setattr(module, "get_engine", lambda: engine)
        monkeypatch.setattr(module, "text", lambda sql: sql)
        return engine

    return _install


@pytest.fixture
def client(payment_harness):
    return payment_harness.client


def _regime_responder(available_weeks: list[date]):
    """A universe of weekdates, newest first, mirroring the endpoint's SQL."""

    def responder(sql: str, params: dict[str, Any]):
        if "DISTINCT weekdate" in sql:
            eligible = available_weeks
            if params.get("start_date") is not None:
                floor = params["start_date"]
                if isinstance(floor, str):
                    floor = date.fromisoformat(floor)
                eligible = [w for w in available_weeks if w >= floor]
            return [{"weekdate": w} for w in eligible[: params["limit"]]]
        # Aggregation: one bullish row per bound weekdate.
        return [
            {"weekdate": wd, "trend": "^+", "cnt": 100, "avg_rsi": 105.0, "avg_mt_cnt": 9.0}
            for wd in params.values()
            if isinstance(wd, date)
        ]

    return responder


def test_regime_history_returns_recent_weeks_not_an_arbitrary_study_window(
    client, engine_on
):
    """
    A far-past start_date does not select the 52 weeks following it.

    The SQL orders weekdates descending, so start_date only removes weeks from
    the eligible set. With ten years available and a 2016 floor, the caller still
    receives the newest weeks — which is why the workflow must not claim this
    covers an arbitrary research period.
    """
    weeks = [date(2026, 8, 28) - timedelta(weeks=i) for i in range(520)]
    engine_on(market_router, _regime_responder(weeks))

    body = client.get(
        "/v1/market/regime/history?limit=52&start_date=2016-01-01",
        headers=x402_headers(),
    ).json()

    returned = [entry["weekdate"] for entry in body["history"]]
    assert len(returned) == 52
    assert returned[0] == "2026-08-28", "the newest eligible week must lead"
    # Not the 52 weeks that follow the requested start_date.
    assert "2016-01-08" not in returned
    assert body["applied_bounds"]["truncated_by_limit"] is True, (
        "more eligible weeks existed than the limit allowed; that must be disclosed"
    )
    assert body["applied_bounds"]["start"] == "2016-01-01"
    assert body["applied_bounds"]["end"] is None, (
        "this endpoint has no end bound; reporting one would be untruthful"
    )


def test_regime_history_never_exceeds_its_52_week_ceiling(openapi_schema):
    limit = openapi_schema["paths"]["/market/regime/history"]["get"]["parameters"]
    limit_param = next(p for p in limit if p.get("name") == "limit")
    assert limit_param["schema"]["maximum"] == 52
    assert HISTORY_ENDPOINT_BOUNDS["/v1/market/regime/history"]["max_limit"] == 52


# ===========================================================================
# Finding 2 — applied_bounds must be behaviourally true, not merely claimed
# ===========================================================================

def _rows(count: int, **extra):
    base = {
        "weekdate": "2026-08-28", "symbol": "IBM", "exchange": "N", "type": "CS",
        "currency_code": "USD", "trend": "^+", "trend_cnt": 5, "mt_cnt": 9,
        "prev_mtcnt": 8, "rsi": 105, "rsi_updn": "+", "vol_tag": "", "rvol": 1.0,
        "atv": 1.0, "fpr_chg1": 0.0, "fpr_chg2": 0.0, "fpr_chg4": 0.0,
        "fpr_chg13": 0.0, "fpr_chg40": 0.0, "pr_chg13": 0.0, "pr_change": 0.0,
        "shortavg": 1.0, "longavg": 1.0, "yr_hi": 1.0, "yr_lo": 1.0,
        "price": 1.0, "adj_close": 1.0, "pr_week_hi": 1.0, "pr_week_lo": 1.0,
        "volume": 10, "trades": 1, "split_fact": 1.0, "prob13wk": 0.6,
        "sector_code": "S1", "sector_name": "Sector", "total": 10,
        "bullish_count": 6, "bearish_count": 3, "neutral_count": 1,
        "avg_trend_cnt": 5.0, "avg_mt_cnt": 9.0, "max_trend_cnt": 10,
        "max_mt_cnt": 20, "avg_rsi": 104.0, "rsi_ge_110_count": 1,
        "rsi_ge_120_count": 0, "young_bullish_count": 1, "mature_bullish_count": 1,
        "x4wk1": 1.0, "x4wk2": 2.0, "x4wk": 1.5, "x4wksd": 0.5,
        "x13wk1": 3.0, "x13wk2": 4.0, "x13wk": 3.5, "x13wksd": 0.5,
        "x40wk1": 7.0, "x40wk2": 8.0, "x40wk": 7.5, "x40wksd": 0.5,
        "n": 10, "bull_n": 6, "bull_pct": 0.6, "avg_trend_cnt_bullish": 5.0,
        "avg_trend_cnt_bearish": 4.0, "avg_mt_cnt_bullish": 9.0,
        "avg_mt_cnt_bearish": 8.0, "bull_avg_rsi": 108.0,
        "leadership_score": 70.0, "rank_in_week": 1, "shortname": "IBM",
        "fullname": "IBM", "industry_id": 1, "shares_os": 100,
    }
    base.update(extra)
    return [dict(base) for _ in range(count)]


# Every endpoint whose published `limit` description now claims an
# applied_bounds block. Derived from the shared bounds table so a new bounded
# endpoint is enrolled by the act of bounding it.
BOUNDS_ENDPOINTS = {
    "/v1/indicators/history": ("routers.indicators", "?symbol_exchange=IBM-N"),
    "/v1/prices/history": ("routers.prices", "?symbol_exchange=IBM-N"),
    "/v1/stim/history": ("routers.stim", "?symbol_exchange=IBM-N"),
    "/v1/market/regime/history": ("routers.market", ""),
    "/v1/breadth/sector/history": ("routers.breadth", ""),
    "/v1/leadership/rotation/history": ("routers.leadership", ""),
    "/v1/stwr/reports/history": ("routers.stwr", "?rpt=bullcross&exchange=N"),
    "/v1/selections/history": ("routers.selections", ""),
    "/v1/selections/published/history": ("routers.selections_published", ""),
}


def test_every_bounded_endpoint_is_enrolled_in_the_behavioural_check():
    assert set(BOUNDS_ENDPOINTS) == set(HISTORY_ENDPOINT_BOUNDS), (
        "an endpoint gained published bounds without a behavioural applied_bounds test"
    )


def _install(monkeypatch, module_name: str, row_count: int):
    import importlib

    module = importlib.import_module(module_name)

    def responder(sql: str, params: dict[str, Any]):
        if "MAX(weekdate)" in sql:
            return [{"weekdate": "2026-08-28", "wd": "2026-08-28"}]
        if "DISTINCT weekdate" in sql:
            weeks = [date(2026, 8, 28) - timedelta(weeks=i) for i in range(row_count)]
            return [{"weekdate": w} for w in weeks]
        if module_name == "routers.market":
            return [
                {"weekdate": wd, "trend": "^+", "cnt": 100, "avg_rsi": 105.0, "avg_mt_cnt": 9.0}
                for wd in params.values()
                if isinstance(wd, date)
            ]
        return _rows(min(row_count, params.get("limit", row_count)))

    engine = RecordingEngine(responder)
    monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(module, "text", lambda sql: sql)
    return engine


@pytest.mark.parametrize("path", sorted(BOUNDS_ENDPOINTS))
def test_applied_bounds_is_present_and_truthful(path, client, monkeypatch):
    module_name, query = BOUNDS_ENDPOINTS[path]
    bounds_spec = HISTORY_ENDPOINT_BOUNDS[path]
    _install(monkeypatch, module_name, row_count=3)

    body = client.get(f"{path}{query}", headers=x402_headers()).json()

    assert "applied_bounds" in body, f"{path} claims applied_bounds but does not return it"
    bounds = body["applied_bounds"]
    assert bounds["limit"] == bounds_spec["default_limit"]
    assert bounds["max_limit"] == bounds_spec["max_limit"]
    assert bounds["limit_source"] == "default"
    assert bounds["truncated_by_limit"] is False
    assert isinstance(bounds["rows_returned"], int)
    assert bounds["rows_returned"] <= bounds["limit"]


@pytest.mark.parametrize("path", sorted(BOUNDS_ENDPOINTS))
def test_caller_supplied_limit_is_reported_as_such(path, client, monkeypatch):
    module_name, query = BOUNDS_ENDPOINTS[path]
    _install(monkeypatch, module_name, row_count=2)
    joiner = "&" if query else "?"

    body = client.get(f"{path}{query}{joiner}limit=2", headers=x402_headers()).json()

    assert body["applied_bounds"]["limit"] == 2
    assert body["applied_bounds"]["limit_source"] == "caller_supplied"


@pytest.mark.parametrize("path", sorted(BOUNDS_ENDPOINTS))
def test_truncation_is_reported_and_the_limit_is_never_exceeded(
    path, client, monkeypatch
):
    """
    The row count and the disclosure must agree, with no off-by-one.

    More rows exist than the caller asked for, so `truncated_by_limit` must be
    true and the payload must still carry exactly `limit` rows.
    """
    module_name, query = BOUNDS_ENDPOINTS[path]
    requested = 2
    _install(monkeypatch, module_name, row_count=probe_limit(requested) + 5)
    joiner = "&" if query else "?"

    body = client.get(
        f"{path}{query}{joiner}limit={requested}", headers=x402_headers()
    ).json()
    bounds = body["applied_bounds"]

    assert bounds["truncated_by_limit"] is True, f"{path} truncated silently"
    assert bounds["rows_returned"] == requested
    assert bounds["limit"] == requested

    payload = (
        body.get("data")
        or body.get("history")
        or [row for week in body.get("weeks", []) for row in week["data"]]
    )
    assert len(payload) == requested, (
        f"{path} returned {len(payload)} rows for limit={requested}"
    )


def test_endpoints_without_a_default_window_do_not_invent_one(client, monkeypatch):
    """A truthful disclosure says no window applied, rather than fabricating one."""
    for path in (
        "/v1/indicators/history",
        "/v1/prices/history",
        "/v1/stim/history",
        "/v1/market/regime/history",
    ):
        module_name, query = BOUNDS_ENDPOINTS[path]
        engine = _install(monkeypatch, module_name, row_count=2)
        bounds = client.get(f"{path}{query}", headers=x402_headers()).json()["applied_bounds"]

        assert bounds["window_source"] == "no_default_window", path
        assert bounds["default_window_weeks"] is None, path
        assert bounds["start"] is None and bounds["end"] is None, path
        # No anchor lookup was introduced where none existed before.
        assert not any("MAX(weekdate)" in sql for sql, _ in engine.executed), (
            f"{path} gained a default anchor query it never had"
        )


def test_symbol_history_reports_caller_supplied_dates(client, monkeypatch):
    for path in ("/v1/indicators/history", "/v1/prices/history", "/v1/stim/history"):
        module_name, _ = BOUNDS_ENDPOINTS[path]
        _install(monkeypatch, module_name, row_count=2)
        bounds = client.get(
            f"{path}?symbol_exchange=IBM-N&start=2020-01-03&end=2020-12-25",
            headers=x402_headers(),
        ).json()["applied_bounds"]

        assert bounds["window_source"] == "caller_supplied", path
        assert bounds["start"] == "2020-01-03" and bounds["end"] == "2020-12-25", path


# ===========================================================================
# Finding 3 — a static illustration is not evidence
# ===========================================================================

PROOF_PATH = "/v1/ai/proof/market-edge"


def _evidence_lists() -> dict[str, list]:
    ctx, tools = ai_context(), ai_tools()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        root = client.get("/").json()

    manifest = build_x402_discovery()
    return {
        "ai_context.evidence.families": [
            endpoint
            for family in ctx["evidence"]["families"]
            for endpoint in family["inspect_at"]
        ],
        "ai_tools.what_evidence_exists":
            tools["acquisition_evaluation_guidance"]["inspect_before_deciding"]["what_evidence_exists"],
        "x402.what_evidence_exists":
            manifest["acquisition_evaluation"]["inspect_before_deciding"]["what_evidence_exists"],
        "root.evidence": root["evidence"],
    }


def test_market_edge_appears_in_no_evidence_category():
    """
    PR1's own taxonomy calls this an illustrative structure surface. Listing it
    as evidence contradicted that and would have let an agent read a synthetic
    body as a measured outcome.
    """
    offenders = {
        name: entries
        for name, entries in _evidence_lists().items()
        if any(PROOF_PATH in str(entry) for entry in entries)
    }
    assert not offenders, f"market-edge classified as evidence in: {sorted(offenders)}"


def test_evidence_category_check_has_a_positive_control():
    lists = _evidence_lists()
    planted = dict(lists)
    planted["root.evidence"] = list(planted["root.evidence"]) + [
        f"https://api.stocktrends.com{PROOF_PATH}"
    ]
    offenders = {
        name: entries for name, entries in planted.items()
        if any(PROOF_PATH in str(entry) for entry in entries)
    }
    assert offenders, "the evidence-classification check cannot detect a violation"


def test_market_edge_is_classified_as_illustrative_wherever_it_appears():
    tools = ai_tools()
    block = tools["acquisition_evaluation_guidance"]["illustrative_capability_example"]
    assert block["endpoint"] == PROOF_PATH
    for phrase in ("not empirical evidence", "not realized outcomes", "not predictive"):
        assert phrase in block["is_not"].lower()

    surface = evidence_map()["illustrative_structure_surface"]
    assert surface["endpoint"] == PROOF_PATH
    assert "not outcome or performance evidence" in surface["what_it_is_not"].lower()

    body = ai_proof_market_edge(_FakeResponse())
    assert body["agent_guidance"]["classification"] == "illustrative_capability_example"
    assert "not empirical evidence" in body["agent_guidance"]["is_not"].lower()


class _FakeResponse:
    def __init__(self):
        self.headers: dict[str, str] = {}


# ===========================================================================
# Finding 3/5 — neutrality of the surface an agent is directed to
# ===========================================================================

UNSUPPORTED_CLAIMS = (
    "proof of value",
    "proof-of-value",
    "extract signal edge",
    "signal edge",
    "before they appear in index prices",
    "identify regime shifts",
    "actionable signals",
    "delivers processed, ranked, actionable",
    "you should buy",
    "you should purchase",
    "before purchasing access",
    "adds alpha",
    "will outperform",
    "guaranteed",
)


def _market_edge_corpus() -> str:
    body = json.dumps(ai_proof_market_edge(_FakeResponse()))
    route = next(
        r for r in main.v1.routes
        if isinstance(r, APIRoute) and r.path == "/ai/proof/market-edge"
    )
    static_entry = ""
    with open("static/tools.json", encoding="utf-8") as handle:
        for tool in json.load(handle)["tools"]:
            if tool["path"] == "/ai/proof/market-edge":
                static_entry = json.dumps(tool)
    return " ".join([body, route.summary or "", route.description or "", static_entry]).lower()


def test_market_edge_body_and_title_make_no_unsupported_claim():
    """
    Acquisition guidance points agents at this endpoint, so its own wording is
    part of the neutrality surface — not exempt from it.
    """
    hits = [phrase for phrase in UNSUPPORTED_CLAIMS if phrase in _market_edge_corpus()]
    assert not hits, f"market-edge carries unsupported claims: {hits}"


def test_market_edge_neutrality_check_has_a_positive_control():
    planted = (
        "Proof of Value: Stock Trends delivers processed, ranked, actionable signals. "
        "Sector breadth identifies regime shifts before they appear in index prices. "
        "Use this workflow to extract signal edge before purchasing access."
    ).lower()
    hits = [phrase for phrase in UNSUPPORTED_CLAIMS if phrase in planted]
    assert len(hits) >= 5, f"neutrality denylist failed to fire: {hits}"


def test_market_edge_still_states_what_it_positively_is():
    """Neutral must not mean empty: the endpoint still explains its own purpose."""
    body = ai_proof_market_edge(_FakeResponse())
    assert "structure" in body["agent_guidance"]["purpose"].lower()
    assert body["market_snapshot"]["instruments"], "the illustration still shows a shape"
    assert body["value_proposition"]["differentiators"], "field composition still described"


def test_market_edge_route_is_preserved(client):
    assert client.get(PROOF_PATH).status_code == 200


# ===========================================================================
# Finding 6 — workflow truthfulness
# ===========================================================================

@pytest.fixture(scope="module")
def workflow():
    return next(
        w for w in WORKFLOW_REGISTRY if w["workflow_id"] == "historical_signal_validation"
    )


def test_regime_history_step_is_optional(workflow):
    step = next(s for s in workflow["steps"] if "market/regime/history" in s["endpoint"])
    assert step["optional"] is True, (
        "regime history cannot be mandatory while the workflow claims the researcher "
        "defines an arbitrary historical period"
    )


def test_workflow_discloses_the_regime_endpoint_coverage_limit(workflow):
    text = json.dumps(workflow).lower()
    assert "52" in text, "the 52-week ceiling is not disclosed"
    assert "most recent" in text or "recent regime history" in text
    # And it must not claim the opposite.
    assert "arbitrary historical period" not in text.replace(
        "define the historical research period appropriate to the question", ""
    )


def test_no_stim_specific_evidence_step_is_mandatory(workflow):
    for step in workflow["steps"]:
        if "stim" in step["endpoint"].lower():
            assert step["optional"] is True, f"{step['step_id']} is mandatory"


def test_indicators_history_remains_mandatory_and_discloses_its_bounds(workflow):
    step = next(s for s in workflow["steps"] if s["endpoint"].endswith("/v1/indicators/history"))
    assert step["optional"] is False
    assert any(
        "applied_bounds" in item for item in workflow["required_interpretation_steps"]
    ), "the workflow tells the caller to read applied_bounds"


# ===========================================================================
# Finding 7 — workflow pricing resolved against the real policy surface
# ===========================================================================

def _runtime_rule_by_route() -> dict[tuple[str, str], str]:
    """
    The pricing rule runtime policy governs for each exact (method, path).

    Keyed by route, not flattened to a set of ids: "this id exists somewhere"
    would pass even when a step is priced against a different endpoint's rule.
    Built from payments.policy_provider, never from the workflow under test.
    """
    config = policy_provider.get_runtime_payment_policy_config()
    return {
        (policy.method.upper(), policy.path_pattern): policy.pricing_rule_id
        for policy in config.endpoint_payment_policies
        if policy.pricing_rule_id
    }


def _runtime_pricing_rule_ids() -> set[str]:
    return set(_runtime_rule_by_route().values())


def test_every_priced_workflow_step_matches_its_routes_runtime_rule():
    """
    Each priced step must carry the rule runtime policy governs for that exact
    (method, path).

    This fails on a nonexistent rule id and equally on a real id borrowed from
    another endpoint — the second is the dangerous case, because the workflow
    would quote and reason about the wrong price while every id still resolves.
    """
    by_route = _runtime_rule_by_route()
    assert by_route, "runtime payment policy exposed no pricing rules"

    mismatches: list[str] = []
    checked = 0
    for workflow in WORKFLOW_REGISTRY:
        for step in workflow["steps"]:
            rule_id = step.get("pricing_rule_id")
            if not rule_id:
                continue
            method, path = step["endpoint"].split(" ", 1)
            expected = by_route.get((method.upper(), path))
            checked += 1
            if expected is None:
                mismatches.append(
                    f"{workflow['workflow_id']}/{step['step_id']}: "
                    f"{step['endpoint']} is not payment-governed at runtime"
                )
            elif expected != rule_id:
                mismatches.append(
                    f"{workflow['workflow_id']}/{step['step_id']}: "
                    f"{step['endpoint']} declares {rule_id!r}, runtime governs it "
                    f"with {expected!r}"
                )

    assert checked >= 15, f"only {checked} priced steps were checked"
    assert not mismatches, "workflow pricing does not match runtime policy:\n" + "\n".join(mismatches)


def test_route_exact_pricing_check_rejects_a_borrowed_but_valid_rule():
    """
    Positive control for the test above.

    `prices_history_paid` is a real, active rule — for a different endpoint. A
    check that only asked "does this id exist?" would accept it.
    """
    by_route = _runtime_rule_by_route()
    borrowed = by_route[("GET", "/v1/prices/history")]
    governed = by_route[("GET", "/v1/indicators/history")]
    assert borrowed != governed

    # It is a genuinely valid id...
    assert borrowed in _runtime_pricing_rule_ids()
    # ...and still wrong for this route, which is what the route-exact check sees.
    assert by_route[("GET", "/v1/indicators/history")] != borrowed


def test_workflow_costs_resolve_from_an_independently_built_runtime_map(monkeypatch):
    """
    The cost map is built from the payment policy, not from the workflow.

    A workflow step naming a rule the policy does not carry therefore raises the
    registry-integrity error rather than silently resolving.
    """
    import routers.workflows as workflows_router

    cost_map = {rule_id: 0.10 for rule_id in _runtime_pricing_rule_ids()}
    monkeypatch.setattr(workflows_router, "_fetch_active_pricing_costs", lambda: cost_map)

    body = json.loads(workflows_router.get_workflows().body)
    entry = next(
        w for w in body["workflows"] if w["workflow_id"] == "historical_signal_validation"
    )
    priced_steps = [s for s in entry["steps"] if s["pricing_rule_id"]]
    assert priced_steps
    assert entry["total_stc_cost"] == pytest.approx(0.10 * len(priced_steps))


def test_registry_integrity_fails_when_a_rule_leaves_the_runtime_policy(monkeypatch):
    """Positive control for the test above."""
    from fastapi import HTTPException

    import routers.workflows as workflows_router

    partial = {
        rule_id: 0.10
        for rule_id in _runtime_pricing_rule_ids()
        if rule_id != "indicators_history_paid"
    }
    monkeypatch.setattr(workflows_router, "_fetch_active_pricing_costs", lambda: partial)

    with pytest.raises(HTTPException) as excinfo:
        workflows_router.get_workflows()
    assert excinfo.value.status_code == 500
    assert "Registry integrity error" in str(excinfo.value.detail)

# ===========================================================================
# Re-review finding 1 — stale persuasion in *discovery*, not just the route body
# ===========================================================================

# The route body was neutralized in the previous pass while the surfaces that
# describe it were not, so an agent reading /v1/ai/tools still met "Proof of
# Value" and "highest immediate value". These phrases are checked across the
# whole live manifest and both static artifacts.
DISCOVERY_PERSUASION = (
    "proof of value",
    "proof-of-value",
    "value proposition",
    "agent workflow value",
    "highest immediate value",
    "highest value",
    "before purchasing access",
    "conversion prompt",
    "extract signal edge",
    "signal edge",
    "actionable signals",
)


def _static_tools_text() -> str:
    with open("static/tools.json", encoding="utf-8") as handle:
        return handle.read()


def _static_llms_text() -> str:
    with open("static/llms.txt", encoding="utf-8") as handle:
        return handle.read()


def test_complete_live_ai_tools_response_carries_no_persuasion():
    """
    The whole manifest, not the market-edge tool entry alone.

    Covers the tool title and description, recommended_first_call, the retained
    conversion-path field, top-level notes and onboarding guidance in one sweep,
    because that is how an agent actually reads it.
    """
    corpus = json.dumps(ai_tools()).lower()
    hits = [phrase for phrase in DISCOVERY_PERSUASION if phrase in corpus]
    assert not hits, f"/v1/ai/tools carries persuasive language: {hits}"


def test_live_ai_context_carries_no_persuasion():
    corpus = json.dumps(ai_context()).lower()
    hits = [phrase for phrase in DISCOVERY_PERSUASION if phrase in corpus]
    assert not hits, f"/v1/ai/context carries persuasive language: {hits}"


@pytest.mark.parametrize(
    "name,loader", [("static/tools.json", _static_tools_text), ("static/llms.txt", _static_llms_text)]
)
def test_static_artifacts_carry_no_persuasion(name, loader):
    corpus = loader().lower()
    hits = [phrase for phrase in DISCOVERY_PERSUASION if phrase in corpus]
    assert not hits, f"{name} carries persuasive language: {hits}"


def test_discovery_persuasion_check_has_a_positive_control():
    planted = (
        "Proof of Value - Market Edge. Understand the value proposition and agent "
        "workflow value before purchasing access; returns the highest immediate value "
        "actionable signals."
    ).lower()
    hits = [phrase for phrase in DISCOVERY_PERSUASION if phrase in planted]
    assert len(hits) >= 5, f"discovery denylist failed to fire: {hits}"


def test_market_edge_tool_entry_is_described_as_an_illustration():
    """Neutral must still be informative: the entry says what it does show."""
    tool = next(t for t in ai_tools()["tools"] if t["endpoint"] == PROOF_PATH)
    assert tool["classification"] == "illustrative_capability_example"
    description = tool["description"].lower()
    assert "schema" in description or "structure" in description
    for phrase in ("not empirical evidence", "not realized outcomes", "not predictive"):
        assert phrase in description


def test_recommended_first_call_is_framed_procedurally():
    """
    A first call may be recommended for what it does, never for being worth most.
    """
    rfc = ai_tools()["recommended_first_call"]
    reason = rfc["reason"].lower()
    for phrase in ("highest", "best value", "most valuable", "greatest"):
        assert phrase not in reason, f"recommended_first_call ranks by worth: {phrase!r}"
    # It says what the endpoint returns, and defers suitability to the caller.
    assert "returns" in reason
    assert "for the caller to determine" in reason
    # And it names the public discovery surfaces that come first.
    assert "/.well-known/x402" in rfc["read_first"]


def test_conversion_path_field_is_retained_but_neutral():
    """
    The key is pinned by an existing contract; its value must not be a funnel.
    """
    acp = ai_tools()["agent_conversion_path"]
    assert acp["proof_endpoint"] == PROOF_PATH, "pinned compatibility key was dropped"
    assert acp["content_type"] == "access_and_discovery_mechanics"
    described = acp["schema_illustration_description"].lower()
    assert "illustration, not evidence" in described


# ===========================================================================
# Re-review finding 2 — the static fallback must state the real regime contract
# ===========================================================================

def _static_tool(path: str) -> dict:
    with open("static/tools.json", encoding="utf-8") as handle:
        for tool in json.load(handle)["tools"]:
            if tool["path"] == path:
                return tool
    raise AssertionError(f"{path} missing from static/tools.json")


def test_static_market_regime_entry_requires_the_real_parameters():
    """
    Required, not checked-if-present.

    An agent that can only reach the static fallback has to learn the same
    contract the runtime enforces, or it will construct a request the endpoint
    silently ignores.
    """
    tool = _static_tool("/market/regime/history")
    params = {p["name"]: p for p in tool.get("parameters", [])}

    assert "start_date" in params, "static entry omits start_date"
    assert "start" not in params, "static entry carries the stale `start` name"

    assert params["limit"]["default"] == 12
    assert params["limit"]["maximum"] == 52
    assert params["start_date"]["type"] == "string"


def test_static_market_regime_entry_discloses_its_real_semantics():
    tool = _static_tool("/market/regime/history")
    semantics = tool["coverage_semantics"]

    assert semantics["max_observations"] == 52
    assert semantics["ordering"] == "most_recent_eligible_weeks_first"
    assert semantics["has_end_bound"] is False
    assert semantics["supports_backward_pagination"] is False
    assert "earliest eligible" in semantics["start_date_meaning"]

    description = tool["description"].lower()
    assert "at most 52" in description
    assert "arbitrary historical period" in description


def test_static_regime_contract_matches_the_runtime_bounds_table():
    """Independently derived: static file text vs the runtime bounds table."""
    tool = _static_tool("/market/regime/history")
    params = {p["name"]: p for p in tool["parameters"]}
    bounds = HISTORY_ENDPOINT_BOUNDS["/v1/market/regime/history"]

    assert params["limit"]["default"] == bounds["default_limit"]
    assert params["limit"]["maximum"] == bounds["max_limit"]


def test_static_llms_describes_regime_coverage_and_researcher_responsibility():
    text = _static_llms_text()

    regime_line = next(
        line for line in text.splitlines() if "Recent weekly market regime" in line
    )
    assert "at most 52" in regime_line
    assert "most recent eligible" in regime_line
    assert "not an arbitrary historical period" in regime_line

    workflow_prose = next(
        line for line in text.splitlines() if "historical_signal_validation" in line
    )
    assert "regime-history step is optional" in workflow_prose
    assert "52 recent eligible weeks" in workflow_prose
    assert "researcher-supplied" in workflow_prose


# ===========================================================================
# Re-review finding 3 — the full 402 preview must publish applied_bounds
# ===========================================================================

REMEDIATED_HISTORY_ENDPOINTS = (
    "/v1/indicators/history",
    "/v1/prices/history",
    "/v1/stim/history",
    "/v1/market/regime/history",
)


@pytest.mark.parametrize("path", sorted(HISTORY_ENDPOINT_BOUNDS))
def test_full_preview_publishes_every_applied_bounds_field_the_runtime_returns(
    path, client, monkeypatch
):
    """
    Two independently derived sources: the published response_shape, and the keys
    the endpoint actually emits when called.

    A paying client reads the preview to learn what it will receive. Telling it to
    check applied_bounds while not publishing the structure leaves the disclosure
    undiscoverable.
    """
    module_name, query = BOUNDS_ENDPOINTS[path]
    _install(monkeypatch, module_name, row_count=2)
    runtime_keys = set(
        client.get(f"{path}{query}", headers=x402_headers()).json()["applied_bounds"]
    )

    preview = build_endpoint_preview(path)
    published = {
        field.split(".", 1)[1]
        for field in preview["response_shape"]
        if field.startswith("applied_bounds.")
    }

    assert runtime_keys, f"{path} returned no applied_bounds"
    missing = runtime_keys - published
    assert not missing, f"{path} returns {sorted(missing)} that the 402 preview never publishes"
    invented = published - runtime_keys
    assert not invented, f"{path} preview advertises {sorted(invented)} that runtime never returns"


@pytest.mark.parametrize("path", REMEDIATED_HISTORY_ENDPOINTS)
def test_remediated_endpoints_publish_the_full_bounds_structure(path):
    preview = build_endpoint_preview(path)
    published = {f for f in preview["response_shape"] if f.startswith("applied_bounds.")}
    for field in (
        "applied_bounds.limit",
        "applied_bounds.limit_source",
        "applied_bounds.max_limit",
        "applied_bounds.rows_returned",
        "applied_bounds.truncated_by_limit",
        "applied_bounds.window_source",
    ):
        assert field in published, f"{path} preview omits {field}"
