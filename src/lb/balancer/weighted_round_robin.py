"""Weighted Round Robin balancer using the "smooth weighted round-robin"
algorithm (the same one Nginx and LVS use internally).

Why "smooth" instead of naive expansion: a naive weighted RR expands the
backend list by weight (e.g. weights 5:1:1 becomes [A,A,A,A,A,B,C]) and
cycles through it. That produces bursts -- backend A receives 5 consecutive
requests, then B, then C -- which is bad for connection reuse and can
transiently overload A's connection pool even though its average share is
correct. The smooth algorithm below produces the same 5:1:1 long-run ratio
but interleaves selections evenly across time (e.g. A B A C A A A).

Algorithm (per selection):
  1. For every alive backend, add its static weight to a running
     "current_weight" counter.
  2. Pick the backend with the highest current_weight.
  3. Subtract the sum of all alive weights from the winner's current_weight.
This is a well-known, provably-fair scheduling algorithm; each backend ends
up selected exactly `weight` times out of every `sum(weights)` selections.
"""
from __future__ import annotations

import threading

from aiohttp.web import Request

from lb.backend import Backend


class WeightedRoundRobin:
    def __init__(self, backends: list[Backend]) -> None:
        self._backends = backends
        # current_weight is per-algorithm scheduling state, not backend
        # health/identity -- it belongs to the balancer, not the Backend
        # class. Keyed by object identity (id()) since Backend doesn't
        # define custom equality/hash. We deliberately read via .get(..., 0)
        # rather than direct indexing: a backend added dynamically after
        # construction (by the autoscaling admin API) won't have a
        # pre-seeded entry, and .get() lets it start at weight 0 gracefully
        # instead of raising KeyError.
        self._current_weights: dict[int, int] = {id(b): 0 for b in backends}
        self._lock = threading.Lock()

    def next_backend(self, request: Request) -> Backend | None:
        with self._lock:
            alive = [b for b in self._backends if b.is_alive]
            if not alive:
                return None

            total_weight = sum(b.weight for b in alive)
            selected: Backend | None = None
            selected_weight = None

            for b in alive:
                new_weight = self._current_weights.get(id(b), 0) + b.weight
                self._current_weights[id(b)] = new_weight
                if selected is None or new_weight > selected_weight:
                    selected = b
                    selected_weight = new_weight

            assert selected is not None  # alive is non-empty, so this always fires
            self._current_weights[id(selected)] -= total_weight
            return selected

    @property
    def name(self) -> str:
        return "weighted_round_robin"
