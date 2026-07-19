import threading

from lb.backend import Backend
from lb.balancer import Balancer, RoundRobin

# Round robin doesn't inspect the request at all, so a None stand-in is fine
# here -- this keeps these tests fast and dependency-free from aiohttp's
# actual Request machinery. Algorithms that DO need real requests (IP Hash,
# sticky sessions) will get proper fixtures in their own Phase 2/6 test files.
FAKE_REQUEST = None


def make_backend(url: str) -> Backend:
    return Backend(url, weight=1)


def test_round_robin_satisfies_balancer_protocol():
    rr = RoundRobin([make_backend("http://localhost:9001")])
    assert isinstance(rr, Balancer)


def test_distributes_evenly_across_three_backends():
    backends = [make_backend(f"http://localhost:900{i}") for i in (1, 2, 3)]
    rr = RoundRobin(backends)

    counts = {b.url: 0 for b in backends}
    total_requests = 300
    for _ in range(total_requests):
        chosen = rr.next_backend(FAKE_REQUEST)
        assert chosen is not None
        counts[chosen.url] += 1

    for url, count in counts.items():
        assert count == total_requests // 3, f"{url} got {count} requests"


def test_skips_dead_backends():
    alive = make_backend("http://localhost:9001")
    dead = make_backend("http://localhost:9002")
    dead.set_alive(False)
    rr = RoundRobin([alive, dead])

    for _ in range(10):
        chosen = rr.next_backend(FAKE_REQUEST)
        assert chosen is alive


def test_all_backends_dead_returns_none():
    b = make_backend("http://localhost:9001")
    b.set_alive(False)
    rr = RoundRobin([b])
    assert rr.next_backend(FAKE_REQUEST) is None


def test_empty_backend_list_returns_none():
    rr = RoundRobin([])
    assert rr.next_backend(FAKE_REQUEST) is None


def test_name_property():
    rr = RoundRobin([])
    assert rr.name == "round_robin"


def test_concurrent_access_is_safe():
    backends = [make_backend("http://localhost:9001"), make_backend("http://localhost:9002")]
    rr = RoundRobin(backends)
    errors = []

    def hit():
        for _ in range(100):
            if rr.next_backend(FAKE_REQUEST) is None:
                errors.append("got None unexpectedly")

    threads = [threading.Thread(target=hit) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
