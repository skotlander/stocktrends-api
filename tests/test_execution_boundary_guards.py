"""
Structural guards for the payment execution boundary.

These do not exercise requests.  They assert properties of the wiring that the
settlement-ordering invariant depends on, so that a future change breaks a test
rather than production economics.

Three things are guarded:

1. Coverage — every v1 route carries the payment-aware endpoint wrapper.
2. Purity  — no payment-governed route runs a dependency before the gate.
3. Coupling — the FastAPI internals the seam is built on still behave as
   verified against 0.116.1.
"""

from __future__ import annotations

import asyncio
import inspect

from fastapi.routing import APIRoute

from support.payment_harness import (
    assert_facilitator_bindings_intact,
    assert_mpp_bindings_intact,
    payment_governed_routes,
    v1_api_routes,
    v1_path,
)


# ---------------------------------------------------------------------------
# 0 — spy integrity
#
# These run unmarked and standalone.  Both guards are also invoked from their
# respective fixtures, but a fixture-raised failure inside an xfail-marked test
# is swallowed as an expected failure: xfail masks setup errors, not just
# assertion errors.  While strict xfails remain in the acceptance suite (the
# semantic cases deferred to PR 3), a broken attach point could therefore leave
# the suite green while real settlements went unobserved.  Calling the guards
# directly from unmarked tests removes that dependence on collateral failures
# elsewhere.
# ---------------------------------------------------------------------------

def test_facilitator_spy_attach_points_are_intact():
    """
    The acceptance suite's central claim — "settle was not called" — is only
    meaningful if the spy is patching the binding the production enforcement
    path actually resolves.
    """
    assert_facilitator_bindings_intact()


def test_mpp_spy_attach_points_are_intact():
    """
    The MPP equivalent.  Guards against a calling module promoting the
    control-plane imports to module level, which would bind them at import time
    and make the harness's patches invisible.
    """
    assert_mpp_bindings_intact()


# ---------------------------------------------------------------------------
# 21 — coverage
# ---------------------------------------------------------------------------

def test_v1_exposes_api_routes_to_guard():
    """Sanity: the guards below are meaningless if the route list is empty."""
    routes = v1_api_routes()
    assert len(routes) > 20, f"expected the full v1 surface, found {len(routes)}"


def test_21_every_v1_api_route_carries_the_payment_wrapper():
    """
    The wrapper is installed on every v1 APIRoute, not only on routes currently
    selected by an exact EndpointPaymentPolicy.

    Payment enforcement can also arise from prefix and runtime policy, so the
    seam must not fail open merely because a route becomes paid through
    configuration without appearing in a static registration set.  The wrapper
    is inert when no deferred gate is present on request.state.
    """
    from api.routing import PAYMENT_WRAPPER_ATTR, is_payment_wrapped

    unwrapped = [
        f"{sorted(route.methods or set())} {v1_path(route)}"
        for route in v1_api_routes()
        if not is_payment_wrapped(route)
    ]

    assert not unwrapped, (
        f"{len(unwrapped)} v1 route(s) are missing the payment wrapper "
        f"(marker attribute {PAYMENT_WRAPPER_ATTR!r}):\n  "
        + "\n  ".join(sorted(unwrapped))
    )


def test_21b_wrapper_preserves_endpoint_coroutine_kind():
    """
    FastAPI derives threadpool-vs-await from `iscoroutinefunction(dependant.call)`
    at handler build time (fastapi/routing.py:234).  A wrapper of the wrong kind
    would silently change execution semantics, so the wrapper's kind must match
    the endpoint it wraps.  Every endpoint is currently sync; this must stay
    correct if an async route is added.
    """
    from api.routing import is_payment_wrapped

    examined = 0
    mismatched = []
    for route in v1_api_routes():
        if not is_payment_wrapped(route):
            continue
        examined += 1
        endpoint_is_async = asyncio.iscoroutinefunction(route.endpoint)
        wrapper_is_async = asyncio.iscoroutinefunction(route.dependant.call)
        if endpoint_is_async != wrapper_is_async:
            mismatched.append(v1_path(route))

    # Non-vacuity: "no mismatches" is worthless if nothing was inspected.
    assert examined > 0, (
        "no wrapped routes were examined; this guard would pass vacuously"
    )
    assert not mismatched, (
        "wrapper coroutine kind diverges from the endpoint it wraps: "
        f"{sorted(mismatched)}"
    )


# ---------------------------------------------------------------------------
# 22 — purity of the pre-gate region
# ---------------------------------------------------------------------------

# Dependencies that have been reviewed and classified as safe to run before the
# payment gate.  Entries are (path, method, dependency qualified name).
#
# Adding an entry is an explicit architectural decision, not a convenience:
# sub-dependencies are invoked inside solve_dependencies, which runs BEFORE the
# payment gate.  Anything registered here executes unpaid on every probe of a
# paid endpoint, including requests that never settle.  Approve only work that
# is pure, cheap, and non-billable.
APPROVED_PRE_PAYMENT_DEPENDENCIES: set[tuple[str, str, str]] = set()


def _dependency_names(route: APIRoute) -> list[str]:
    names = []
    for sub in route.dependant.dependencies:
        call = sub.call
        names.append(
            f"{getattr(call, '__module__', '?')}.{getattr(call, '__qualname__', call)}"
        )
    return names


def test_22_payment_governed_routes_have_no_unapproved_pre_payment_dependencies():
    """
    Fail closed on new dependencies over payment-governed routes.

    The guard's purpose is not performance.  FastAPI invokes sub-dependency
    callables in solve_dependencies (fastapi/dependencies/utils.py:638/640),
    which runs before the endpoint wrapper and therefore before the payment
    gate.  A dependency that performs a DB read, an external call, or any other
    billable work would execute pre-payment on every unpaid request.
    """
    governed = payment_governed_routes()
    assert governed, "no payment-governed routes discovered; the guard is inert"

    violations = []
    for route, method in governed:
        for name in _dependency_names(route):
            entry = (v1_path(route), method, name)
            if entry not in APPROVED_PRE_PAYMENT_DEPENDENCIES:
                violations.append(entry)

    assert not violations, (
        "payment-governed route(s) declare unapproved pre-payment dependencies. "
        "These run BEFORE the payment gate. Either remove them, or classify "
        "them in APPROVED_PRE_PAYMENT_DEPENDENCIES with review:\n  "
        + "\n  ".join(f"{m} {p} -> {n}" for p, m, n in sorted(violations))
    )


def test_22_positive_control_dependency_detector_finds_a_dependency():
    """
    Positive control for `_dependency_names`.

    The production surface legitimately declares zero dependencies, so test_22
    currently passes over an empty set on every route.  Without this control, a
    detector that always returned `[]` — a wrong attribute name, a change in
    where FastAPI stores sub-dependants — would look identical to a clean
    codebase.  A synthetic route is used so nothing is added to production.
    """
    from fastapi import APIRouter, Depends, FastAPI
    from fastapi.routing import APIRoute

    def a_pre_payment_dependency() -> str:
        return "would run before the payment gate"

    probe_router = APIRouter()

    @probe_router.get("/probe")
    def probe_endpoint(value: str = Depends(a_pre_payment_dependency)) -> dict:
        return {"value": value}

    probe_app = FastAPI()
    probe_app.include_router(probe_router)

    routes = [r for r in probe_app.routes if isinstance(r, APIRoute) and r.path == "/probe"]
    assert len(routes) == 1, "probe route was not registered"

    names = _dependency_names(routes[0])
    assert len(names) == 1, f"detector found {names} instead of exactly one dependency"
    assert names[0].endswith("a_pre_payment_dependency"), (
        f"detector reported {names[0]!r}, which does not identify the dependency"
    )


def test_22b_approved_dependency_allowlist_has_no_stale_entries():
    """An allowlist entry that no longer matches a real route hides drift."""
    live = {
        (v1_path(route), method, name)
        for route, method in payment_governed_routes()
        for name in _dependency_names(route)
    }
    stale = APPROVED_PRE_PAYMENT_DEPENDENCIES - live
    assert not stale, f"stale pre-payment dependency approvals: {sorted(stale)}"


# ---------------------------------------------------------------------------
# 23 — framework coupling alarm
# ---------------------------------------------------------------------------

def test_fastapi_endpoint_seam_still_behaves_as_verified():
    """
    Pin the FastAPI behaviour the endpoint-call seam is built on.

    If an upgrade changes any of these, the seam must be re-verified before it
    is trusted.  Failing here is loud and early; the alternative is a silent
    return to settling before validation.
    """
    from fastapi.dependencies.models import Dependant
    from fastapi.routing import run_endpoint_function

    # `Dependant.call` is the single invocation point the wrapper replaces.
    fields = Dependant.__dataclass_fields__
    assert "call" in fields, "Dependant.call is gone; the seam has moved"
    assert "request_param_name" in fields, (
        "Dependant.request_param_name is gone; the wrapper can no longer obtain "
        "the Request without it"
    )

    source = inspect.getsource(run_endpoint_function)
    assert "dependant.call(**values)" in source, (
        "run_endpoint_function no longer invokes dependant.call directly"
    )
    assert "run_in_threadpool(dependant.call" in source, (
        "run_endpoint_function no longer dispatches sync endpoints to the "
        "threadpool via dependant.call"
    )

    # The endpoint is only reached when validation produced no errors — this is
    # what makes the ordering guarantee FastAPI's rather than ours.
    import fastapi.routing as fastapi_routing

    handler_source = inspect.getsource(fastapi_routing.get_request_handler)
    assert "if not errors:" in handler_source, (
        "get_request_handler no longer gates run_endpoint_function on an empty "
        "error list; validation may no longer precede endpoint execution"
    )
    assert "is_coroutine = asyncio.iscoroutinefunction(dependant.call)" in handler_source, (
        "is_coroutine is no longer derived from dependant.call; a wrapper could "
        "silently change sync/async execution semantics"
    )


def test_solve_dependencies_still_runs_sub_dependencies_before_param_validation():
    """
    The reason payment must not be a `Depends()`, asserted rather than assumed.

    solve_dependencies invokes sub-dependency callables before validating the
    path operation's own parameters, so a dependency-based gate would settle
    ahead of query and body validation.
    """
    from fastapi.dependencies.utils import solve_dependencies

    source = inspect.getsource(solve_dependencies)
    sub_call = source.index("for sub_dependant in dependant.dependencies:")
    param_validation = source.index("dependant.path_params, request.path_params")

    assert sub_call < param_validation, (
        "sub-dependency invocation no longer precedes parameter validation; "
        "the rationale for rejecting a Depends()-based payment gate has changed"
    )
