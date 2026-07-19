"""Admin API: HTTP endpoints for inspecting and dynamically changing the
backend pool at runtime, without restarting the load balancer.

This is what makes the autoscaling simulation (scripts/autoscaler.py)
possible: it lets an external process launch a new backend container and
register it with the running load balancer, or deregister and stop one,
without any config reload or process restart.

Design decision: these handlers mutate the shared `backends` list
in-place (`.append()` / `.remove()`), never reassign it to a new list
object. Every component that was constructed with a reference to this list
-- the Balancer, the ProxyHandler -- holds that same reference, so an
in-place mutation is immediately visible everywhere with no extra wiring,
no pub/sub, no polling. Reassigning `backends = [...]` instead would break
this: the other components would keep pointing at the old list forever.

SECURITY NOTE (also called out in config.py and the README): this API is
unauthenticated. That's an acceptable simplification for a resume/demo
project running in a private Docker network, but it lets any caller who
can reach it re-route production traffic. A real deployment must put this
behind a firewall / private network / mTLS, or add authentication, before
exposing it beyond localhost.
"""
from __future__ import annotations

import logging

from aiohttp.web import Request, Response, RouteTableDef, json_response

from lb.backend import Backend

routes = RouteTableDef()


def backend_to_dict(b: Backend) -> dict:
    return {
        "url": b.url,
        "weight": b.weight,
        "alive": b.is_alive,
        "active_connections": b.active_connections,
        "consecutive_failures": b.consecutive_failures,
    }


class AdminHandlers:
    """Bundles the shared backend list + logger the admin routes need.
    Registered onto an aiohttp app via `register(app, path_prefix)`.
    """

    def __init__(self, backends: list[Backend], logger: logging.Logger) -> None:
        self._backends = backends
        self._log = logger

    async def list_backends(self, request: Request) -> Response:
        return json_response({"backends": [backend_to_dict(b) for b in self._backends]})

    async def add_backend(self, request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return json_response({"error": "invalid JSON body"}, status=400)

        url = payload.get("url")
        weight = payload.get("weight", 1)
        if not url:
            return json_response({"error": "'url' is required"}, status=400)

        if any(b.url == url for b in self._backends):
            return json_response({"error": f"backend {url!r} already registered"}, status=409)

        try:
            new_backend = Backend(url, weight=weight)
        except ValueError as exc:
            return json_response({"error": str(exc)}, status=400)

        self._backends.append(new_backend)
        self._log.info("backend registered via admin API", extra={"backend": url, "weight": weight})
        return json_response(backend_to_dict(new_backend), status=201)

    async def remove_backend(self, request: Request) -> Response:
        url = request.match_info["url"]
        for b in self._backends:
            if b.url == url:
                self._backends.remove(b)
                self._log.info("backend deregistered via admin API", extra={"backend": url})
                return Response(status=204)
        return json_response({"error": f"backend {url!r} not found"}, status=404)


def register(app, backends: list[Backend], logger: logging.Logger, path_prefix: str = "/admin") -> None:
    handlers = AdminHandlers(backends, logger)
    app.router.add_get(f"{path_prefix}/backends", handlers.list_backends)
    app.router.add_post(f"{path_prefix}/backends", handlers.add_backend)
    # {url:.*} (not the default {url}) so a full "http://host:port" value
    # -- which itself contains slashes -- survives as a single path
    # parameter instead of aiohttp splitting on the embedded slashes.
    app.router.add_delete(f"{path_prefix}/backends/{{url:.*}}", handlers.remove_backend)
