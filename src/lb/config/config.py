"""Configuration schema and loading for the load balancer.

Design decision: we use Pydantic models rather than hand-rolled dict
parsing. Pydantic gives us declarative validation, automatic type
coercion, and readable error messages for free.

Config is loaded once at startup and treated as immutable ("declared
intent"). Runtime state that changes while the process is running (is a
backend healthy right now? how many active connections does it have? is a
circuit open?) lives in lb.backend / lb.circuitbreaker instead, kept
deliberately separate.

Each feature area (health checking, circuit breaking, retries, rate
limiting, sticky sessions) gets its own small nested model rather than one
flat namespace -- this keeps the JSON file self-documenting (the nesting
itself communicates "these settings belong together") and keeps each
model's validation logic scoped to just the fields it owns.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

SUPPORTED_ALGORITHMS = {
    "round_robin",
    "weighted_round_robin",
    "random",
    "ip_hash",
    "power_of_two_choices",
    "least_connections",
}


class BackendConfig(BaseModel):
    """Static description of one upstream server."""

    url: str
    weight: int = Field(default=1, ge=0)

    @field_validator("url")
    @classmethod
    def url_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("backend url must not be empty")
        return v

    @field_validator("weight")
    @classmethod
    def default_zero_weight_to_one(cls, v: int) -> int:
        return v if v > 0 else 1


class HealthCheckConfig(BaseModel):
    """Active health-check probing settings."""

    enabled: bool = True
    path: str = "/"
    interval_seconds: float = Field(default=5.0, gt=0)
    timeout_seconds: float = Field(default=2.0, gt=0)
    unhealthy_threshold: int = Field(default=3, ge=1)
    healthy_threshold: int = Field(default=2, ge=1)


class PassiveHealthConfig(BaseModel):
    """Passive (real-traffic-driven) health-check settings."""

    failure_threshold: int = Field(default=3, ge=1)


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = Field(default=5, ge=1)
    recovery_timeout_seconds: float = Field(default=30.0, gt=0)


class RetryConfig(BaseModel):
    max_retries: int = Field(default=2, ge=0)
    base_delay_seconds: float = Field(default=0.1, ge=0)
    max_delay_seconds: float = Field(default=2.0, ge=0)


class RateLimitConfig(BaseModel):
    enabled: bool = False
    requests_per_second: float = Field(default=100.0, gt=0)
    burst: int = Field(default=200, ge=1)


class StickySessionConfig(BaseModel):
    enabled: bool = False
    cookie_name: str = "LB_STICKY_BACKEND"
    cookie_max_age_seconds: int = Field(default=3600, ge=1)


class MetricsConfig(BaseModel):
    enabled: bool = True
    path: str = "/metrics"


class AdminConfig(BaseModel):
    # The admin API allows adding/removing backends at runtime (used by the
    # autoscaling simulation). It is NOT authenticated in this project --
    # that's a deliberate scope cut for a resume/demo project, but a real
    # deployment MUST put this behind a firewall/VPC-internal network or
    # add authentication before exposing it, since it lets any caller
    # change where production traffic is routed.
    enabled: bool = True
    path_prefix: str = "/admin"


class Config(BaseModel):
    """Root configuration object for the load balancer."""

    listen_host: str = "0.0.0.0"
    listen_port: int = Field(default=8080, ge=1, le=65535)
    algorithm: str = "round_robin"
    backends: list[BackendConfig] = Field(default_factory=list)
    client_timeout_seconds: float = Field(default=30.0, gt=0)

    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    passive_health: PassiveHealthConfig = Field(default_factory=PassiveHealthConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    sticky_sessions: StickySessionConfig = Field(default_factory=StickySessionConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)

    @field_validator("algorithm")
    @classmethod
    def algorithm_must_be_supported(cls, v: str) -> str:
        if v not in SUPPORTED_ALGORITHMS:
            supported = ", ".join(sorted(SUPPORTED_ALGORITHMS))
            raise ValueError(
                f"unsupported algorithm {v!r} (supported: {supported})"
            )
        return v

    @model_validator(mode="after")
    def must_have_at_least_one_backend(self) -> "Config":
        if not self.backends:
            raise ValueError("at least one backend must be configured")
        return self


def load(path: str | Path) -> Config:
    """Load and validate a JSON config file.

    Raises FileNotFoundError if the file doesn't exist, and
    pydantic.ValidationError if the contents are invalid.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"config file not found: {file_path}")

    raw = json.loads(file_path.read_text())
    return Config.model_validate(raw)
