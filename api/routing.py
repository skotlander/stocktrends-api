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


def _gate_rejection(request: Any) -> Optional[Response]:
    """
    Invoke the deferred payment gate, if one was published for this request.

    Returns the response that must be sent instead of executing the endpoint,
    or `None` when the request may proceed.  The gate is responsible for its
    own one-shot semantics and for recording its result on `request.state`;
    this module deliberately knows nothing about pricing or payment rails.
    """
    if request is None:
        return None

    state = getattr(request, "state", None)
    gate = getattr(state, PAYMENT_GATE_STATE_ATTR, None) if state is not None else None
    if gate is None:
        return None

    return gate()


def _build_wrapper(
    endpoint: Callable[..., Any],
    request_value_key: str,
    injected_request: bool,
) -> Callable[..., Any]:
    """
    Build a payment-aware wrapper whose coroutine kind matches `endpoint`.

    `get_request_handler` derives `is_coroutine` from
    `asyncio.iscoroutinefunction(dependant.call)` and dispatches sync callables
    to the threadpool on that basis.  A wrapper of the wrong kind would
    silently change execution semantics, so the two branches below are not
    interchangeable.  Every current endpoint is sync; the async branch keeps
    the seam correct if one is added.
    """

    def _take_request(values: dict[str, Any]) -> Any:
        # When we injected the parameter name ourselves the endpoint knows
        # nothing about it, so it must not reach the call.  When the endpoint
        # declares `request: Request` the value stays in place.
        if injected_request:
            return values.pop(request_value_key, None)
        return values.get(request_value_key)

    if asyncio.iscoroutinefunction(endpoint):

        @functools.wraps(endpoint)
        async def wrapper(**values: Any) -> Any:
            rejection = _gate_rejection(_take_request(values))
            if rejection is not None:
                return rejection
            return await endpoint(**values)

    else:

        @functools.wraps(endpoint)
        def wrapper(**values: Any) -> Any:
            rejection = _gate_rejection(_take_request(values))
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
        )

        # Rebuild the handler so it closes over the wrapped call and re-derives
        # sync/async dispatch from it.
        route.app = request_response(route.get_route_handler())
        wrapped += 1

    return wrapped
