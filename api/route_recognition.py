"""
Authoritative route and method recognition, asked before routing happens.

Why this exists
---------------
`MeteringMiddleware` runs before Starlette dispatches, so anything it decides
about a request is decided without knowing whether the path names a real route
or whether the method is allowed.  That is exactly the ignorance that made
early payment enforcement unsafe, and it is the same ignorance that would make
an early *challenge* unsafe: a typo under a paid prefix must stay `404`, and a
wrong method must stay `405`, not become a priced `402`.

There are two ways to answer the question early.  One is to keep a second
path/method table inside the payment layer, which is free to drift away from
the routers it claims to describe — the failure mode being a `402` quoted for a
resource that does not exist.  The other is to ask the application's own
routers the question the dispatcher is about to ask.  This module does the
second.

How it mirrors the dispatcher
-----------------------------
`starlette.routing.Router.app` resolves a request by walking `self.routes` in
order, taking the first `Match.FULL`, remembering whether any route returned
`Match.PARTIAL` (path matched, method did not), and otherwise falling through
to its redirect/`404` handling.  `recognize_route` performs the same walk with
the same `route.matches(scope)` calls, descending into a mount the way the
dispatcher descends into a sub-application — which is how a route on the `/v1`
sub-app is reached from middleware mounted on the root app.

It reports; it does not route.  Nothing here handles a request or mutates the
live scope: matching is performed against copies, so the scope the dispatcher
later receives is untouched.

Fail-closed by construction
---------------------------
Only a definite `Match.FULL` onto an `APIRoute` is reported as recognized.
A path that would be answered by Starlette's redirect-slash fallback, a mount
that resolves to something other than an API route, and an unmatched path are
all reported as unrecognized, because in each case the middleware does not
actually know what the dispatcher will do.  A caller may use recognition to
*refuse* to act early; it must never use it to answer a request the dispatcher
would have sent somewhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi.routing import APIRoute
from starlette.routing import Match

# Depth limit for the mount descent.  This application nests one level (`/v1`),
# so the bound only exists to make a pathological or cyclic route graph fail
# closed instead of recursing without end.
_MAX_MOUNT_DEPTH = 8


class RouteRecognition(str, Enum):
    """What the dispatcher would find for a request, as far as we can tell."""

    #: The path and method resolve to a real `APIRoute`.
    API_ROUTE = "api_route"

    #: The path matches at least one route, but not for this method (`405`).
    METHOD_NOT_ALLOWED = "method_not_allowed"

    #: Nothing matched, or only a redirect candidate matched (`404`/`307`).
    NOT_FOUND = "not_found"

    #: Something matched, but it is not an `APIRoute` — a mount with no routes,
    #: a static-files app, a websocket route.  No paid service lives there.
    NOT_AN_API_ROUTE = "not_an_api_route"


@dataclass(frozen=True)
class RecognizedRoute:
    """The recognition outcome, and the route when there is one."""

    recognition: RouteRecognition
    route: APIRoute | None = None

    #: Prefix consumed while descending through mounts to reach `route`.
    _mount_prefix: str = ""

    @property
    def is_api_route(self) -> bool:
        return self.recognition is RouteRecognition.API_ROUTE and self.route is not None

    @property
    def route_template(self) -> str | None:
        """
        The matched route's declared path template, including any mount prefix.

        `APIRoute.path` is relative to the application the route belongs to, so
        a route on the `/v1` sub-app reports `/prices/history`.  The prefix the
        descent consumed is added back, giving the externally addressable
        template — `/v1/prices/history`, or `/v1/intelligence/guidance/{artifact_id}`
        for a parameterized resource.  This is FastAPI's own declaration of the
        route's shape, not a string reconstructed from the request.
        """
        if self.route is None:
            return None
        return f"{self._mount_prefix}{self.route.path}"


def recognize_route(app: Any, scope: Any) -> RecognizedRoute:
    """
    Report what `app`'s routers would resolve `scope` to.

    `app` is the application whose routing tables are authoritative — inside
    middleware, `scope["app"]`, which Starlette sets to the application the
    middleware stack belongs to.  A missing or route-less app is reported as
    `NOT_FOUND` rather than treated as a match.
    """
    routes = getattr(app, "routes", None)
    if not routes:
        return RecognizedRoute(RouteRecognition.NOT_FOUND)

    return _walk(routes, dict(scope), depth=0, prefix="")


def _walk(routes: Any, scope: dict, *, depth: int, prefix: str) -> RecognizedRoute:
    if depth > _MAX_MOUNT_DEPTH:
        return RecognizedRoute(RouteRecognition.NOT_FOUND)

    saw_partial = False

    for route in routes:
        try:
            match, child_scope = route.matches(scope)
        except Exception:  # noqa: BLE001 - an unmatchable route is not a match
            # A route whose matcher raises tells us nothing, and guessing on its
            # behalf is how an early challenge would reach a path that does not
            # resolve.  Report unrecognized and let the dispatcher answer.
            return RecognizedRoute(RouteRecognition.NOT_FOUND)

        if match == Match.FULL:
            if isinstance(route, APIRoute):
                return RecognizedRoute(
                    RouteRecognition.API_ROUTE,
                    route,
                    _mount_prefix=prefix,
                )

            sub_routes = getattr(route, "routes", None)
            if not sub_routes:
                return RecognizedRoute(RouteRecognition.NOT_AN_API_ROUTE)

            # A mount: descend with the scope the dispatcher would hand the
            # sub-application, and remember the prefix it consumed.
            return _walk(
                sub_routes,
                {**scope, **child_scope},
                depth=depth + 1,
                prefix=f"{prefix}{getattr(route, 'path', '')}",
            )

        if match == Match.PARTIAL:
            saw_partial = True

    if saw_partial:
        return RecognizedRoute(RouteRecognition.METHOD_NOT_ALLOWED)

    return RecognizedRoute(RouteRecognition.NOT_FOUND)
