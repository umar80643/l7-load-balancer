"""Rate limiting as an aiohttp middleware.

Design decision: this lives as a middleware, not inside ProxyHandler.
Rate limiting is a per-client-request concern ("should this request be let
through at all?") that's fully decidable before we know anything about
backends or balancing -- keeping it as a middleware in front of the proxy
handler follows the Single Responsibility Principle and means it applies
uniformly to every route (including /metrics and /admin/*) without the
proxy handler needing to know it exists.

Client identification: we key on request.remote (the direct TCP peer),
same caveat as IP Hash -- behind another proxy this would be that proxy's
IP rather than the real client's. Noted here and in the README as a
shared, deliberate scope cut across all IP-based features in this project.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from aiohttp.web import Request, Response, StreamResponse, middleware

from lb.metrics import Metrics
from lb.ratelimit import RateLimiter

Handler = Callable[[Request], Awaitable[StreamResponse]]


def make_rate_limit_middleware(
    limiter: RateLimiter,
    logger: logging.Logger,
    metrics: Metrics | None = None,
):
    @middleware
    async def rate_limit_middleware(request: Request, handler: Handler) -> StreamResponse:
        client_key = request.remote or "unknown"
        if limiter.allow(client_key):
            return await handler(request)

        logger.warning("rate limit exceeded", extra={"client": client_key, "path": request.path})
        if metrics is not None:
            metrics.rate_limit_rejections_total.inc()
        return Response(
            status=429,
            text="429 Too Many Requests",
            headers={"Retry-After": "1"},
        )

    return rate_limit_middleware
