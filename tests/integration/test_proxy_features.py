"""Integration tests for the Phase 3+ proxy features layered on top of the
core reverse-proxy behavior already covered in test_proxy.py: retries,
sticky sessions, passive health tracking, and circuit breaking.
"""
from __future__ import annotations

import pytest
from aiohttp import web

from lb.backend import Backend
from lb.balancer import RoundRobin
from lb.logging import new as new_logger
from lb.proxy import ProxyHandler


async def make_lb_client(aiohttp_client, balancer, backends, **kwargs):
    logger = new_logger(level="error", name="test-proxy-features")
    handler = ProxyHandler(balancer, backends, logger, **kwargs)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler.handle)
    app.on_startup.append(lambda _: handler.startup())
    app.on_cleanup.append(lambda _: handler.cleanup())

    client = await aiohttp_client(app)
    return client, handler


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_backend_after_first_fails(aiohttp_client):
    async def always_fail(request: web.Request) -> web.Response:
        return web.Response(status=503)

    async def always_succeed(request: web.Request) -> web.Response:
        return web.Response(text="ok from good backend")

    fail_app = web.Application()
    fail_app.router.add_route("*", "/{tail:.*}", always_fail)
    fail_client = await aiohttp_client(fail_app)

    good_app = web.Application()
    good_app.router.add_route("*", "/{tail:.*}", always_succeed)
    good_client = await aiohttp_client(good_app)

    bad_backend = Backend(str(fail_client.make_url("")).rstrip("/"), weight=1)
    good_backend = Backend(str(good_client.make_url("")).rstrip("/"), weight=1)
    backends = [bad_backend, good_backend]
    # Round robin over exactly 2 backends: first pick is bad_backend, retry
    # picks good_backend deterministically.
    rr = RoundRobin(backends)
    lb_client, _ = await make_lb_client(
        aiohttp_client, rr, backends, max_retries=2, retry_base_delay_seconds=0.01
    )

    resp = await lb_client.get("/test")
    assert resp.status == 200
    body = await resp.text()
    assert body == "ok from good backend"


@pytest.mark.asyncio
async def test_non_idempotent_method_is_not_retried(aiohttp_client):
    call_count = {"n": 0}

    async def fail_and_count(request: web.Request) -> web.Response:
        call_count["n"] += 1
        return web.Response(status=503)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fail_and_count)
    client = await aiohttp_client(app)

    backend = Backend(str(client.make_url("")).rstrip("/"), weight=1)
    backends = [backend]
    rr = RoundRobin(backends)
    lb_client, _ = await make_lb_client(
        aiohttp_client, rr, backends, max_retries=3, retry_base_delay_seconds=0.01
    )

    resp = await lb_client.post("/test")
    assert resp.status == 503
    assert call_count["n"] == 1, "POST should not be retried even though it failed"


@pytest.mark.asyncio
async def test_passive_health_trips_backend_after_threshold_failures(aiohttp_client):
    async def always_fail(request: web.Request) -> web.Response:
        return web.Response(status=503)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", always_fail)
    client = await aiohttp_client(app)

    backend = Backend(str(client.make_url("")).rstrip("/"), weight=1)
    backends = [backend]
    rr = RoundRobin(backends)
    # max_retries=0 so each HTTP call to the LB maps to exactly one dispatch
    # attempt, making it easy to count passive failures precisely.
    lb_client, _ = await make_lb_client(
        aiohttp_client, rr, backends, max_retries=0, passive_failure_threshold=3
    )

    assert backend.is_alive is True
    await lb_client.get("/1")
    assert backend.is_alive is True
    await lb_client.get("/2")
    assert backend.is_alive is True
    await lb_client.get("/3")
    assert backend.is_alive is False, "3rd consecutive failure should trip the backend dead"


@pytest.mark.asyncio
async def test_sticky_session_cookie_set_and_honored(aiohttp_client):
    async def make_handler(backend_id):
        async def handler(request: web.Request) -> web.Response:
            return web.Response(text=backend_id, headers={"X-Backend-Id": backend_id})
        return handler

    app1 = web.Application()
    app1.router.add_route("*", "/{tail:.*}", await make_handler("backend-1"))
    client1 = await aiohttp_client(app1)

    app2 = web.Application()
    app2.router.add_route("*", "/{tail:.*}", await make_handler("backend-2"))
    client2 = await aiohttp_client(app2)

    b1 = Backend(str(client1.make_url("")).rstrip("/"), weight=1)
    b2 = Backend(str(client2.make_url("")).rstrip("/"), weight=1)
    backends = [b1, b2]
    rr = RoundRobin(backends)
    lb_client, _ = await make_lb_client(aiohttp_client, rr, backends, sticky_sessions=True)

    first_resp = await lb_client.get("/a")
    first_backend = first_resp.headers["X-Backend-Id"]
    assert "LB_STICKY_BACKEND" in [c.key for c in lb_client.session.cookie_jar]

    # Subsequent requests through the same client (same cookie jar) should
    # keep landing on the same backend, even though round robin alone would
    # alternate between the two.
    for _ in range(5):
        resp = await lb_client.get("/a")
        assert resp.headers["X-Backend-Id"] == first_backend


@pytest.mark.asyncio
async def test_sticky_session_falls_back_when_target_backend_dead(aiohttp_client):
    async def ok_handler(request: web.Request) -> web.Response:
        return web.Response(text="ok", headers={"X-Backend-Id": "backend-2"})

    app2 = web.Application()
    app2.router.add_route("*", "/{tail:.*}", ok_handler)
    client2 = await aiohttp_client(app2)

    # backend-1 is never actually reachable/alive; backend-2 is healthy.
    b1 = Backend("http://127.0.0.1:1", weight=1)
    b1.set_alive(False)
    b2 = Backend(str(client2.make_url("")).rstrip("/"), weight=1)
    backends = [b1, b2]
    rr = RoundRobin(backends)
    lb_client, handler = await make_lb_client(aiohttp_client, rr, backends, sticky_sessions=True)

    # Manually set a sticky cookie pointing at the dead backend to simulate
    # "you were previously stuck to a backend that has since gone down".
    from lb.proxy.proxy import _sticky_id_for
    lb_client.session.cookie_jar.update_cookies({"LB_STICKY_BACKEND": _sticky_id_for(b1)})

    resp = await lb_client.get("/a")
    assert resp.status == 200
    assert resp.headers["X-Backend-Id"] == "backend-2"


@pytest.mark.asyncio
async def test_circuit_breaker_opens_and_short_circuits_repeated_failures(aiohttp_client):
    call_count = {"n": 0}

    async def always_fail(request: web.Request) -> web.Response:
        call_count["n"] += 1
        return web.Response(status=503)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", always_fail)
    client = await aiohttp_client(app)

    backend = Backend(str(client.make_url("")).rstrip("/"), weight=1)
    backends = [backend]
    rr = RoundRobin(backends)
    lb_client, handler = await make_lb_client(
        aiohttp_client,
        rr,
        backends,
        max_retries=0,
        circuit_failure_threshold=2,
        circuit_recovery_timeout_seconds=10,
        passive_failure_threshold=100,  # keep backend "alive" so the breaker is what's tested
    )

    await lb_client.get("/1")  # failure 1
    await lb_client.get("/2")  # failure 2 -> breaker should open now

    from lb.circuitbreaker import CircuitState
    assert handler.get_breaker_state(backend) == CircuitState.OPEN

    calls_before = call_count["n"]
    resp = await lb_client.get("/3")
    # With the breaker open and only one backend in the pool, the request
    # should be short-circuited (503, "no healthy backend") without the
    # real backend being called again.
    assert resp.status == 503
    assert call_count["n"] == calls_before, "breaker should have prevented a real network call"


@pytest.mark.asyncio
async def test_connection_error_synthesizes_502_after_exhausting_single_backend(aiohttp_client):
    backend = Backend("http://127.0.0.1:1", weight=1)
    backends = [backend]
    rr = RoundRobin(backends)
    lb_client, _ = await make_lb_client(
        aiohttp_client, rr, backends, max_retries=2, retry_base_delay_seconds=0.01
    )

    resp = await lb_client.get("/")
    assert resp.status == 502
