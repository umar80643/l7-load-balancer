import pytest
from aiohttp import web

from lb.logging import new as new_logger
from lb.metrics import Metrics
from lb.middleware import make_rate_limit_middleware
from lb.ratelimit import RateLimiter


async def make_rate_limited_client(aiohttp_client, requests_per_second, burst, metrics=None):
    limiter = RateLimiter(requests_per_second=requests_per_second, burst=burst)
    logger = new_logger("error", "test-ratelimit")
    middleware = make_rate_limit_middleware(limiter, logger, metrics=metrics)

    async def ok_handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(middlewares=[middleware])
    app.router.add_get("/{tail:.*}", ok_handler)
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_requests_within_burst_are_allowed(aiohttp_client):
    client = await make_rate_limited_client(aiohttp_client, requests_per_second=1, burst=3)

    for _ in range(3):
        resp = await client.get("/")
        assert resp.status == 200


@pytest.mark.asyncio
async def test_requests_beyond_burst_are_rejected(aiohttp_client):
    client = await make_rate_limited_client(aiohttp_client, requests_per_second=0.001, burst=2)

    assert (await client.get("/")).status == 200
    assert (await client.get("/")).status == 200
    resp = await client.get("/")
    assert resp.status == 429
    assert resp.headers.get("Retry-After") is not None


@pytest.mark.asyncio
async def test_rejection_increments_metrics_counter(aiohttp_client):
    metrics = Metrics()
    client = await make_rate_limited_client(
        aiohttp_client, requests_per_second=0.001, burst=1, metrics=metrics
    )

    await client.get("/")  # consumes the only token
    await client.get("/")  # rejected

    assert metrics.rate_limit_rejections_total._value.get() == 1
