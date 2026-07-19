"""The reverse proxy: an aiohttp request handler that asks a Balancer which
backend to use, forwards the request there, and streams the response back.

This module also owns several cross-cutting request-dispatch concerns that
only make sense at the point where we're actually talking to a specific
backend: circuit breaking, retries with backoff, passive health tracking,
and sticky sessions. Rate limiting is deliberately NOT here -- it's applied
as an aiohttp middleware in front of this handler (see lb.ratelimit and
main.create_app), because it's a per-client-request concern that doesn't
need to know which backend will eventually be chosen, so keeping it
separate follows the Single Responsibility Principle.

Design decision: aiohttp has no built-in "reverse proxy" helper (unlike
Go's net/http/httputil.ReverseProxy), so forwarding is implemented
explicitly here using aiohttp.ClientSession. Two choices matter for
production correctness:

1. A single ClientSession is created once and reused for the lifetime of
   the process (not one per request), for connection pooling / keep-alive.

2. Hop-by-hop headers (RFC 7230 section 6.1) are stripped in both
   directions -- Go's ReverseProxy does this for you; in aiohttp we do it
   ourselves.

Retry/streaming interaction: once we start streaming an upstream response
back to the client, we can't "undo" that and try a different backend. So
the retry-or-not decision is made by inspecting the upstream response
*status* before any bytes are written to the client -- see _dispatch below.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time

from aiohttp import ClientConnectorError, ClientSession, ClientTimeout
from aiohttp.web import Request, Response, StreamResponse

from lb.backend import Backend
from lb.balancer import Balancer
from lb.circuitbreaker import CircuitBreaker, CircuitState
from lb.metrics import Metrics

_CIRCUIT_STATE_METRIC_VALUE = {
    CircuitState.CLOSED: 0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN: 2,
}

# RFC 7230 section 6.1 hop-by-hop headers -- stripped in both directions.
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

# Only these methods are safe to automatically retry against a different
# backend: they're defined as idempotent by RFC 7231, so re-executing them
# after a failure (where we can't be sure whether the first attempt's
# effects landed or not) doesn't risk duplicating a side effect. POST, PUT
# (arguably idempotent, but commonly used non-idempotently in practice),
# PATCH, and DELETE are deliberately excluded from automatic retry by
# default -- retrying a POST that already succeeded server-side but timed
# out on the response could double-charge a customer, double-send an
# email, etc. This is a conservative, deliberate safety choice.
_DEFAULT_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Upstream status codes that indicate the backend itself is unhealthy or
# overloaded (as opposed to a legitimate application-level response like
# 404 or 401), and are therefore worth retrying against a different
# backend rather than treated as "the answer".
_DEFAULT_RETRYABLE_STATUSES = frozenset({502, 503, 504})


def _filtered_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}


def _sticky_id_for(backend: Backend) -> str:
    """A short, stable identifier for a backend, safe to put in a cookie.
    Derived from the backend's URL via SHA-256 (not Python's hash(), which
    is randomized per-process and would break stickiness across restarts).
    """
    return hashlib.sha256(backend.url.encode("utf-8")).hexdigest()[:16]


class ProxyHandler:
    """aiohttp request handler that load-balances across backends, with
    circuit breaking, retries, passive health tracking, and optional
    sticky sessions.
    """

    def __init__(
        self,
        balancer: Balancer,
        backends: list[Backend],
        logger: logging.Logger,
        client_timeout_seconds: float = 30.0,
        max_retries: int = 2,
        retry_base_delay_seconds: float = 0.1,
        retry_max_delay_seconds: float = 2.0,
        retryable_methods: frozenset[str] = _DEFAULT_RETRYABLE_METHODS,
        retryable_statuses: frozenset[int] = _DEFAULT_RETRYABLE_STATUSES,
        passive_failure_threshold: int = 3,
        circuit_failure_threshold: int = 5,
        circuit_recovery_timeout_seconds: float = 30.0,
        sticky_sessions: bool = False,
        sticky_cookie_name: str = "LB_STICKY_BACKEND",
        sticky_cookie_max_age: int = 3600,
        metrics: Metrics | None = None,
    ) -> None:
        self._balancer = balancer
        self._backends = backends
        self._log = logger
        self._metrics = metrics
        self._timeout = ClientTimeout(total=client_timeout_seconds)

        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay_seconds
        self._retry_max_delay = retry_max_delay_seconds
        self._retryable_methods = retryable_methods
        self._retryable_statuses = retryable_statuses
        self._passive_failure_threshold = passive_failure_threshold

        self._sticky_sessions = sticky_sessions
        self._sticky_cookie_name = sticky_cookie_name
        self._sticky_cookie_max_age = sticky_cookie_max_age
        # Deliberately NOT pre-building a cookie-id -> Backend dict here:
        # that map would go stale the moment a backend is added or removed
        # at runtime (which the autoscaling admin API does). Sticky ID
        # lookups instead recompute on demand against the live
        # self._backends list -- see _find_backend_by_sticky_id below.

        self._circuit_failure_threshold = circuit_failure_threshold
        self._circuit_recovery_timeout = circuit_recovery_timeout_seconds
        self._breakers: dict[int, CircuitBreaker] = {
            id(b): CircuitBreaker(
                failure_threshold=circuit_failure_threshold,
                recovery_timeout_seconds=circuit_recovery_timeout_seconds,
            )
            for b in backends
        }

        # Created lazily in `startup()` rather than __init__, because
        # ClientSession must be created inside a running event loop.
        self._session: ClientSession | None = None

    async def startup(self) -> None:
        self._session = ClientSession(timeout=self._timeout)

    async def cleanup(self) -> None:
        if self._session is not None:
            await self._session.close()

    def _breaker_for(self, backend: Backend) -> CircuitBreaker:
        # setdefault so a backend added dynamically after startup (e.g. by
        # the admin/autoscaling API) still gets a breaker on first use,
        # rather than raising a KeyError.
        key = id(backend)
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(
                failure_threshold=self._circuit_failure_threshold,
                recovery_timeout_seconds=self._circuit_recovery_timeout,
            )
        return self._breakers[key]

    def get_breaker_state(self, backend: Backend) -> CircuitState:
        """Public read-only accessor used by the metrics snapshot loop
        (see lb.main) to report circuit state per backend as a gauge.
        """
        return self._breaker_for(backend).state

    def _find_backend_by_sticky_id(self, sticky_id: str) -> Backend | None:
        for b in self._backends:
            if _sticky_id_for(b) == sticky_id:
                return b
        return None

    def _pick_backend(self, request: Request, excluded: set[int]) -> Backend | None:
        """Selects a backend for this attempt.

        On the very first attempt (excluded is empty) with sticky sessions
        enabled, honors an existing sticky cookie if it points at a
        backend that's alive and whose circuit isn't open. Otherwise (or
        on any retry attempt), falls through to the configured Balancer,
        skipping any backend already tried this request (`excluded`) or
        whose circuit breaker currently rejects new requests.
        """
        if self._sticky_sessions and not excluded:
            cookie_val = request.cookies.get(self._sticky_cookie_name)
            if cookie_val:
                candidate = self._find_backend_by_sticky_id(cookie_val)
                if (
                    candidate is not None
                    and candidate.is_alive
                    and self._breaker_for(candidate).allow_request()
                ):
                    return candidate
                # Sticky target unavailable -- fall through to normal
                # balancing rather than failing the request outright.

        # Bounded attempts to skip past excluded/open-circuit candidates.
        # Bounded by len(backends)+1 so a fully-exhausted pool can't spin
        # forever asking the balancer for a backend that will never come.
        for _ in range(len(self._backends) + 1):
            candidate = self._balancer.next_backend(request)
            if candidate is None:
                return None
            if id(candidate) in excluded:
                continue
            if not self._breaker_for(candidate).allow_request():
                continue
            return candidate

        return None

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter. Jitter (a small random
        addition) prevents many simultaneously-retrying clients from all
        retrying at exactly the same moment and re-overwhelming a
        recovering backend in lockstep (the "thundering herd" problem).
        """
        exponential = self._retry_base_delay * (2 ** (attempt - 1))
        capped = min(exponential, self._retry_max_delay)
        jitter = random.uniform(0, capped * 0.1)
        return capped + jitter

    async def _dispatch(
        self,
        request: Request,
        backend: Backend,
        body: bytes,
        is_final_attempt: bool,
    ) -> tuple[str, StreamResponse | None, int | None]:
        """Makes one attempt against `backend`. Returns a 3-tuple whose
        first element is one of:

          "success" -- a non-retryable-status response (2xx/3xx/4xx/other),
              fully streamed to the client already. This is a real
              transport+application success; backend health/circuit state
              should reflect success.

          "failure_final" -- the upstream responded with a retryable-status
              5xx (502/503/504) but this was our last allowed attempt, so
              we streamed it through anyway (nothing better to fall back
              to). This DOES still count as a failure for passive health
              and circuit-breaker bookkeeping, even though a response has
              already been sent to the client -- "we had to use it" is not
              the same as "it was healthy".

          "failure_retry" -- this attempt failed (retryable-status
              response, drained but NOT streamed; or a connection
              error/timeout). The caller should try a different backend.
              `response` is None in this case; `status` is the upstream
              status if we got one, else None.

        Whether a retryable-status response becomes "success" (never, by
        definition), "failure_final", or "failure_retry" depends on
        is_final_attempt. Connection errors/timeouts always become
        "failure_retry" and it's the caller's job to notice there are no
        more attempts left and synthesize a final response.
        """
        target_url = f"{backend.scheme}://{backend.host}:{backend.port}{request.path_qs}"
        assert self._session is not None, "ProxyHandler.startup() was not called"

        try:
            async with self._session.request(
                method=request.method,
                url=target_url,
                headers=_filtered_headers(request.headers),
                data=body,
                allow_redirects=False,
            ) as upstream_response:
                is_retryable_status = upstream_response.status in self._retryable_statuses

                if is_retryable_status and not is_final_attempt:
                    # Drain the body so aiohttp can consider the connection
                    # cleanly finished (and potentially reuse it), but do
                    # NOT write anything to the client -- we're about to
                    # try a different backend instead.
                    await upstream_response.read()
                    return "failure_retry", None, upstream_response.status

                response = Response(
                    status=upstream_response.status,
                    headers=_filtered_headers(upstream_response.headers),
                )
                # Sticky cookie must be set BEFORE response.prepare(), which
                # flushes headers to the client -- setting it any later (as
                # a post-hoc mutation after streaming) would silently do
                # nothing, since the header bytes are already gone. Only
                # attached on a genuine, non-retryable-status response --
                # we never want to stick a client to a backend that just
                # failed, even if we were forced to hand its response back.
                if self._sticky_sessions and not is_retryable_status:
                    response.set_cookie(
                        self._sticky_cookie_name,
                        _sticky_id_for(backend),
                        max_age=self._sticky_cookie_max_age,
                        httponly=True,
                        samesite="Lax",
                    )

                await response.prepare(request)
                async for chunk in upstream_response.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()

                outcome = "failure_final" if is_retryable_status else "success"
                return outcome, response, upstream_response.status

        except ClientConnectorError as exc:
            self._log.error(
                "failed to connect to backend",
                extra={"backend": backend.url, "error": str(exc)},
            )
            return "failure_retry", None, None

        except (TimeoutError, asyncio.TimeoutError):
            self._log.error("backend request timed out", extra={"backend": backend.url})
            return "failure_retry", None, None

    async def handle(self, request: Request) -> StreamResponse:
        retryable_method = request.method in self._retryable_methods
        excluded: set[int] = set()
        last_status: int | None = None
        attempt = 0

        # Read the body once and reuse it across retries. This buffers the
        # whole request body in memory, which is an accepted trade-off here
        # since retries are restricted to GET/HEAD/OPTIONS by default --
        # methods that essentially never carry a meaningful body.
        body = await request.read()

        while True:
            backend = self._pick_backend(request, excluded)
            if backend is None:
                if excluded:
                    # We DID try at least one backend this request; we've
                    # simply run out of alternatives to retry against. This
                    # is a different situation from "no backend was ever
                    # available" below, so it gets a different, more
                    # accurate response: the last real failure status if we
                    # had one, or 502 if every attempt was a connection
                    # error/timeout (no status to report).
                    if last_status is not None:
                        return Response(
                            status=last_status,
                            text=f"{last_status}: no further healthy backends to retry",
                        )
                    return Response(
                        status=502,
                        text="502 Bad Gateway: all retries exhausted (no further backends)",
                    )
                self._log.warning("no healthy backend available", extra={"path": request.path})
                return Response(
                    status=503,
                    text="503 Service Unavailable: no healthy backend available",
                )

            self._log.debug(
                "routing request",
                extra={
                    "method": request.method,
                    "path": request.path,
                    "backend": backend.url,
                    "algorithm": self._balancer.name,
                    "attempt": attempt,
                },
            )

            is_final_attempt = (not retryable_method) or (attempt >= self._max_retries)

            # Connection tracking around the whole attempt, success or
            # failure -- this is what gives Power of Two Choices and Least
            # Connections real, current load data to compare backends by.
            backend.increment_connections()
            start_time = time.monotonic()
            try:
                result, response, status = await self._dispatch(
                    request, backend, body, is_final_attempt
                )
            finally:
                backend.decrement_connections()
            elapsed = time.monotonic() - start_time

            if self._metrics is not None:
                self._metrics.request_duration_seconds.labels(backend=backend.url).observe(elapsed)

            if result == "success":
                backend.record_success()
                self._breaker_for(backend).record_success()
                if self._metrics is not None:
                    self._metrics.requests_total.labels(backend=backend.url, status=str(status)).inc()
                # Sticky cookie is set inside _dispatch, before the response
                # was prepared/streamed -- setting it here would be too
                # late, since headers are already flushed by this point.
                return response

            # --- failure paths: "failure_final" and "failure_retry" both
            # count as a failure for passive health and circuit-breaker
            # bookkeeping, even though only "failure_final" already has a
            # response object to hand back. ---
            self._breaker_for(backend).record_failure()
            failures = backend.record_failure()
            if failures >= self._passive_failure_threshold and backend.is_alive:
                backend.set_alive(False)
                self._log.warning(
                    "passive health check tripped backend to unhealthy",
                    extra={"backend": backend.url, "consecutive_failures": failures},
                )

            if self._metrics is not None:
                status_label = str(status) if status is not None else "connection_error"
                self._metrics.requests_total.labels(backend=backend.url, status=status_label).inc()

            if result == "failure_final":
                # _dispatch already streamed this response to the client
                # (it was the last attempt, so there was nothing better to
                # fall back to) -- just return it as-is.
                return response

            # result == "failure_retry": nothing has been sent to the
            # client yet. Record what we saw in case we run out of
            # backends to retry against, then loop to try again.
            last_status = status
            excluded.add(id(backend))

            if self._metrics is not None:
                self._metrics.retries_total.labels(backend=backend.url).inc()

            attempt += 1
            await asyncio.sleep(self._backoff_delay(attempt))
