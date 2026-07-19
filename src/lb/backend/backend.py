"""The Backend type: one upstream server plus its mutable runtime state.

Design decision: this is kept separate from config.BackendConfig on purpose.
BackendConfig is the static, declared topology read once at startup;
Backend is the mutable runtime truth that health checks and connection
tracking update continuously while the process is running.

Concurrency note: unlike the Go version (which needs a mutex because
goroutines can run on separate OS threads simultaneously), a single asyncio
event loop only ever runs one coroutine at a time -- code between `await`
points can't be interleaved. So a plain attribute read/write here is
already atomic *as long as no `await` happens in between*. We still keep
state changes explicit single-statement operations (no read-modify-write
spanning an await) so this remains true as the class grows.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse


class Backend:
    """Represents one upstream server the load balancer can route to."""

    def __init__(self, url: str, weight: int = 1) -> None:
        # Parse eagerly so a malformed backend URL fails fast at startup,
        # rather than causing a confusing error mid-request later.
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"invalid backend url: {url!r}")

        self.url = url
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.weight = weight

        # Optimistic default: assume healthy until a health check says
        # otherwise.
        self._alive = True

        # Active connection count, incremented by the proxy layer when a
        # request is dispatched to this backend and decremented when the
        # response finishes. Read by Power of Two Choices and Least
        # Connections to compare backend load.
        self._active_connections = 0

        # --- Passive health-check state ---
        # Consecutive failures observed on *real production traffic* (as
        # opposed to the active health checker's separate synthetic probes).
        # A run of failures here marks the backend dead immediately, without
        # waiting for the next scheduled active probe -- this is what makes
        # passive checking faster to react than active checking alone.
        self._consecutive_failures = 0
        self._last_state_change: float = time.monotonic()

    @property
    def is_alive(self) -> bool:
        return self._alive

    def set_alive(self, alive: bool) -> None:
        if alive != self._alive:
            self._last_state_change = time.monotonic()
        self._alive = alive
        if alive:
            # Recovering (whether via active probe success or passive
            # traffic success) always clears the passive failure streak, so
            # a backend that comes back doesn't immediately get re-marked
            # dead on some stale failure count.
            self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def seconds_since_last_state_change(self) -> float:
        return time.monotonic() - self._last_state_change

    def record_success(self) -> None:
        """Called by the proxy layer after a request to this backend
        completes successfully. Resets the passive failure streak -- a
        single success is enough to forgive prior isolated failures,
        avoiding flapping a backend dead over one blip.
        """
        self._consecutive_failures = 0

    def record_failure(self) -> int:
        """Called by the proxy layer after a request to this backend fails
        (connection error, timeout, or 5xx, depending on proxy policy).
        Returns the updated consecutive-failure count so the caller can
        decide whether to trip the backend to unhealthy.
        """
        self._consecutive_failures += 1
        return self._consecutive_failures

    @property
    def active_connections(self) -> int:
        return self._active_connections

    def increment_connections(self) -> None:
        self._active_connections += 1

    def decrement_connections(self) -> None:
        # Clamped at 0 as a defensive guard: a decrement should never be
        # able to make the counter negative, even if a future bug calls it
        # without a matching increment.
        self._active_connections = max(0, self._active_connections - 1)

    def __repr__(self) -> str:
        return (
            f"Backend(url={self.url!r}, weight={self.weight}, "
            f"alive={self._alive}, active_connections={self._active_connections}, "
            f"consecutive_failures={self._consecutive_failures})"
        )

    def __str__(self) -> str:
        return self.url
