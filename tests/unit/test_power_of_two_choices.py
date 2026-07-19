from lb.backend import Backend
from lb.balancer import Balancer, PowerOfTwoChoices

FAKE_REQUEST = None


def make_backend(url: str) -> Backend:
    return Backend(url, weight=1)


def test_satisfies_balancer_protocol():
    p2c = PowerOfTwoChoices([make_backend("http://localhost:9001")])
    assert isinstance(p2c, Balancer)


def test_name_property():
    p2c = PowerOfTwoChoices([])
    assert p2c.name == "power_of_two_choices"


def test_empty_backend_list_returns_none():
    p2c = PowerOfTwoChoices([])
    assert p2c.next_backend(FAKE_REQUEST) is None


def test_all_dead_returns_none():
    b = make_backend("http://localhost:9001")
    b.set_alive(False)
    p2c = PowerOfTwoChoices([b])
    assert p2c.next_backend(FAKE_REQUEST) is None


def test_single_alive_backend_returned_directly():
    b = make_backend("http://localhost:9001")
    p2c = PowerOfTwoChoices([b])
    assert p2c.next_backend(FAKE_REQUEST) is b


def test_prefers_backend_with_fewer_active_connections():
    busy = make_backend("http://localhost:9001")
    idle = make_backend("http://localhost:9002")
    for _ in range(10):
        busy.increment_connections()

    p2c = PowerOfTwoChoices([busy, idle])

    # With only 2 backends, P2C always samples both, so the idle one must
    # always win regardless of random sampling order.
    for _ in range(20):
        assert p2c.next_backend(FAKE_REQUEST) is idle


def test_only_selects_alive_backends():
    alive = make_backend("http://localhost:9001")
    dead = make_backend("http://localhost:9002")
    dead.set_alive(False)
    p2c = PowerOfTwoChoices([alive, dead])

    for _ in range(20):
        assert p2c.next_backend(FAKE_REQUEST) is alive


def test_equal_load_distributes_across_backends():
    backends = [make_backend(f"http://localhost:900{i}") for i in (1, 2, 3)]
    p2c = PowerOfTwoChoices(backends)

    seen = set()
    for _ in range(200):
        seen.add(p2c.next_backend(FAKE_REQUEST).url)

    assert seen == {b.url for b in backends}
