from lb.backend import Backend
from lb.balancer import Balancer, LeastConnections

FAKE_REQUEST = None


def make_backend(url: str) -> Backend:
    return Backend(url, weight=1)


def test_satisfies_balancer_protocol():
    lc = LeastConnections([make_backend("http://localhost:9001")])
    assert isinstance(lc, Balancer)


def test_name_property():
    assert LeastConnections([]).name == "least_connections"


def test_empty_backend_list_returns_none():
    assert LeastConnections([]).next_backend(FAKE_REQUEST) is None


def test_all_dead_returns_none():
    b = make_backend("http://localhost:9001")
    b.set_alive(False)
    assert LeastConnections([b]).next_backend(FAKE_REQUEST) is None


def test_picks_backend_with_fewest_connections():
    busy = make_backend("http://localhost:9001")
    idle = make_backend("http://localhost:9002")
    for _ in range(5):
        busy.increment_connections()

    lc = LeastConnections([busy, idle])
    assert lc.next_backend(FAKE_REQUEST) is idle


def test_ties_broken_deterministically_by_first_occurrence():
    a = make_backend("http://localhost:9001")
    b = make_backend("http://localhost:9002")
    lc = LeastConnections([a, b])
    # Both at 0 connections -- min() with no key ties returns the first.
    assert lc.next_backend(FAKE_REQUEST) is a


def test_ignores_dead_backends_even_if_least_loaded():
    dead_but_idle = make_backend("http://localhost:9001")
    dead_but_idle.set_alive(False)
    alive_but_busy = make_backend("http://localhost:9002")
    for _ in range(3):
        alive_but_busy.increment_connections()

    lc = LeastConnections([dead_but_idle, alive_but_busy])
    assert lc.next_backend(FAKE_REQUEST) is alive_but_busy


def test_reacts_to_live_connection_count_changes():
    a = make_backend("http://localhost:9001")
    b = make_backend("http://localhost:9002")
    lc = LeastConnections([a, b])

    a.increment_connections()
    assert lc.next_backend(FAKE_REQUEST) is b

    b.increment_connections()
    b.increment_connections()
    assert lc.next_backend(FAKE_REQUEST) is a
