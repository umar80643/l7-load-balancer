"""Least Connections balancer: scans every alive backend and routes to
whichever currently has the fewest active connections.

This is the "ground truth" load-aware algorithm that Power of Two Choices
approximates cheaply. Least Connections is O(n) per request (a full scan),
which is fine at moderate backend counts but is exactly the cost P2C was
introduced to avoid at large scale -- the two algorithms are a deliberate
pair in this project, one exact and expensive, one approximate and cheap,
so a reader can compare them directly.

Ties are broken by picking the first minimum found (stable, deterministic
order) rather than randomly -- this keeps behavior easy to reason about
and test.
"""
from __future__ import annotations

from aiohttp.web import Request

from lb.backend import Backend


class LeastConnections:
    def __init__(self, backends: list[Backend]) -> None:
        self._backends = backends

    def next_backend(self, request: Request) -> Backend | None:
        alive = [b for b in self._backends if b.is_alive]
        if not alive:
            return None
        return min(alive, key=lambda b: b.active_connections)

    @property
    def name(self) -> str:
        return "least_connections"
