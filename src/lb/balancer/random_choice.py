"""Random balancer: picks uniformly among currently healthy backends.

Design decision: this ignores backend weight entirely (weighted random is a
reasonable variant, but WeightedRoundRobin already covers the
weight-aware case -- adding weighted random too would be a second way to
do the same job, which adds surface area without adding capability).

Why this is useful despite being "dumb": it requires zero shared state
between requests -- no counter, no lock contention, nothing to
resynchronize if you ever ran multiple load balancer processes side by
side. At high request volume, uniform random converges to a near-even
distribution by the law of large numbers, which is why several real
service meshes use it as a default.

Concurrency note: Python's `random` module keeps its state in a global,
process-wide instance and is documented as not thread-safe for use across
OS threads. That's not a concern here: our balancer methods never `await`,
so under asyncio's single-threaded cooperative scheduler nothing else can
run between the start and end of `random.choice()`.
"""
from __future__ import annotations

import random

from aiohttp.web import Request

from lb.backend import Backend


class RandomChoice:
    def __init__(self, backends: list[Backend]) -> None:
        self._backends = backends

    def next_backend(self, request: Request) -> Backend | None:
        alive = [b for b in self._backends if b.is_alive]
        if not alive:
            return None
        return random.choice(alive)

    @property
    def name(self) -> str:
        return "random"
