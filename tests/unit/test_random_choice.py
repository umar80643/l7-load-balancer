from lb.backend import Backend
from lb.balancer import Balancer, RandomChoice

FAKE_REQUEST = None


def make_backend(url: str) -> Backend:
    return Backend(url, weight=1)


def test_satisfies_balancer_protocol():
    rc = RandomChoice([make_backend("http://localhost:9001")])
    assert isinstance(rc, Balancer)


def test_name_property():
    rc = RandomChoice([])
    assert rc.name == "random"


def test_empty_backend_list_returns_none():
    rc = RandomChoice([])
    assert rc.next_backend(FAKE_REQUEST) is None


def test_all_dead_returns_none():
    b = make_backend("http://localhost:9001")
    b.set_alive(False)
    rc = RandomChoice([b])
    assert rc.next_backend(FAKE_REQUEST) is None


def test_only_selects_alive_backends():
    alive = make_backend("http://localhost:9001")
    dead = make_backend("http://localhost:9002")
    dead.set_alive(False)
    rc = RandomChoice([alive, dead])

    for _ in range(50):
        assert rc.next_backend(FAKE_REQUEST) is alive


def test_distribution_hits_all_backends_over_many_trials():
    backends = [make_backend(f"http://localhost:900{i}") for i in (1, 2, 3)]
    rc = RandomChoice(backends)

    seen = set()
    for _ in range(500):
        seen.add(rc.next_backend(FAKE_REQUEST).url)

    assert seen == {b.url for b in backends}
