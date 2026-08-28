"""
Payment execution boundary — the seam where paid work begins.

Why this exists
---------------
Payment enforcement used to run in `MeteringMiddleware` before `call_next()`.
Middleware runs before routing, so a request could settle before FastAPI had
established that the route exists, that the JSON body parses, or that the query
and body values are valid.  A typo in a paid path, or a malformed body, could
therefore move money for work that was never performed.

The governing invariant is:

    No deterministic client-input failure or route-miss condition knowable
    before paid service execution may cause x402 settlement.

Why the endpoint-call seam
--------------------------
FastAPI 0.116.1 reaches `dependant.call(**values)` (fastapi/routing.py,
`run_endpoint_function`) only after `get_request_handler` has parsed the body
and `solve_dependencies` has produced an empty error list.  Route matching,
JSON parsing and Pydantic/query/path validation are therefore all complete
before that call.  Wrapping it puts the payment gate after FastAPI's own
validation without solving dependencies twice, and without a second validation
system of our own.

`tests/test_execution_boundary_guards.py` pins each of those framework
assumptions, so an upgrade that moves the seam fails loudly rather than
silently returning to settle-before-validate.

Request-only semantic validation
--------------------------------
FastAPI's own validation establishes that each value is *structurally* valid.
It cannot establish that the combination of values is *semantically* answerable
— `symbol_exchange=IBM` is a well-formed string, and an empty
`EvaluateSymbolRequest` body is a valid model.  Those rejections are knowable
from the request alone, so they must not cost money either.

An endpoint may therefore register a pre-payment semantic validator with
`@pre_payment_semantic_validator(...)`.  The installer reads the marker off the
endpoint callable it is already wrapping and carries it into the wrapper, which
invokes it on the values FastAPI has already solved, immediately before the
payment gate:

    already-solved FastAPI values
        -> registered route semantic validator, if any
        -> deferred payment gate
        -> original endpoint

Registration is endpoint-local by design.  A central `path -> validator` table
would be a second definition of which routes validate what, free to drift away
from the endpoints it describes; a marker on the callable cannot.

The validator must be synchronous and side-effect free: no database, no
service, no network, no mutable application state.  It exists to reject what is
already knowable, not to do a cheap piece of the paid work early.

Coverage
--------
The wrapper is installed on *every* v1 `APIRoute`, not only on routes a static
policy currently marks as paid: payment eligibility can also arise from prefix
and runtime policy, so a route must not become payable through configuration
while lacking the seam.  The wrapper is inert when no gate is published on
`request.state`.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, Optional

from fastapi.routing import APIRoute, request_response
from starlette.responses import Response

# Marker set on the wrapped endpoint callable.  `test_21` reads it through
# `is_payment_wrapped` to prove universal coverage.
PAYMENT_WRAPPER_ATTR = "__stocktrends_payment_wrapped__"

# `request.state` attribute carrying the middleware's deferred one-shot gate.
PAYMENT_GATE_STATE_ATTR = "payment_gate"

# Key used to receive the `Request` from the solved endpoint values when the
# endpoint does not declare one itself.  `solve_dependencies` assigns
# `values[dependant.request_param_name] = request` unconditionally, so setting
# the name is enough — no extra dependency and no second solve.  The wrapper
# removes the key again before calling the original endpoint.
BOUNDARY_REQUEST_VALUE_KEY = "__stocktrends_boundary_request"

# Stable internal error code for a breach of the execution-boundary invariant.
# Surfaced by the metering finaliser's runtime backstop and by the wrapper's
# own fail-closed path, so operators see one code for one condition.
BOUNDARY_NOT_CONSULTED_ERROR = "payment_execution_boundary_not_consulted"

# Marker carrying an endpoint's registered pre-payment semantic validator.
# Read off the endpoint callable at install time; see `pre_payment_semantic_validator`.
SEMANTIC_VALIDATOR_ATTR = "__stocktrends_pre_payment_semantic_validator__"


class PaymentExecutionBoundaryError(RuntimeError):
    """
    The payment execution boundary could not do its job.

    Raised rather than returned: there is no safe way to continue, and an
    exception cannot be mistaken for a normal result the way a `None` can.
    """


def is_payment_wrapped(target: Any) -> bool:
    """
    True when `target` carries the payment execution boundary.

    Accepts either an `APIRoute` or an endpoint callable so callers can ask the
    question at whichever level they hold.
    """
    dependant = getattr(target, "dependant", None)
    call = getattr(dependant, "call", None) if dependant is not None else None
    if call is None:
        call = target

    return bool(getattr(call, PAYMENT_WRAPPER_ATTR, False))


def pre_payment_semantic_validator(
    validator: Callable[[Any, dict[str, Any]], Any]
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Register `validator` as the endpoint's request-only semantic validation.

    Applied to the endpoint callable itself so that the rule and the route it
    guards cannot be separated.  The installer reads the marker while wrapping
    the finished `APIRoute`; nothing else consults it, and there is deliberately
    no central registry to keep in step.

    `validator(request, values)` receives the recovered `Request` (for
    `request.state.request_id`) and the endpoint values FastAPI has already
    solved — the same mapping the endpoint is about to be called with.  It
    returns nothing and signals rejection by raising the endpoint's existing
    `HTTPException`, so the client-visible contract is unchanged by the move.

    It must be synchronous and side-effect free.  It runs before payment, so
    any database read, service call or billable work placed here would execute
    unpaid on every probe of a paid endpoint.
    """

    def decorate(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        setattr(endpoint, SEMANTIC_VALIDATOR_ATTR, validator)
        return endpoint

    return decorate


def get_pre_payment_semantic_validator(target: Any) -> Optional[Callable[..., Any]]:
    """
    The semantic validator registered for `target`, or `None`.

    Accepts an `APIRoute` or an endpoint callable, mirroring `is_payment_wrapped`,
    so the audit tests can ask the question at whichever level they hold.  On a
    wrapped route the marker is still readable: `functools.wraps` copies the
    wrapped function's `__dict__` onto the wrapper.
    """
    dependant = getattr(target, "dependant", None)
    call = getattr(dependant, "call", None) if dependant is not None else None
    if call is None:
        call = target

    return getattr(call, SEMANTIC_VALIDATOR_ATTR, None)


def _require_request(request: Any) -> Any:
    """
    The `Request` the boundary needs, or a fail-closed error.

    An installed wrapper with no request in hand cannot read `request.state`, so
    it can neither tell a free endpoint from a paid one with a gate waiting, nor
    give a semantic validator the request context its error payloads carry.
    Returning `None` there would let a paid endpoint execute unpaid, so this
    raises instead.
    """
    if request is None:
        raise PaymentExecutionBoundaryError(
            f"{BOUNDARY_NOT_CONSULTED_ERROR}: the payment execution boundary "
            "could not recover the Request from the solved endpoint values, so "
            "it cannot determine whether a payment gate is pending. Refusing to "
            "execute the endpoint."
        )
    return request


def _gate_rejection(request: Any) -> Optional[Response]:
    """
    Invoke the deferred payment gate, if one was published for this request.

    Returns the response that must be sent instead of executing the endpoint,
    or `None` when the request may proceed.  The gate is responsible for its
    own one-shot semantics and for recording its result on `request.state`;
    this module deliberately knows nothing about pricing or payment rails.

    Fails closed when the `Request` cannot be recovered — see `_require_request`.
    """
    request = _require_request(request)

    state = getattr(request, "state", None)
    if state is None:
        raise PaymentExecutionBoundaryError(
            f"{BOUNDARY_NOT_CONSULTED_ERROR}: the request carries no state, so "
            "a pending payment gate could not be read. Refusing to execute the "
            "endpoint."
        )

    gate = getattr(state, PAYMENT_GATE_STATE_ATTR, None)
    if gate is None:
        return None

    return gate()


def _build_wrapper(
    endpoint: Callable[..., Any],
    request_value_key: str,
    injected_request: bool,
    semantic_validator: Optional[Callable[..., Any]] = None,
) -> Callable[..., Any]:
    """
    Build a payment-aware wrapper whose coroutine kind matches `endpoint`.

    `get_request_handler` derives `is_coroutine` from
    `asyncio.iscoroutinefunction(dependant.call)` and dispatches sync callables
    to the threadpool on that basis.  A wrapper of the wrong kind would
    silently change execution semantics, so the two branches below are not
    interchangeable.  Every current endpoint is sync; the async branch keeps
    the seam correct if one is added.

    `semantic_validator`, when the endpoint registered one, runs on the solved
    values before the gate.  It raises to reject, so a rejection propagates as
    the endpoint's own `HTTPException` and the gate is never invoked — nothing
    is verified, settled or authorized for a request that could not be served.
    """

    def _take_request(values: dict[str, Any]) -> Any:
        # When we injected the parameter name ourselves the endpoint knows
        # nothing about it, so it must not reach the call.  When the endpoint
        # declares `request: Request` the value stays in place.
        if injected_request:
            return values.pop(request_value_key, None)
        return values.get(request_value_key)

    def _validate_then_gate(request: Any, values: dict[str, Any]) -> Optional[Response]:
        """
        Semantic validation, then payment — in that order, always.

        The request is recovered before either step: a validator whose error
        payload carries `request_id` needs it, and a missing request must fail
        closed rather than skip the gate.
        """
        request = _require_request(request)
        if semantic_validator is not None:
            semantic_validator(request, values)
        return _gate_rejection(request)

    if asyncio.iscoroutinefunction(endpoint):

        @functools.wraps(endpoint)
        async def wrapper(**values: Any) -> Any:
            rejection = _validate_then_gate(_take_request(values), values)
            if rejection is not None:
                return rejection
            return await endpoint(**values)

    else:

        @functools.wraps(endpoint)
        def wrapper(**values: Any) -> Any:
            rejection = _validate_then_gate(_take_request(values), values)
            if rejection is not None:
                return rejection
            return endpoint(**values)

    # Set after `functools.wraps`, which copies the wrapped function's __dict__.
    setattr(wrapper, PAYMENT_WRAPPER_ATTR, True)
    return wrapper


def install_payment_execution_boundary(app: Any) -> int:
    """
    Install the payment-aware wrapper on every `APIRoute` of `app`.

    Must run *after* all routers have been included: `APIRoute.__init__` builds
    the parameter/dependency model and then the request handler, so the
    dependant must already exist before its `call` is replaced.  The handler is
    rebuilt afterwards because `is_coroutine` and the closed-over dependant are
    captured at build time.

    Mounts and non-API routes (docs, openapi, redirects) are left alone — they
    carry no paid service execution.  Returns the number of routes wrapped so
    startup can assert non-vacuity if it ever needs to.
    """
    wrapped = 0

    for route in getattr(app, "routes", []):
        if not isinstance(route, APIRoute):
            continue
        if is_payment_wrapped(route):
            continue

        dependant = route.dependant
        endpoint_call = dependant.call
        if endpoint_call is None:
            continue

        request_value_key = dependant.request_param_name
        injected_request = False
        if not request_value_key:
            request_value_key = BOUNDARY_REQUEST_VALUE_KEY
            dependant.request_param_name = request_value_key
            injected_request = True

        dependant.call = _build_wrapper(
            endpoint_call,
            request_value_key,
            injected_request,
            get_pre_payment_semantic_validator(endpoint_call),
        )

        # Rebuild the handler so it closes over the wrapped call and re-derives
        # sync/async dispatch from it.
        route.app = request_response(route.get_route_handler())
        wrapped += 1

    return wrapped


def unwrapped_api_routes(app: Any) -> list[APIRoute]:
    """Every `APIRoute` on `app` that does not carry the execution boundary."""
    return [
        route
        for route in getattr(app, "routes", [])
        if isinstance(route, APIRoute) and not is_payment_wrapped(route)
    ]


def assert_payment_boundary_complete(app: Any, *, expected_minimum: int = 1) -> int:
    """
    Refuse to start with an incompletely wrapped route surface.

    Coverage is a load-bearing runtime invariant, not merely a tested property.
    A payment-governed route registered after `install_payment_execution_boundary`
    would otherwise serve its full paid payload with no gate, no challenge and
    no settlement — visible only as a log line after the fact.  Failing
    initialization converts that into an error nobody can deploy past.

    Uses the same marker contract the tests read, so there is one definition of
    "wrapped" rather than two that can drift apart.
    """
    api_routes = [r for r in getattr(app, "routes", []) if isinstance(r, APIRoute)]

    # Non-vacuity: an empty surface would satisfy "every route is wrapped".
    if len(api_routes) < expected_minimum:
        raise PaymentExecutionBoundaryError(
            "payment execution boundary verification is vacuous: found "
            f"{len(api_routes)} APIRoute(s), expected at least {expected_minimum}. "
            "The boundary is probably being verified against the wrong app."
        )

    unwrapped = unwrapped_api_routes(app)
    if unwrapped:
        listing = "\n  ".join(
            sorted(
                f"{sorted(route.methods or set())} {route.path}"
                for route in unwrapped
            )
        )
        raise PaymentExecutionBoundaryError(
            f"{len(unwrapped)} route(s) are missing the payment execution "
            "boundary and could execute paid work without consulting the "
            "payment gate. Every APIRoute must be wrapped before the "
            f"application serves traffic:\n  {listing}"
        )

    return len(api_routes)
