"""Active health checking: a background asyncio task that periodically
probes every configured backend on a fixed interval, independent of real
client traffic.

Why we need this *in addition to* passive checking (Backend.record_failure
called from the proxy on real request failures): passive checking only
reacts when traffic is actually flowing to a backend. If a backend goes
down, gets marked dead, and receives zero further traffic (because the
balancer correctly stops routing to it), passive checking alone has no way
to ever notice it recovered -- nothing is testing it anymore. Active
checking closes that loop: it keeps probing dead backends on a schedule
so they can automatically rejoin the pool once they're healthy again,
with no dependency on production traffic patterns.

Design decisions:
  - A single shared aiohttp ClientSession is used for all probes (created
    once, reused every cycle) for the same connection-reuse reasons as the
    main proxy's ClientSession.
  - Each backend is probed concurrently (via asyncio.gather), not
    sequentially -- with N backends and a probe timeout of T seconds,
    sequential probing would take up to N*T seconds per cycle, which
    could exceed the check interval itself for a large fleet.
  - A backend flips to unhealthy after `unhealthy_threshold` consecutive
    *active-probe* failures (separate counter from the passive one on
    Backend, since this loop shouldn't be reset by unrelated production
    traffic outcomes) and back to healthy after `healthy_threshold`
    consecutive successes -- requiring more than one success before
    trusting recovery avoids flapping a backend that's still warming up.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientSession, ClientTimeout

from lb.backend import Backend


class ActiveHealthChecker:
    def __init__(
        self,
        backends: list[Backend],
        logger: logging.Logger,
        path: str = "/",
        interval_seconds: float = 5.0,
        timeout_seconds: float = 2.0,
        unhealthy_threshold: int = 3,
        healthy_threshold: int = 2,
    ) -> None:
        self._backends = backends
        self._log = logger
        self._path = path
        self._interval = interval_seconds
        self._timeout = ClientTimeout(total=timeout_seconds)
        self._unhealthy_threshold = unhealthy_threshold
        self._healthy_threshold = healthy_threshold

        # Per-backend active-probe streak counters, deliberately separate
        # from Backend._consecutive_failures (which tracks passive,
        # real-traffic failures). Keyed by id() like the balancer's
        # per-backend scheduling state.
        self._probe_failure_streak: dict[int, int] = {id(b): 0 for b in backends}
        self._probe_success_streak: dict[int, int] = {id(b): 0 for b in backends}

        self._session: ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        self._session = ClientSession(timeout=self._timeout)
        self._task = asyncio.ensure_future(self._run_loop())
        self._log.info(
            "active health checker started",
            extra={
                "interval_seconds": self._interval,
                "path": self._path,
                "backend_count": len(self._backends),
            },
        )

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session is not None:
            await self._session.close()

    async def _run_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                await self._check_all_once()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise

    async def _check_all_once(self) -> None:
        if self._session is None:
            # Lazily created so this method also works standalone (e.g. in
            # tests, or a future "run one check pass" CLI) without going
            # through the full start()/stop() background-task lifecycle.
            self._session = ClientSession(timeout=self._timeout)
        await asyncio.gather(*(self._check_one(b) for b in self._backends))

    async def _check_one(self, backend: Backend) -> None:
        assert self._session is not None
        url = f"{backend.scheme}://{backend.host}:{backend.port}{self._path}"
        key = id(backend)

        try:
            async with self._session.get(url) as response:
                healthy = response.status < 500
        except Exception:  # noqa: BLE001 - any network failure counts as a failed probe
            healthy = False

        if healthy:
            self._probe_success_streak[key] += 1
            self._probe_failure_streak[key] = 0
            if (
                not backend.is_alive
                and self._probe_success_streak[key] >= self._healthy_threshold
            ):
                backend.set_alive(True)
                self._log.info(
                    "backend recovered, marking alive",
                    extra={"backend": backend.url},
                )
        else:
            self._probe_failure_streak[key] += 1
            self._probe_success_streak[key] = 0
            if (
                backend.is_alive
                and self._probe_failure_streak[key] >= self._unhealthy_threshold
            ):
                backend.set_alive(False)
                self._log.warning(
                    "backend failed active health check, marking dead",
                    extra={
                        "backend": backend.url,
                        "consecutive_probe_failures": self._probe_failure_streak[key],
                    },
                )
