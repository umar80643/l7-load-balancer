import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from lb import config as config_module
from lb.main import create_app


@pytest.fixture
async def backend_url(aiohttp_client):
    async def echo(request: web.Request) -> web.Response:
        return web.Response(text="hello from backend")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", echo)
    client = await aiohttp_client(app)
    return str(client.make_url("")).rstrip("/")


@pytest.mark.asyncio
async def test_create_app_proxies_requests(backend_url):
    cfg = config_module.Config.model_validate(
        {
            "backends": [{"url": backend_url, "weight": 1}],
            "health_check": {"enabled": False},
        }
    )
    app = create_app(cfg, log_level="error")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/hello")
        assert resp.status == 200
        assert await resp.text() == "hello from backend"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_app_exposes_admin_and_metrics_routes(backend_url):
    cfg = config_module.Config.model_validate(
        {
            "backends": [{"url": backend_url, "weight": 1}],
            "health_check": {"enabled": False},
        }
    )
    app = create_app(cfg, log_level="error")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/admin/backends")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["backends"]) == 1

        resp = await client.post(
            "/admin/backends", json={"url": "http://localhost:9999", "weight": 1}
        )
        assert resp.status == 201

        resp = await client.get("/metrics")
        assert resp.status == 200
        text = await resp.text()
        assert "lb_requests_total" in text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_app_with_rate_limiting_enabled(backend_url):
    cfg = config_module.Config.model_validate(
        {
            "backends": [{"url": backend_url, "weight": 1}],
            "health_check": {"enabled": False},
            "rate_limit": {"enabled": True, "requests_per_second": 0.001, "burst": 1},
        }
    )
    app = create_app(cfg, log_level="error")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/a")).status == 200
        assert (await client.get("/a")).status == 429
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_app_with_health_checks_enabled_starts_and_stops_cleanly(backend_url):
    cfg = config_module.Config.model_validate(
        {
            "backends": [{"url": backend_url, "weight": 1}],
            "health_check": {"enabled": True, "interval_seconds": 30},
        }
    )
    app = create_app(cfg, log_level="error")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/hello")
        assert resp.status == 200
    finally:
        await client.close()


def test_build_balancer_rejects_unknown_algorithm():
    from lb.main import build_balancer

    with pytest.raises(ValueError):
        build_balancer("not_a_real_algorithm", [])
