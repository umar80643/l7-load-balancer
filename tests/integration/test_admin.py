import pytest
from aiohttp import web

from lb.admin import register
from lb.backend import Backend
from lb.logging import new as new_logger


async def make_admin_client(aiohttp_client, backends):
    app = web.Application()
    register(app, backends, new_logger("error", "test-admin"))
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_list_backends_empty(aiohttp_client):
    backends = []
    client = await make_admin_client(aiohttp_client, backends)

    resp = await client.get("/admin/backends")
    assert resp.status == 200
    data = await resp.json()
    assert data == {"backends": []}


@pytest.mark.asyncio
async def test_list_backends_returns_current_state(aiohttp_client):
    backends = [Backend("http://localhost:9001", weight=2)]
    backends[0].increment_connections()
    client = await make_admin_client(aiohttp_client, backends)

    resp = await client.get("/admin/backends")
    data = await resp.json()
    assert len(data["backends"]) == 1
    entry = data["backends"][0]
    assert entry["url"] == "http://localhost:9001"
    assert entry["weight"] == 2
    assert entry["alive"] is True
    assert entry["active_connections"] == 1


@pytest.mark.asyncio
async def test_add_backend_appends_to_shared_list(aiohttp_client):
    backends = []
    client = await make_admin_client(aiohttp_client, backends)

    resp = await client.post("/admin/backends", json={"url": "http://localhost:9002", "weight": 3})
    assert resp.status == 201
    data = await resp.json()
    assert data["url"] == "http://localhost:9002"
    assert data["weight"] == 3

    # The list passed into register() must have been mutated in place --
    # this is what makes the balancer/proxy see the new backend immediately.
    assert len(backends) == 1
    assert backends[0].url == "http://localhost:9002"


@pytest.mark.asyncio
async def test_add_backend_missing_url_returns_400(aiohttp_client):
    backends = []
    client = await make_admin_client(aiohttp_client, backends)

    resp = await client.post("/admin/backends", json={"weight": 1})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_add_duplicate_backend_returns_409(aiohttp_client):
    backends = [Backend("http://localhost:9003", weight=1)]
    client = await make_admin_client(aiohttp_client, backends)

    resp = await client.post("/admin/backends", json={"url": "http://localhost:9003"})
    assert resp.status == 409
    assert len(backends) == 1


@pytest.mark.asyncio
async def test_add_backend_invalid_url_returns_400(aiohttp_client):
    backends = []
    client = await make_admin_client(aiohttp_client, backends)

    resp = await client.post("/admin/backends", json={"url": "not-a-valid-url"})
    assert resp.status == 400
    assert len(backends) == 0


@pytest.mark.asyncio
async def test_remove_backend_removes_from_shared_list(aiohttp_client):
    backends = [Backend("http://localhost:9004", weight=1)]
    client = await make_admin_client(aiohttp_client, backends)

    resp = await client.delete("/admin/backends/http://localhost:9004")
    assert resp.status == 204
    assert len(backends) == 0


@pytest.mark.asyncio
async def test_remove_nonexistent_backend_returns_404(aiohttp_client):
    backends = []
    client = await make_admin_client(aiohttp_client, backends)

    resp = await client.delete("/admin/backends/http://localhost:9999")
    assert resp.status == 404
