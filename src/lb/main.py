"""Entrypoint for the L7 load balancer.

This file is deliberately "dumb": it only wires dependencies together (the
Composition Root pattern). No business logic lives here -- that keeps every
package in `lb/` independently testable without needing to spin up the
whole server process.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from aiohttp import web

from lb import admin
from lb import config as config_module
from lb.backend import Backend
from lb.balancer import (
    Balancer,
    IPHash,
    LeastConnections,
    PowerOfTwoChoices,
    RandomChoice,
    RoundRobin,
    WeightedRoundRobin,
)
from lb.circuitbreaker import CircuitState
from lb.healthcheck import ActiveHealthChecker
from lb.logging import new as new_logger
from lb.metrics import Metrics
from lb.middleware import make_rate_limit_middleware
from lb.proxy import ProxyHandler
from lb.ratelimit import RateLimiter

_ALGORITHMS = {
    "round_robin": lambda backends: RoundRobin(backends),
    "weighted_round_robin": lambda backends: WeightedRoundRobin(backends),
    "random": lambda backends: RandomChoice(backends),
    "ip_hash": lambda backends: IPHash(backends),
    "power_of_two_choices": lambda backends: PowerOfTwoChoices(backends),
    "least_connections": lambda backends: LeastConnections(backends),
}

_CIRCUIT_STATE_METRIC_VALUE = {
    CircuitState.CLOSED: 0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN: 2,
}


def build_backends(cfg: config_module.Config) -> list[Backend]:
    """Converts static config entries into runtime Backend instances."""
    return [Backend(bc.url, bc.weight) for bc in cfg.backends]


def build_balancer(algorithm: str, backends: list[Backend]) -> Balancer:
    """Selects and constructs a Balancer implementation by name. This is
    the single place that needs a new entry when a new algorithm is added.
    """
    try:
        factory = _ALGORITHMS[algorithm]
    except KeyError:
        raise ValueError(f"unknown algorithm {algorithm!r}") from None
    return factory(backends)


async def _metrics_snapshot_loop(
    backends: list[Backend],
    proxy_handler: ProxyHandler,
    metrics: Metrics,
    interval_seconds: float = 2.0,
) -> None:
    """Periodically snapshots current-state gauges (backend up/down, active
    connections, circuit breaker state, backend count) into Prometheus.

    Design decision: this runs as its own background loop rather than
    updating these gauges inline from business-logic code (e.g. inside
    Backend.set_alive or ProxyHandler.handle). That would scatter
    metrics-specific code throughout packages that otherwise have nothing
    to do with Prometheus. A periodic snapshot keeps all "current state"
    gauge updates in one place, at the cost of up to `interval_seconds` of
    staleness -- an acceptable trade-off for a dashboard, as opposed to the
    event-driven Counters (requests_total, retries_total, etc.), which
    update immediately at the point of occurrence since staleness there
    would actually lose data, not just delay a display.
    """
    try:
        while True:
            for b in backends:
                metrics.backend_up.labels(backend=b.url).set(1 if b.is_alive else 0)
                metrics.backend_active_connections.labels(backend=b.url).set(b.active_connections)
                metrics.circuit_breaker_state.labels(backend=b.url).set(
                    _CIRCUIT_STATE_METRIC_VALUE[proxy_handler.get_breaker_state(b)]
                )
            metrics.backends_total.set(len(backends))
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        raise


def create_app(cfg: config_module.Config, log_level: str = "info") -> web.Application:
    """Builds a fully-wired aiohttp Application. Exposed separately from
    main() so tests can construct the app without going through argv
    parsing.
    """
    logger = new_logger(level=log_level)

    backends = build_backends(cfg)
    balancer = build_balancer(cfg.algorithm, backends)
    metrics = Metrics() if cfg.metrics.enabled else None

    proxy_handler = ProxyHandler(
        balancer,
        backends,
        logger,
        client_timeout_seconds=cfg.client_timeout_seconds,
        max_retries=cfg.retry.max_retries,
        retry_base_delay_seconds=cfg.retry.base_delay_seconds,
        retry_max_delay_seconds=cfg.retry.max_delay_seconds,
        passive_failure_threshold=cfg.passive_health.failure_threshold,
        circuit_failure_threshold=cfg.circuit_breaker.failure_threshold,
        circuit_recovery_timeout_seconds=cfg.circuit_breaker.recovery_timeout_seconds,
        sticky_sessions=cfg.sticky_sessions.enabled,
        sticky_cookie_name=cfg.sticky_sessions.cookie_name,
        sticky_cookie_max_age=cfg.sticky_sessions.cookie_max_age_seconds,
        metrics=metrics,
    )

    middlewares = []
    rate_limiter = None
    if cfg.rate_limit.enabled:
        rate_limiter = RateLimiter(
            requests_per_second=cfg.rate_limit.requests_per_second,
            burst=cfg.rate_limit.burst,
        )
        middlewares.append(make_rate_limit_middleware(rate_limiter, logger, metrics=metrics))

    app = web.Application(middlewares=middlewares)

    if cfg.admin.enabled:
        admin.register(app, backends, logger, path_prefix=cfg.admin.path_prefix)

    if metrics is not None:
        async def metrics_handler(request: web.Request) -> web.Response:
            # prometheus_client's CONTENT_TYPE_LATEST is of the form
            # "text/plain; version=0.0.4; charset=utf-8" -- aiohttp's
            # Response rejects a content_type argument that itself contains
            # "charset=", since it manages the charset separately via its
            # own `charset` parameter. So we pass just the media type and
            # let aiohttp set charset=utf-8 itself.
            return web.Response(
                body=metrics.render(),
                content_type="text/plain",
                charset="utf-8",
            )

        app.router.add_get(cfg.metrics.path, metrics_handler)

    # Catch-all proxy route registered last so it doesn't shadow the more
    # specific /admin/* and /metrics routes above (aiohttp matches routes
    # in registration order for overlapping patterns).
    app.router.add_route("*", "/{tail:.*}", proxy_handler.handle)

    app.on_startup.append(lambda _: proxy_handler.startup())
    app.on_cleanup.append(lambda _: proxy_handler.cleanup())

    health_checker = None
    if cfg.health_check.enabled:
        health_checker = ActiveHealthChecker(
            backends,
            logger,
            path=cfg.health_check.path,
            interval_seconds=cfg.health_check.interval_seconds,
            timeout_seconds=cfg.health_check.timeout_seconds,
            unhealthy_threshold=cfg.health_check.unhealthy_threshold,
            healthy_threshold=cfg.health_check.healthy_threshold,
        )
        app.on_startup.append(lambda _: health_checker.start())
        app.on_cleanup.append(lambda _: health_checker.stop())

    if metrics is not None:
        metrics_task: asyncio.Task | None = None

        async def _start_metrics_loop(_app: web.Application) -> None:
            nonlocal metrics_task
            metrics_task = asyncio.ensure_future(
                _metrics_snapshot_loop(backends, proxy_handler, metrics)
            )

        async def _stop_metrics_loop(_app: web.Application) -> None:
            if metrics_task is not None:
                metrics_task.cancel()
                try:
                    await metrics_task
                except asyncio.CancelledError:
                    pass

        app.on_startup.append(_start_metrics_loop)
        app.on_cleanup.append(_stop_metrics_loop)

    logger.info(
        "load balancer configured",
        extra={
            "listen_host": cfg.listen_host,
            "listen_port": cfg.listen_port,
            "algorithm": balancer.name,
            "backend_count": len(backends),
            "health_check_enabled": cfg.health_check.enabled,
            "rate_limit_enabled": cfg.rate_limit.enabled,
            "sticky_sessions_enabled": cfg.sticky_sessions.enabled,
            "admin_api_enabled": cfg.admin.enabled,
            "metrics_enabled": cfg.metrics.enabled,
        },
    )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="L7 HTTP load balancer")
    parser.add_argument("--config", default="configs/config.json", help="path to JSON config file")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warn", "error"])
    args = parser.parse_args()

    try:
        cfg = config_module.load(args.config)
    except Exception as exc:  # noqa: BLE001 - top-level fatal startup error
        print(f"failed to load config: {exc}", file=sys.stderr)
        sys.exit(1)

    app = create_app(cfg, log_level=args.log_level)
    web.run_app(app, host=cfg.listen_host, port=cfg.listen_port, print=None)


if __name__ == "__main__":
    main()
