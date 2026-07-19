import asyncio

import pytest
from aiohttp import web

from lb.backend import Backend
from lb.healthcheck import ActiveHealthChecker
from lb.logging import new as new_logger


@pytest.mark.asyncio
async def test_marks_backend_dead_after_threshold_failures(aiohttp_client):
    async def always_fail(request: web.Request) -> web.Response:
        return web.Response(status=500)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", always_fail)
    client = await aiohttp_client(app)

    backend = Backend(str(client.make_url("")).rstrip("/"), weight=1)
    checker = ActiveHealthChecker(
        [backend],
        logger=new_logger("error", "test-health-1"),
        interval_seconds=0.05,
        unhealthy_threshold=2,
        healthy_threshold=1,
    )

    assert backend.is_alive is True
    await checker._check_all_once()
    assert backend.is_alive is True, "one failure shouldn't trip it yet"
    await checker._check_all_once()
    assert backend.is_alive is False, "second consecutive failure should trip it"
    await checker.stop()


@pytest.mark.asyncio
async def test_marks_backend_alive_again_after_recovery(aiohttp_client):
    state = {"healthy": False}

    async def flaky(request: web.Request) -> web.Response:
        return web.Response(status=200 if state["healthy"] else 503)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", flaky)
    client = await aiohttp_client(app)

    backend = Backend(str(client.make_url("")).rstrip("/"), weight=1)
    backend.set_alive(False)  # starts dead
    checker = ActiveHealthChecker(
        [backend],
        logger=new_logger("error", "test-health-2"),
        interval_seconds=0.05,
        unhealthy_threshold=1,
        healthy_threshold=2,
    )

    state["healthy"] = True
    await checker._check_all_once()
    assert backend.is_alive is False, "only 1 success so far, threshold is 2"
    await checker._check_all_once()
    assert backend.is_alive is True, "second consecutive success should recover it"
    await checker.stop()


@pytest.mark.asyncio
async def test_connection_refused_counts_as_failure(aiohttp_client):
    backend = Backend("http://127.0.0.1:1", weight=1)
    checker = ActiveHealthChecker(
        [backend],
        logger=new_logger("error", "test-health-3"),
        interval_seconds=0.05,
        unhealthy_threshold=1,
    )

    await checker._check_all_once()
    assert backend.is_alive is False
    await checker.stop()


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle_runs_without_error(aiohttp_client):
    async def ok(request: web.Request) -> web.Response:
        return web.Response(status=200)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", ok)
    client = await aiohttp_client(app)

    backend = Backend(str(client.make_url("")).rstrip("/"), weight=1)
    checker = ActiveHealthChecker(
        [backend],
        logger=new_logger("error", "test-health-4"),
        interval_seconds=0.02,
    )

    await checker.start()
    await asyncio.sleep(0.1)  # let a couple of check cycles run
    await checker.stop()
    assert backend.is_alive is True


@pytest.mark.asyncio
async def test_checks_multiple_backends_concurrently(aiohttp_client):
    async def ok(request: web.Request) -> web.Response:
        return web.Response(status=200)

    async def fail(request: web.Request) -> web.Response:
        return web.Response(status=500)

    app_ok = web.Application()
    app_ok.router.add_route("*", "/{tail:.*}", ok)
    client_ok = await aiohttp_client(app_ok)

    app_fail = web.Application()
    app_fail.router.add_route("*", "/{tail:.*}", fail)
    client_fail = await aiohttp_client(app_fail)

    good = Backend(str(client_ok.make_url("")).rstrip("/"), weight=1)
    bad = Backend(str(client_fail.make_url("")).rstrip("/"), weight=1)

    checker = ActiveHealthChecker(
        [good, bad],
        logger=new_logger("error", "test-health-5"),
        interval_seconds=0.05,
        unhealthy_threshold=1,
    )

    await checker._check_all_once()
    assert good.is_alive is True
    assert bad.is_alive is False
    await checker.stop()
