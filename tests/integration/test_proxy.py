"""Integration tests for ProxyHandler.

These spin up real aiohttp TestServer instances (actual TCP listeners) for
both the "backend" and the load balancer itself, and make real HTTP
requests through the full stack. This exercises the actual network path,
not just our own code in isolation -- that's what makes these integration
tests rather than unit tests.
"""
from __future__ import annotations

import pytest
from aiohttp import web

from lb.backend import Backend
from lb.balancer import RoundRobin
from lb.logging import new as new_logger
from lb.proxy import ProxyHandler


async def make_echo_backend(aiohttp_client, backend_id: str):
    """Starts a real backend server that identifies itself in responses."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(
            text=f"response from {backend_id}",
            headers={"X-Backend-Id": backend_id},
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return await aiohttp_client(app)


async def make_lb_client(aiohttp_client, balancer, backends, **kwargs):
    """Wires a ProxyHandler into a fresh aiohttp app and starts a test client."""
    logger = new_logger(level="error", name="test-proxy")
    handler = ProxyHandler(balancer, backends, logger, **kwargs)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler.handle)
    app.on_startup.append(lambda _: handler.startup())
    app.on_cleanup.append(lambda _: handler.cleanup())

    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_routes_to_single_backend(aiohttp_client):
    backend_client = await make_echo_backend(aiohttp_client, "backend-1")
    backend_url = str(backend_client.make_url(""))

    backend = Backend(backend_url.rstrip("/"), weight=1)
    backends_list = [backend]
    rr = RoundRobin(backends_list)
    lb_client = await make_lb_client(aiohttp_client, rr, backends_list)

    resp = await lb_client.get("/hello")
    assert resp.status == 200
    body = await resp.text()
    assert body == "response from backend-1"
    assert resp.headers["X-Backend-Id"] == "backend-1"


@pytest.mark.asyncio
async def test_distributes_across_multiple_backends(aiohttp_client):
    client1 = await make_echo_backend(aiohttp_client, "backend-1")
    client2 = await make_echo_backend(aiohttp_client, "backend-2")

    b1 = Backend(str(client1.make_url("")).rstrip("/"), weight=1)
    b2 = Backend(str(client2.make_url("")).rstrip("/"), weight=1)
    backends_list = [b1, b2]
    rr = RoundRobin(backends_list)
    lb_client = await make_lb_client(aiohttp_client, rr, backends_list)

    seen = {}
    for _ in range(10):
        resp = await lb_client.get("/")
        backend_id = resp.headers["X-Backend-Id"]
        seen[backend_id] = seen.get(backend_id, 0) + 1

    assert seen == {"backend-1": 5, "backend-2": 5}


@pytest.mark.asyncio
async def test_no_backends_returns_503(aiohttp_client):
    backends_list = []
    rr = RoundRobin(backends_list)
    lb_client = await make_lb_client(aiohttp_client, rr, backends_list)

    resp = await lb_client.get("/")
    assert resp.status == 503


@pytest.mark.asyncio
async def test_all_backends_dead_returns_503(aiohttp_client):
    backend_client = await make_echo_backend(aiohttp_client, "backend-1")
    b = Backend(str(backend_client.make_url("")).rstrip("/"), weight=1)
    b.set_alive(False)
    backends_list = [b]
    rr = RoundRobin(backends_list)
    lb_client = await make_lb_client(aiohttp_client, rr, backends_list)

    resp = await lb_client.get("/")
    assert resp.status == 503


@pytest.mark.asyncio
async def test_backend_connection_refused_returns_502(aiohttp_client):
    # Port 1 should have nothing listening on it in the test environment.
    b = Backend("http://127.0.0.1:1", weight=1)
    backends_list = [b]
    rr = RoundRobin(backends_list)
    lb_client = await make_lb_client(aiohttp_client, rr, backends_list)

    resp = await lb_client.get("/")
    assert resp.status == 502


@pytest.mark.asyncio
async def test_request_method_and_path_are_forwarded(aiohttp_client):
    seen = {}

    async def handler(request: web.Request) -> web.Response:
        seen["method"] = request.method
        seen["path"] = request.path
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    backend_client = await aiohttp_client(app)

    b = Backend(str(backend_client.make_url("")).rstrip("/"), weight=1)
    backends_list = [b]
    rr = RoundRobin(backends_list)
    lb_client = await make_lb_client(aiohttp_client, rr, backends_list)

    resp = await lb_client.post("/api/v1/widgets")
    assert resp.status == 200
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/widgets"


@pytest.mark.asyncio
async def test_active_connections_tracked_during_and_after_request(aiohttp_client):
    # Uses a backend that pauses mid-request so we can observe the
    # connection counter while a request is actually in flight, then
    # confirm it drops back to zero once the response completes. This is
    # what gives Power of Two Choices real signal to compare backends by.
    import asyncio

    async def slow_handler(request: web.Request) -> web.Response:
        await asyncio.sleep(0.2)
        return web.Response(text="done")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", slow_handler)
    backend_client = await aiohttp_client(app)

    backend = Backend(str(backend_client.make_url("")).rstrip("/"), weight=1)
    backends_list = [backend]
    rr = RoundRobin(backends_list)
    lb_client = await make_lb_client(aiohttp_client, rr, backends_list)

    assert backend.active_connections == 0

    request_task = asyncio.ensure_future(lb_client.get("/slow"))
    await asyncio.sleep(0.05)  # let the request actually reach the backend
    assert backend.active_connections == 1, "expected counter to be 1 while request is in flight"

    resp = await request_task
    assert resp.status == 200
    assert backend.active_connections == 0, "expected counter to return to 0 after completion"


@pytest.mark.asyncio
async def test_active_connections_decremented_even_on_backend_error(aiohttp_client):
    # A connection-refused backend should still leave the counter at 0
    # afterward -- the finally block must run on the error path too.
    backend = Backend("http://127.0.0.1:1", weight=1)
    backends_list = [backend]
    rr = RoundRobin(backends_list)
    lb_client = await make_lb_client(aiohttp_client, rr, backends_list)

    resp = await lb_client.get("/")
    assert resp.status == 502
    assert backend.active_connections == 0


@pytest.mark.asyncio
async def test_response_body_streamed_correctly_for_larger_payload(aiohttp_client):
    large_body = "x" * 500_000  # 500KB, large enough to span multiple chunks

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=large_body)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    backend_client = await aiohttp_client(app)

    b = Backend(str(backend_client.make_url("")).rstrip("/"), weight=1)
    backends_list = [b]
    rr = RoundRobin(backends_list)
    lb_client = await make_lb_client(aiohttp_client, rr, backends_list)

    resp = await lb_client.get("/big")
    assert resp.status == 200
    body = await resp.text()
    assert body == large_body
