from dataclasses import dataclass

from lb.backend import Backend
from lb.balancer import Balancer, IPHash


@dataclass
class FakeRequest:
    """A minimal stand-in for aiohttp.web.Request exposing just the
    attribute IPHash actually reads. Since Python doesn't enforce the type
    annotation at runtime, this duck-typed object works fine as a test
    double and avoids needing a real aiohttp server for these unit tests.
    """

    remote: str


def make_backend(url: str) -> Backend:
    return Backend(url, weight=1)


def test_satisfies_balancer_protocol():
    ip_hash = IPHash([make_backend("http://localhost:9001")])
    assert isinstance(ip_hash, Balancer)


def test_name_property():
    ip_hash = IPHash([])
    assert ip_hash.name == "ip_hash"


def test_empty_backend_list_returns_none():
    ip_hash = IPHash([])
    assert ip_hash.next_backend(FakeRequest(remote="1.2.3.4")) is None


def test_all_dead_returns_none():
    b = make_backend("http://localhost:9001")
    b.set_alive(False)
    ip_hash = IPHash([b])
    assert ip_hash.next_backend(FakeRequest(remote="1.2.3.4")) is None


def test_same_ip_always_routes_to_same_backend():
    backends = [make_backend(f"http://localhost:900{i}") for i in (1, 2, 3)]
    ip_hash = IPHash(backends)

    req = FakeRequest(remote="203.0.113.42")
    first = ip_hash.next_backend(req)
    for _ in range(20):
        assert ip_hash.next_backend(req) is first


def test_different_ips_can_land_on_different_backends():
    backends = [make_backend(f"http://localhost:900{i}") for i in (1, 2, 3)]
    ip_hash = IPHash(backends)

    chosen = {
        ip_hash.next_backend(FakeRequest(remote=f"10.0.0.{i}")).url
        for i in range(50)
    }
    # Not a strict requirement of the algorithm, but with 50 distinct IPs
    # across 3 backends we'd expect more than one backend to be used.
    assert len(chosen) > 1


def test_deterministic_across_new_balancer_instances():
    # crc32 is stable across process runs (unlike Python's built-in hash()
    # with hash randomization), so a fresh IPHash instance over the same
    # backend list must produce the same routing decision for the same IP.
    backends1 = [make_backend(f"http://localhost:900{i}") for i in (1, 2, 3)]
    backends2 = [make_backend(f"http://localhost:900{i}") for i in (1, 2, 3)]
    req = FakeRequest(remote="198.51.100.7")

    result1 = IPHash(backends1).next_backend(req).url
    result2 = IPHash(backends2).next_backend(req).url
    assert result1 == result2


def test_only_selects_alive_backends():
    alive = make_backend("http://localhost:9001")
    dead = make_backend("http://localhost:9002")
    dead.set_alive(False)
    ip_hash = IPHash([alive, dead])

    for i in range(20):
        chosen = ip_hash.next_backend(FakeRequest(remote=f"192.168.1.{i}"))
        assert chosen is alive
