"""
Governance for the pre-payment semantic validation layer.

PR 2 established *where* paid work begins: FastAPI route matching, body parsing
and Pydantic/query/path validation all complete before the payment gate.  PR 3
inserts the remaining deterministic, request-only validation immediately in
front of that gate, producing the full required ordering:

    auth / classification / pricing
      -> FastAPI route matching
      -> JSON/body parsing
      -> Pydantic/query/path validation
      -> request-only semantic validation
      -> deferred payment gate
      -> x402 settlement / MPP authorization
      -> endpoint/service execution
      -> finalization/accounting

Two distinct things are guarded here:

1. The seam itself — a registered validator runs, runs before the gate, and its
   rejection propagates with the gate never invoked.
2. The audit — every payment-governed route has an explicit, recorded
   disposition, so a newly priced route cannot enter the money path without
   somebody deciding whether it needs semantic validation.

`tests/test_settlement_ordering.py` proves the economics end to end; this file
proves the wiring those economics depend on.
"""

from __future__ import annotations

import ast
import inspect
from contextlib import contextmanager
from pathlib import Path

# Imported at module scope on purpose: `from __future__ import annotations`
# defers annotations to strings, and FastAPI resolves a probe endpoint's
# annotations against this module's globals.  A function-local `Request` import
# is invisible there, and the parameter would be misread as a query field.
from fastapi import Request

from api.routing import (
    SEMANTIC_VALIDATOR_ATTR,
    get_pre_payment_semantic_validator,
    install_payment_execution_boundary,
    is_payment_wrapped,
    pre_payment_semantic_validator,
)
from support.payment_harness import (
    mpp_headers,
    payment_governed_routes,
    unpaid_headers,
    v1_path,
    x402_headers,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ===========================================================================
# 40 — a registered validator runs, and runs before the gate
#
# Asserted on a synthetic route rather than a production one so the claim is
# about the seam, not about any particular endpoint's validation rules.
# `/v1/stim/*` is a prefix enforcement scope, so a route registered there is
# payment-governed by policy without a bespoke registration.
# ===========================================================================

_PROBE_PATH = "/stim/_semantic_probe"
_PROBE_PAYLOAD_MARKER = "paid-payload-that-must-never-reach-a-rejected-caller"


@contextmanager
def temporary_semantic_route(*, reject: bool):
    """
    Register a payment-governed probe route carrying a semantic validator.

    Records what ran, in order, so "before the gate" is measured rather than
    inferred from a status code.  The route is withdrawn afterwards, along with
    any OpenAPI schema generated while it existed.
    """
    import main
    from fastapi import HTTPException, Query

    calls: list[str] = []
    original_routes = list(main.v1.routes)
    original_schema = main.v1.openapi_schema

    def probe_validator(request, values):
        calls.append("validator")
        assert request is not None, "the validator was called without a Request"
        if reject:
            raise HTTPException(
                status_code=400,
                detail={
                    "request_id": getattr(request.state, "request_id", None),
                    "error": "probe_semantic_rejection",
                    "value": values.get("q"),
                },
            )

    try:
        @main.v1.get(_PROBE_PATH)
        @pre_payment_semantic_validator(probe_validator)
        def probe_endpoint(q: str = Query(default="ok")):
            calls.append("endpoint")
            return {"marker": _PROBE_PAYLOAD_MARKER}

        install_payment_execution_boundary(main.v1)
        yield calls
    finally:
        main.v1.router.routes[:] = original_routes
        main.v1.openapi_schema = original_schema


def test_40_rejecting_validator_runs_and_the_gate_never_does(payment_harness):
    """
    The core claim of PR 3.

    A semantic rejection must cost nothing on any rail: no facilitator verify,
    no settle, no MPP authorization, and no endpoint execution.
    """
    with temporary_semantic_route(reject=True) as calls:
        response = payment_harness.client.get(
            f"/v1{_PROBE_PATH}", headers=x402_headers()
        )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "probe_semantic_rejection"
    assert _PROBE_PAYLOAD_MARKER not in response.text

    assert calls == ["validator"], (
        f"expected the validator alone to run, got {calls}"
    )
    assert payment_harness.verify_count == 0
    assert payment_harness.settle_count == 0
    assert payment_harness.mpp.authorize_count == 0
    assert payment_harness.mpp.capture_count == 0
    assert payment_harness.mpp.void_count == 0
    assert payment_harness.logs.only_economics_row()["billed_amount_usd"] == 0


def test_40b_rejecting_validator_costs_nothing_on_mpp(payment_harness):
    """The same request on the MPP rail: no control-plane round trip at all."""
    with temporary_semantic_route(reject=True) as calls:
        response = payment_harness.client.get(
            f"/v1{_PROBE_PATH}", headers=mpp_headers()
        )

    assert response.status_code == 400
    assert calls == ["validator"]
    assert payment_harness.mpp.authorize_count == 0
    assert payment_harness.mpp.capture_count == 0
    assert payment_harness.mpp.void_count == 0


def test_40c_rejecting_validator_answers_before_the_challenge(payment_harness):
    """
    An unpaid request that could never be served is told why, not challenged.

    The same precedence `test_08` asserts for prices, at the seam itself: there
    is no point quoting a price for a request the endpoint would refuse.
    """
    with temporary_semantic_route(reject=True) as calls:
        response = payment_harness.client.get(
            f"/v1{_PROBE_PATH}", headers=unpaid_headers()
        )

    assert response.status_code == 400
    assert "payment-required" not in response.headers
    assert calls == ["validator"]


def test_41_valid_semantic_input_settles_exactly_once(payment_harness):
    """
    The positive control.

    Without it, test_40 would pass just as well if the seam rejected everything.
    Validator, gate and endpoint each run once, in that order, and the rail
    settles once.
    """
    with temporary_semantic_route(reject=False) as calls:
        response = payment_harness.client.get(
            f"/v1{_PROBE_PATH}", headers=x402_headers()
        )

    assert response.status_code == 200
    assert response.json()["marker"] == _PROBE_PAYLOAD_MARKER
    assert calls == ["validator", "endpoint"], (
        f"expected the validator to run before the endpoint, got {calls}"
    )
    assert payment_harness.verify_count == 1
    assert payment_harness.settle_count == 1
    assert payment_harness.logs.only_economics_row()["payment_status"] == "settled"


def test_41b_valid_semantic_input_authorizes_and_captures_once_on_mpp(payment_harness):
    with temporary_semantic_route(reject=False) as calls:
        response = payment_harness.client.get(
            f"/v1{_PROBE_PATH}", headers=mpp_headers()
        )

    assert response.status_code == 200
    assert calls == ["validator", "endpoint"]
    assert payment_harness.mpp.authorize_count == 1
    assert payment_harness.mpp.capture_count == 1
    assert payment_harness.mpp.void_count == 0


def test_42_validator_exception_keeps_its_status_and_detail(payment_harness):
    """
    A validator's `HTTPException` propagates through FastAPI unchanged.

    This is what lets validation move earlier without redesigning any error
    contract: the endpoint's own exception is raised, from a different place.
    A 422 is used here specifically because it is the status the decision
    endpoint's `missing_symbol` case carries — proof the seam does not flatten
    everything to 400.
    """
    import main
    from fastapi import HTTPException

    original_routes = list(main.v1.routes)
    original_schema = main.v1.openapi_schema
    endpoint_runs: list[str] = []

    def raising_validator(request, values):
        raise HTTPException(
            status_code=422,
            detail={"error": "probe_unprocessable", "hint": "structured detail"},
        )

    try:
        @main.v1.get("/stim/_semantic_status_probe")
        @pre_payment_semantic_validator(raising_validator)
        def probe_endpoint():
            endpoint_runs.append("endpoint")
            return {"marker": _PROBE_PAYLOAD_MARKER}

        install_payment_execution_boundary(main.v1)

        response = payment_harness.client.get(
            "/v1/stim/_semantic_status_probe", headers=x402_headers()
        )
    finally:
        main.v1.router.routes[:] = original_routes
        main.v1.openapi_schema = original_schema

    assert response.status_code == 422, (
        "the validator's status was not preserved; a moved validation would "
        "silently change its public contract"
    )
    assert response.json()["detail"] == {
        "error": "probe_unprocessable",
        "hint": "structured detail",
    }, "the structured detail body was not preserved"
    assert not endpoint_runs
    assert payment_harness.verify_count == 0
    assert payment_harness.settle_count == 0


# ===========================================================================
# 43 — the seam's mechanics
# ===========================================================================

def test_43_marker_is_readable_through_the_installed_wrapper():
    """
    Registration survives wrapping, so the audit below can read it off a route.

    `functools.wraps` copies the wrapped function's `__dict__`, which is what
    carries the marker.  A wrapper built without it would leave every route
    looking unregistered — and the audit would pass vacuously.
    """
    from fastapi import FastAPI

    def validator(request, values):
        return None

    app = FastAPI()

    @app.get("/stim/probe")
    @pre_payment_semantic_validator(validator)
    def probe():
        return {"paid": True}

    assert install_payment_execution_boundary(app) == 1
    route = app.routes[-1]

    assert is_payment_wrapped(route)
    assert get_pre_payment_semantic_validator(route) is validator, (
        "the registered validator is not readable through the installed wrapper"
    )
    assert getattr(route.dependant.call, SEMANTIC_VALIDATOR_ATTR, None) is validator


def test_43b_routes_without_a_validator_report_none():
    """The negative half, so the audit's set B is a real measurement."""
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/stim/probe")
    def probe():
        return {"paid": True}

    install_payment_execution_boundary(app)
    assert get_pre_payment_semantic_validator(app.routes[-1]) is None


def test_43c_seam_never_solves_dependencies_a_second_time():
    """
    PR 2's invariant, re-asserted now that a second step shares the seam.

    The semantic validator receives the values FastAPI already solved.  It must
    not trigger a second dependency solve, which would run sub-dependencies
    twice and could double any work they do.
    """
    import api.routing as routing_module

    # Asserted on call nodes rather than on the text: the module explains its
    # relationship to solve_dependencies at length in comments and docstrings,
    # and a substring scan would flag that prose rather than a real invocation.
    tree = ast.parse(inspect.getsource(routing_module))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "solve_dependencies" not in called, (
        "api/routing.py now calls solve_dependencies; the seam would solve "
        "dependencies a second time, running every sub-dependency twice"
    )


def test_43d_validator_does_not_change_endpoint_coroutine_kind():
    """
    A registered validator must not turn a sync endpoint async, or vice versa.

    `get_request_handler` derives threadpool-vs-await from
    `iscoroutinefunction(dependant.call)`, so a wrapper of the wrong kind would
    silently change execution semantics.
    """
    import asyncio

    from fastapi import FastAPI

    def validator(request, values):
        return None

    app = FastAPI()

    @app.get("/stim/sync")
    @pre_payment_semantic_validator(validator)
    def sync_probe():
        return {"kind": "sync"}

    @app.get("/stim/async")
    @pre_payment_semantic_validator(validator)
    async def async_probe():
        return {"kind": "async"}

    install_payment_execution_boundary(app)

    kinds = {
        route.path: (
            asyncio.iscoroutinefunction(route.endpoint),
            asyncio.iscoroutinefunction(route.dependant.call),
        )
        for route in app.routes
        if getattr(route, "path", None) in {"/stim/sync", "/stim/async"}
    }

    assert kinds["/stim/sync"] == (False, False)
    assert kinds["/stim/async"] == (True, True)


# ===========================================================================
# 44 — the audited inventory
#
# Set A and set B below are a review artifact, not a registry: nothing in
# production reads them.  Their job is to make a newly priced route fail this
# test until somebody records which class its rejections fall into.
#
# Class 1 — knowable from the already-solved request values alone.  Must have a
#           registered pre-payment semantic validator.
# Class 2 — requires querying or executing paid work.  Must stay after the gate.
# Class 3 — both; only the class 1 portion moves forward.
# ===========================================================================

# Governed routes whose rejections include a class 1 portion.  The note records
# what moved ahead of payment and what deliberately did not, so a reviewer can
# check the split without re-reading each endpoint.
SEMANTIC_VALIDATION_REQUIRED: dict[tuple[str, str], str] = {
    ("GET", "/v1/agent/screener/top"):
        "class 3 — sort / exchange / trend-code vocabulary moved; latest weekdate "
        "and whether any signal rows match stay post-payment",
    ("GET", "/v1/breadth/sector/history"): "class 1 — optional exchange code",
    ("GET", "/v1/breadth/sector/latest"):
        "class 3 — optional exchange code moved; latest weekdate stays post-payment",
    ("POST", "/v1/decision/evaluate-symbol"):
        "class 3 — resolvable instrument identity moved; weekdate availability, "
        "symbol_not_found and regime computation stay post-payment",
    ("GET", "/v1/indicators/history"): "class 1 — symbol/exchange identity",
    ("GET", "/v1/indicators/latest"): "class 1 — symbol/exchange identity",
    ("GET", "/v1/intelligence/guidance/{artifact_id}"):
        "class 3 — artifact_id shape moved; store availability and whether the "
        "artifact exists stay post-payment",
    ("GET", "/v1/intelligence/research/{artifact_id}"):
        "class 3 — artifact_id shape moved; store availability and whether the "
        "artifact exists stay post-payment",
    ("GET", "/v1/leadership/rotation/history"): "class 1 — optional exchange code",
    ("GET", "/v1/leadership/summary/latest"):
        "class 3 — optional exchange code moved; latest weekdate stays post-payment",
    ("POST", "/v1/portfolio/compare"):
        "class 3 — both sides' position lists moved; per-symbol existence stays "
        "post-payment",
    ("POST", "/v1/portfolio/construct"):
        "class 3 — universe / bias / exchange moved; weekdates, regime, candidate "
        "availability and insufficient_candidates stay post-payment",
    ("POST", "/v1/portfolio/evaluate"):
        "class 3 — position list moved; per-symbol existence stays post-payment",
    ("GET", "/v1/prices/history"):
        "class 3 — instrument identity moved; price_not_found stays post-payment",
    ("GET", "/v1/prices/latest"):
        "class 3 — instrument identity moved; price_not_found stays post-payment",
    ("GET", "/v1/selections/history"):
        "class 1 — composite identifier shape and exchange code",
    ("GET", "/v1/selections/latest"):
        "class 3 — optional exchange code moved; latest weekdate stays post-payment",
    ("GET", "/v1/selections/published/history"):
        "class 1 — composite identifier shape and exchange code",
    ("GET", "/v1/selections/published/latest"):
        "class 3 — optional exchange code moved; latest weekdate stays post-payment",
    ("GET", "/v1/stim/history"):
        "class 3 — instrument identity moved; ST-IM availability and gap "
        "computation stay post-payment",
    ("GET", "/v1/stim/latest"):
        "class 3 — instrument identity moved; stim_not_found and staleness stay "
        "post-payment",
    ("GET", "/v1/stwr/reports/history"): "class 1 — report code and exchange code",
    ("GET", "/v1/stwr/reports/latest"):
        "class 3 — report code and exchange code moved; latest report week stays "
        "post-payment",
}

# Governed routes audited as having no request-only rejection to move.  Each
# records why, because "no validator" and "nobody looked" are indistinguishable
# from the outside.
NO_SEMANTIC_VALIDATION_REQUIRED: dict[tuple[str, str], str] = {
    ("GET", "/v1/intelligence/guidance/latest"):
        "class 2 — takes no parameters; store availability and whether a "
        "published artifact exists are both discovered by the paid lookup",
    ("GET", "/v1/intelligence/research/latest"):
        "class 2 — takes no parameters; store availability and whether a "
        "published artifact exists are both discovered by the paid lookup",
    ("GET", "/v1/market/regime/forecast"):
        "class 2 — only `lookback`, fully constrained by its Query bounds, which "
        "FastAPI already rejects with a 422 ahead of the gate",
    ("GET", "/v1/market/regime/history"):
        "class 2 — only `limit` and `start_date`, both fully constrained by their "
        "Query and type declarations",
    ("GET", "/v1/market/regime/latest"):
        "class 2 — takes no parameters beyond the request itself",
}


def _governed_surface() -> set[tuple[str, str]]:
    """The payment-governed route/method surface, derived from runtime policy."""
    return {(method, v1_path(route)) for route, method in payment_governed_routes()}


def test_44_audit_covers_the_governed_surface_exactly():
    """
    A union B == the governed surface, and A intersect B == the empty set.

    This is the test that makes pricing a new endpoint force a semantic audit.
    A route that becomes payment-governed lands in neither set and fails here,
    so it cannot enter the money path on the strength of a pricing rule alone.
    """
    surface = _governed_surface()
    assert surface, "no payment-governed routes discovered; the audit is inert"

    set_a = set(SEMANTIC_VALIDATION_REQUIRED)
    set_b = set(NO_SEMANTIC_VALIDATION_REQUIRED)

    overlap = set_a & set_b
    assert not overlap, (
        "routes audited as both requiring and not requiring semantic "
        f"validation: {sorted(overlap)}"
    )

    unaudited = surface - (set_a | set_b)
    assert not unaudited, (
        "payment-governed route(s) have no recorded semantic-audit disposition. "
        "Decide whether each rejection is knowable from the request alone "
        "(class 1 -> register a validator, add to SEMANTIC_VALIDATION_REQUIRED) "
        "or requires paid execution (class 2 -> add to "
        "NO_SEMANTIC_VALIDATION_REQUIRED with a reason):\n  "
        + "\n  ".join(f"{m} {p}" for m, p in sorted(unaudited))
    )

    stale = (set_a | set_b) - surface
    assert not stale, (
        "audit entries no longer match a payment-governed route; a stale entry "
        "hides drift:\n  " + "\n  ".join(f"{m} {p}" for m, p in sorted(stale))
    )


def test_44b_every_class_1_route_has_a_registered_validator():
    """Set A is a claim about production wiring, so it is checked against it."""
    missing = []
    for route, method in payment_governed_routes():
        key = (method, v1_path(route))
        if key not in SEMANTIC_VALIDATION_REQUIRED:
            continue
        if get_pre_payment_semantic_validator(route) is None:
            missing.append(key)

    assert not missing, (
        "route(s) audited as having request-only validation carry no registered "
        "pre-payment semantic validator, so those rejections still happen after "
        "payment:\n  " + "\n  ".join(f"{m} {p}" for m, p in sorted(missing))
    )


def test_44c_class_2_routes_carry_no_validator():
    """
    The complement.  A validator on a set B route means the audit is wrong —
    either the route does have request-only validation, or something
    data-dependent was dragged in front of the gate.
    """
    unexpected = []
    for route, method in payment_governed_routes():
        key = (method, v1_path(route))
        if key not in NO_SEMANTIC_VALIDATION_REQUIRED:
            continue
        if get_pre_payment_semantic_validator(route) is not None:
            unexpected.append(key)

    assert not unexpected, (
        "route(s) audited as having no request-only validation carry a "
        f"pre-payment validator: {sorted(unexpected)}"
    )


def test_44d_audit_entries_record_a_classification_and_reason():
    """An entry with no rationale is a checkbox, not an audit."""
    for source, name in (
        (SEMANTIC_VALIDATION_REQUIRED, "SEMANTIC_VALIDATION_REQUIRED"),
        (NO_SEMANTIC_VALIDATION_REQUIRED, "NO_SEMANTIC_VALIDATION_REQUIRED"),
    ):
        for key, note in source.items():
            assert note and note.strip().startswith("class "), (
                f"{name}[{key}] must record the classification and rationale, "
                f"got {note!r}"
            )


def test_44e_registered_validators_do_no_database_or_service_work():
    """
    Semantic validation must be pure.

    A validator runs before payment on every probe of a paid endpoint, so a DB
    read or service call placed there would be unpaid work on requests that
    never settle — and would move a class 2 rejection in front of the gate.
    Checked structurally over each distinct validator's source.
    """
    forbidden = (
        "get_engine",
        "engine.connect",
        "conn.execute",
        "_service.",
        "requests.get",
        "urlopen",
        "httpx",
    )

    validators = {}
    for route, _method in payment_governed_routes():
        validator = get_pre_payment_semantic_validator(route)
        if validator is not None:
            validators[f"{validator.__module__}.{validator.__qualname__}"] = validator

    assert validators, "no registered validators found; this guard would be inert"

    violations = []
    for name, validator in sorted(validators.items()):
        source = inspect.getsource(validator)
        for token in forbidden:
            if token in source:
                violations.append(f"{name} references {token!r}")

    assert not violations, (
        "pre-payment semantic validator(s) appear to do data or service work, "
        "which would execute unpaid before the gate:\n  " + "\n  ".join(violations)
    )


def test_44f_registered_validators_are_synchronous():
    """An async validator would not be awaited by the sync wrapper branch."""
    import asyncio

    for route, method in payment_governed_routes():
        validator = get_pre_payment_semantic_validator(route)
        if validator is None:
            continue
        assert not asyncio.iscoroutinefunction(validator), (
            f"{method} {v1_path(route)} registered an async semantic validator; "
            "the seam invokes validators synchronously"
        )


# ===========================================================================
# 45 — PR 2 hardening pinned into this release boundary
# ===========================================================================

def test_45_main_invokes_the_startup_boundary_assertion():
    """
    The startup assertion is wired in production, not merely tested.

    `assert_payment_boundary_complete` has its own unit coverage, so deleting
    the *call* from `main.py` left the suite green while the running application
    lost its refusal to start on an unwrapped route.  Parsed from the source so
    the call site itself is the thing asserted.
    """
    tree = ast.parse((_REPO_ROOT / "main.py").read_text(encoding="utf-8"))

    install_line = None
    assert_line = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        first_arg = node.args[0] if node.args else None
        if not (isinstance(first_arg, ast.Name) and first_arg.id == "v1"):
            continue
        if node.func.id == "install_payment_execution_boundary":
            install_line = node.lineno
        elif node.func.id == "assert_payment_boundary_complete":
            assert_line = node.lineno

    assert install_line is not None, (
        "main.py no longer calls install_payment_execution_boundary(v1); the "
        "payment execution boundary is not installed"
    )
    assert assert_line is not None, (
        "main.py no longer calls assert_payment_boundary_complete(v1); an "
        "unwrapped payment-governed route could reach the surface and serve paid "
        "work with no gate, and nothing would refuse to start"
    )
    assert assert_line > install_line, (
        "assert_payment_boundary_complete runs before the boundary is installed, "
        "so it would assert against an unwrapped surface"
    )


def test_45b_startup_assertion_enumerates_rather_than_counts():
    """
    Universal enumeration is the invariant; the minimum count is only
    non-vacuity protection.

    A guard rewritten as "at least N routes are wrapped" would pass with an
    unwrapped route present, so the enumeration must remain the actual check.
    """
    from api.routing import assert_payment_boundary_complete, unwrapped_api_routes

    source = inspect.getsource(assert_payment_boundary_complete)
    assert "unwrapped_api_routes(" in source, (
        "the startup assertion no longer enumerates unwrapped routes; a count "
        "alone cannot establish universal coverage"
    )
    assert inspect.isfunction(unwrapped_api_routes)


def test_45c_fastapi_writes_the_matched_route_into_the_request_scope():
    """
    Pin the `scope["route"]` coupling the runtime fail-closed backstop relies on.

    The metering finaliser reads `request.scope["route"]` to tell an unwrapped
    payment-governed route from a route miss.  If FastAPI stopped writing the
    matched `APIRoute` into the child scope, that backstop would read `None` on
    every request and silently never fire again.

    Verified behaviourally, through a real request, rather than by re-deriving
    route matching here.  FastAPI 0.116.1 / Starlette 0.47.3: `APIRoute.matches`
    sets `child_scope["route"] = self` and the router merges the child scope.
    """
    from fastapi import FastAPI
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient

    seen: dict[str, object] = {}

    app = FastAPI()

    @app.get("/coupling-probe")
    def probe(request: Request):
        seen["route"] = request.scope.get("route")
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/coupling-probe").status_code == 200

    matched = seen.get("route")
    assert matched is not None, (
        'FastAPI no longer writes the matched route into request.scope["route"]; '
        "the metering execution-boundary backstop cannot detect an unwrapped "
        "payment-governed route and must be re-verified before it is trusted"
    )
    assert isinstance(matched, APIRoute)
    assert matched.path == "/coupling-probe", (
        f'scope["route"] carried {matched!r}, not the matched APIRoute'
    )

    # And the backstop's own predicate reads correctly off what the scope carries.
    assert is_payment_wrapped(matched) is False
    install_payment_execution_boundary(app)
    assert is_payment_wrapped(seen["route"]) is True, (
        "the route object in the scope is not the same object the installer "
        "wrapped; the backstop would misreport coverage"
    )


def test_45d_metering_backstop_still_reads_the_matched_route():
    """The other half of the coupling: the reader has not moved either."""
    import middleware.metering as metering_module

    source = inspect.getsource(metering_module.MeteringMiddleware.dispatch)
    assert 'request.scope.get("route")' in source, (
        "the metering finaliser no longer reads the matched route from the "
        "request scope; test_45c would pin a framework behaviour nothing consumes"
    )
    assert "is_payment_wrapped(" in source, (
        "the finaliser no longer tests the matched route for the payment "
        "wrapper; the runtime fail-closed backstop is gone"
    )
