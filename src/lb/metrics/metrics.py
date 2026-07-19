"""Prometheus metrics for the load balancer.

Design decision: we use the official `prometheus_client` library rather
than hand-rolling metric collection and text-format serialization. This is
one of the deliberate exceptions to "prefer stdlib/hand-rolled" elsewhere in
this project -- Prometheus's exposition format has specific escaping and
type-annotation rules, and reimplementing it would be pure risk for zero
learning value, whereas the balancer algorithms and reverse-proxy plumbing
are the actual point of this project.

Metric naming follows Prometheus convention: `<namespace>_<subsystem>_<name>_<unit>`.
We use namespace "lb" throughout.
"""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class Metrics:
    """Holds all Prometheus collectors for one load balancer process.

    A dedicated CollectorRegistry (rather than the global default registry)
    is used so multiple ProxyHandler/Metrics instances can coexist safely
    within the same process -- this matters for tests, where each test
    constructs a fresh app and would otherwise collide on duplicate metric
    registration in the shared default registry.
    """

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.requests_total = Counter(
            "lb_requests_total",
            "Total requests processed, labeled by backend and outcome status",
            ["backend", "status"],
            registry=self.registry,
        )

        self.request_duration_seconds = Histogram(
            "lb_request_duration_seconds",
            "Time to get a full response from the backend, per backend",
            ["backend"],
            registry=self.registry,
        )

        self.backend_up = Gauge(
            "lb_backend_up",
            "1 if the backend is currently considered healthy, else 0",
            ["backend"],
            registry=self.registry,
        )

        self.backend_active_connections = Gauge(
            "lb_backend_active_connections",
            "Current number of in-flight requests to this backend",
            ["backend"],
            registry=self.registry,
        )

        self.circuit_breaker_state = Gauge(
            "lb_circuit_breaker_state",
            "Circuit breaker state per backend: 0=closed, 1=half_open, 2=open",
            ["backend"],
            registry=self.registry,
        )

        self.rate_limit_rejections_total = Counter(
            "lb_rate_limit_rejections_total",
            "Total requests rejected by the rate limiter",
            registry=self.registry,
        )

        self.retries_total = Counter(
            "lb_retries_total",
            "Total retry attempts made, labeled by backend that failed",
            ["backend"],
            registry=self.registry,
        )

        self.backends_total = Gauge(
            "lb_backends_total",
            "Total number of backends currently registered (for autoscaling visibility)",
            registry=self.registry,
        )

    def render(self) -> bytes:
        """Serializes all current metric values in Prometheus text format."""
        return generate_latest(self.registry)


CONTENT_TYPE = CONTENT_TYPE_LATEST
