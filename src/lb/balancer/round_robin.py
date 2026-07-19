"""Round Robin balancer: cycles through backends in order 0, 1, 2, 0, 1, 2..."""
from __future__ import annotations

import itertools
import threading

from aiohttp.web import Request

from lb.backend import Backend


class RoundRobin:
    """Cycles through backends in order, skipping unhealthy ones.

    Implementation notes:
      - We use `itertools.count()` behind a lock rather than a bare Python
        int with `+= 1`. Even though asyncio itself is single-threaded, we
        use a `threading.Lock` here (not an asyncio.Lock) deliberately: this
        method contains no `await`, so it never yields control, and a plain
        Lock is cheaper than an asyncio.Lock for a critical section that
        never suspends. This also makes the class safe to reuse unmodified
        if a future phase runs balancers under `run_in_executor` or multiple
        worker processes share state via a lock-based structure.
      - We skip backends where `is_alive` is False. In Phase 1 this never
        triggers (nothing marks a backend unhealthy yet), but wiring it in
        now means Phase 3's health checker "just works" with zero changes
        here.
      - If every backend is unhealthy, `next_backend` returns None. The
        proxy layer must handle this (e.g. respond 503).
    """

    def __init__(self, backends: list[Backend]) -> None:
        self._backends = backends
        self._counter = itertools.count()
        self._lock = threading.Lock()

    def next_backend(self, request: Request) -> Backend | None:
        n = len(self._backends)
        if n == 0:
            return None

        # Bounded to n attempts so a fully-dead pool can't loop forever.
        for _ in range(n):
            with self._lock:
                idx = next(self._counter) % n
            candidate = self._backends[idx]
            if candidate.is_alive:
                return candidate

        return None

    @property
    def name(self) -> str:
        return "round_robin"
