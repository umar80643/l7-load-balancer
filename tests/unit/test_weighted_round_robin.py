from lb.backend import Backend
from lb.balancer import Balancer, WeightedRoundRobin

FAKE_REQUEST = None


def make_backend(url: str, weight: int = 1) -> Backend:
    return Backend(url, weight=weight)


def test_satisfies_balancer_protocol():
    wrr = WeightedRoundRobin([make_backend("http://localhost:9001")])
    assert isinstance(wrr, Balancer)


def test_name_property():
    wrr = WeightedRoundRobin([])
    assert wrr.name == "weighted_round_robin"


def test_empty_backend_list_returns_none():
    wrr = WeightedRoundRobin([])
    assert wrr.next_backend(FAKE_REQUEST) is None


def test_all_dead_returns_none():
    b = make_backend("http://localhost:9001")
    b.set_alive(False)
    wrr = WeightedRoundRobin([b])
    assert wrr.next_backend(FAKE_REQUEST) is None


def test_equal_weights_behaves_like_round_robin():
    backends = [make_backend(f"http://localhost:900{i}", weight=1) for i in (1, 2, 3)]
    wrr = WeightedRoundRobin(backends)

    counts = {b.url: 0 for b in backends}
    for _ in range(30):
        counts[wrr.next_backend(FAKE_REQUEST).url] += 1

    assert all(c == 10 for c in counts.values())


def test_distribution_matches_weight_ratio_exactly_over_one_period():
    # Classic smooth-WRR property: over exactly sum(weights) selections,
    # each backend is chosen precisely `weight` times -- not approximately,
    # exactly. Weights 5:1:1 -> period of 7 selections.
    a = make_backend("http://localhost:9001", weight=5)
    b = make_backend("http://localhost:9002", weight=1)
    c = make_backend("http://localhost:9003", weight=1)
    wrr = WeightedRoundRobin([a, b, c])

    counts = {a.url: 0, b.url: 0, c.url: 0}
    period = a.weight + b.weight + c.weight  # 7
    for _ in range(period):
        counts[wrr.next_backend(FAKE_REQUEST).url] += 1

    assert counts[a.url] == 5
    assert counts[b.url] == 1
    assert counts[c.url] == 1


def test_distribution_is_smooth_not_bursty():
    # With weights 5:1:1, naive expansion (A A A A A B C) puts all five A's
    # in one contiguous block. The smooth algorithm interleaves B and C in
    # among the A's within every period (verified as "A A B A C A A" here),
    # which is what the exact-ratio test above already confirms structurally.
    # Consecutive runs of A *can* still reach 4 at a period boundary (the
    # tail of one period touching the head of the next: "...A A | A A..."),
    # but never all 5 in one unbroken block the way naive expansion would --
    # that's the concrete, checkable difference between "smooth" and "naive".
    a = make_backend("http://localhost:9001", weight=5)
    b = make_backend("http://localhost:9002", weight=1)
    c = make_backend("http://localhost:9003", weight=1)
    wrr = WeightedRoundRobin([a, b, c])

    sequence = [wrr.next_backend(FAKE_REQUEST).url for _ in range(21)]  # three full periods

    max_consecutive_a = 0
    current_run = 0
    for url in sequence:
        if url == a.url:
            current_run += 1
            max_consecutive_a = max(max_consecutive_a, current_run)
        else:
            current_run = 0

    assert max_consecutive_a < 5, (
        f"expected fewer than 5 consecutive A's (naive-expansion worst case), got sequence {sequence}"
    )

    # And confirm the first period is genuinely interleaved, not the naive
    # "all A's first, then B, then C" expansion pattern.
    naive_expansion = [a.url] * 5 + [b.url] + [c.url]
    assert sequence[:7] != naive_expansion, "expected interleaved distribution, got naive burst pattern"


def test_skips_dead_backend_and_redistributes():
    a = make_backend("http://localhost:9001", weight=1)
    b = make_backend("http://localhost:9002", weight=1)
    b.set_alive(False)
    wrr = WeightedRoundRobin([a, b])

    for _ in range(5):
        assert wrr.next_backend(FAKE_REQUEST) is a
