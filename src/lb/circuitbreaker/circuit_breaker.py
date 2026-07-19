"""A per-backend circuit breaker.

Design decision: this is a *distinct* mechanism from the passive health
checker, even though both react to failures, and they operate at different
timescales and for different reasons:

  - Passive health checking (lb.backend.Backend.record_failure /
    is_alive) answers "is this backend healthy enough to receive new
    traffic at all?" and recovery is gated by the active health checker's
    periodic probes.
  - The circuit breaker answers "have we been hammering a struggling
    backend with request after request, each paying a full timeout, and
    should we stop doing that for a bit to let it recover and to stop
    wasting our own client-side resources (connections, threads, retry
    budget) on calls that are very likely to fail anyway?"

A backend can be "alive" (passes health checks) but still have its circuit
open (its been erroring on real requests) -- the breaker is a faster,
finer-grained circuit specifically for request dispatch, not a
replacement for health checking.

States (the classic three-state design, e.g. as used in Hystrix/resilience4j):

    CLOSED --(failure_threshold consecutive failures)--> OPEN
    OPEN --(recovery_timeout elapses)--> HALF_OPEN
    HALF_OPEN --(trial request succeeds)--> CLOSED
    HALF_OPEN --(trial request fails)--> OPEN

In HALF_OPEN, only a single trial request is allowed through at a time
(gated by `_half_open_probe_in_flight`) -- letting a flood of requests all
retry the recovering backend simultaneously would defeat the purpose of
backing off.
"""
from __future__ import annotations

import enum
import threading
import time


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_seconds

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_probe_in_flight = False

        # A plain threading.Lock, not asyncio.Lock: every method here is
        # synchronous (no `await`), matching the same reasoning used for
        # the balancer implementations -- under asyncio's cooperative
        # scheduler nothing can interleave between lock acquire/release
        # anyway, but the lock keeps this class safe to reuse unmodified if
        # it's ever driven from multiple OS threads.
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    def _maybe_transition_to_half_open(self) -> None:
        """Must be called with the lock held. Moves OPEN -> HALF_OPEN once
        the recovery timeout has elapsed, allowing exactly one trial
        request through.
        """
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_probe_in_flight = False

    def allow_request(self) -> bool:
        """Returns True if a request should be allowed through right now.

        In HALF_OPEN, only the first caller gets True (the trial probe);
        subsequent callers get False until that probe resolves, to avoid a
        thundering herd hitting a backend that's still recovering.
        """
        with self._lock:
            self._maybe_transition_to_half_open()

            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                return False

            # HALF_OPEN: allow exactly one in-flight probe at a time.
            if self._half_open_probe_in_flight:
                return False
            self._half_open_probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._half_open_probe_in_flight = False
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._half_open_probe_in_flight = False

            if self._state == CircuitState.HALF_OPEN:
                # The trial probe failed -- back to fully OPEN, and reset
                # the recovery timer so we wait the full timeout again.
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                return

            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
