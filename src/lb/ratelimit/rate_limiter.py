"""Token bucket rate limiting, keyed per client IP.

Why token bucket specifically (over e.g. fixed-window or sliding-log
counters): it naturally allows short bursts up to `capacity` while still
enforcing a steady-state average rate of `refill_rate` tokens/second, which
matches how real client traffic behaves (bursty, not perfectly uniform).
Fixed-window counters have a well-known edge-of-window doubling problem
(2x the intended rate right at a window boundary); token bucket doesn't.

Design decision: refill is computed lazily on each check ("as needed"),
rather than via a background task ticking every bucket on a timer. With
potentially many thousands of distinct client IPs, running a timer per
bucket (or even one global timer that walks every bucket) wastes CPU on
buckets nobody is currently using. Lazy refill means a bucket that hasn't
been touched in an hour costs nothing until the next request from that
client, at which point we compute "how many tokens would have accumulated
over that elapsed time" in one arithmetic step.
"""
from __future__ import annotations

import threading
import time


class TokenBucket:
    """A single client's token bucket."""

    def __init__(self, capacity: float, refill_rate: float) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate  # tokens per second
        self._tokens = capacity
        self._last_refill = time.monotonic()

    def try_consume(self, tokens: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now

        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False


class RateLimiter:
    """Keeps one TokenBucket per client key (typically client IP).

    Design decision: buckets are created lazily on first use and never
    proactively evicted in this phase -- for a resume-scale project this is
    an acceptable simplification, but it's a real memory-growth concern
    in production with many unique/spoofable client IPs (a classic DoS
    vector against the rate limiter itself). Flagged here and in the
    README as a known future improvement (e.g. an LRU cap or a periodic
    sweep of buckets untouched for N minutes).
    """

    def __init__(self, requests_per_second: float, burst: int) -> None:
        self._rate = requests_per_second
        self._burst = burst
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, client_key: str) -> bool:
        with self._lock:
            bucket = self._buckets.get(client_key)
            if bucket is None:
                bucket = TokenBucket(capacity=self._burst, refill_rate=self._rate)
                self._buckets[client_key] = bucket
            return bucket.try_consume()

    @property
    def tracked_clients(self) -> int:
        return len(self._buckets)
