"""Power of Two Choices (P2C) balancer: samples two backends at random and
routes to whichever has fewer active connections.

Why not just scan all backends for the true minimum ("least connections")?
That's O(n) per request and, more subtly, under concurrent load many
requests can all observe the same "least loaded" backend simultaneously and
all pile onto it before its counter updates -- a herd effect. Sampling just
two candidates and picking the better one is O(1), avoids the herd effect
(different requests are likely to sample different pairs), and is
provably close to true least-connections in the resulting balance quality.
This is the algorithm modern L7 proxies (e.g. Envoy) default to for exactly
these reasons.

This reads Backend.active_connections, which the proxy layer maintains by
calling increment_connections()/decrement_connections() around each
dispatched request (see lb.proxy.proxy.ProxyHandler.handle). The balancer
itself never mutates connection counts -- it only observes them.
"""
from __future__ import annotations

import random

from aiohttp.web import Request

from lb.backend import Backend


class PowerOfTwoChoices:
    def __init__(self, backends: list[Backend]) -> None:
        self._backends = backends

    def next_backend(self, request: Request) -> Backend | None:
        alive = [b for b in self._backends if b.is_alive]
        if not alive:
            return None
        if len(alive) == 1:
            return alive[0]

        candidate_a, candidate_b = random.sample(alive, 2)
        if candidate_a.active_connections <= candidate_b.active_connections:
            return candidate_a
        return candidate_b

    @property
    def name(self) -> str:
        return "power_of_two_choices"
