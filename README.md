# L7 Load Balancer

A production-grade Layer-7 HTTP load balancer, built from scratch in
Python (asyncio + aiohttp) with no framework doing the load-balancing or
proxying logic for you. Six load-balancing algorithms, active + passive
health checking, a circuit breaker, retries with exponential backoff,
token-bucket rate limiting, sticky sessions, Prometheus metrics, a
Grafana dashboard, Docker Compose for local dev, a live autoscaling
simulation, and a k6 benchmark suite with real measured numbers below.

This project was built incrementally, phase by phase, with a design
rationale documented for every non-trivial decision directly in the code
comments -- the goal is for the code itself to be readable as an
explanation of *why*, not just *what*.

## Table of contents

- [Architecture](#architecture)
- [Features](#features)
- [Quickstart](#quickstart)
- [Configuration reference](#configuration-reference)
- [Load balancing algorithms](#load-balancing-algorithms)
- [Health checking](#health-checking)
- [Circuit breaker](#circuit-breaker)
- [Retries](#retries)
- [Rate limiting](#rate-limiting)
- [Sticky sessions](#sticky-sessions)
- [Admin API](#admin-api)
- [Metrics & Grafana](#metrics--grafana)
- [Autoscaling simulation](#autoscaling-simulation)
- [Benchmarks](#benchmarks)
- [Testing](#testing)
- [Project structure](#project-structure)
- [Deployment](#deployment)
- [Known limitations & future improvements](#known-limitations--future-improvements)

## Architecture

```
                         ┌─────────────────────────────────────────┐
                         │            Load Balancer (aiohttp)        │
                         │                                            │
   Client ─────HTTP────▶ │  Rate Limit  ──▶  Admin API   /metrics    │
                         │  Middleware        (/admin/*)  (/metrics)  │
                         │       │                                    │
                         │       ▼                                    │
                         │  ┌──────────┐    ┌───────────┐             │
                         │  │  Proxy   │───▶│ Balancer  │             │
                         │  │ Handler  │    │(pluggable)│             │
                         │  └────┬─────┘    └───────────┘             │
                         │       │                                    │
                         │       │  per backend:                      │
                         │       │  ┌──────────────────┐               │
                         │       ├─▶│ Circuit Breaker   │               │
                         │       │  └──────────────────┘               │
                         │       │  ┌──────────────────┐               │
                         │       ├─▶│ Retry + Backoff   │               │
                         │       │  └──────────────────┘               │
                         │       │                                    │
                         │  ┌────▼─────────────────┐                  │
                         │  │ Active Health Checker  │ (background)    │
                         │  └────────────────────────┘                 │
                         └───────┬──────────┬──────────┬───────────────┘
                                 │          │          │
                          ┌──────▼───┐ ┌────▼─────┐ ┌──▼───────┐
                          │ Backend 1│ │ Backend 2│ │ Backend 3│  ◀── dynamically
                          └──────────┘ └──────────┘ └──────────┘      scalable via
                                                                        Admin API
```

**Package layout** (clean architecture -- each package has one job, and
depends only on the abstractions below it, not concrete implementations):

```
src/lb/
├── config/         # Pydantic config schema + JSON loading/validation
├── logging/        # structured JSON logging (stdlib logging + custom formatter)
├── backend/        # Backend: URL, weight, health/connection state
├── balancer/        # Balancer protocol + 6 algorithm implementations
├── circuitbreaker/  # per-backend CLOSED/OPEN/HALF_OPEN state machine
├── healthcheck/      # background active health-check probing loop
├── ratelimit/         # token bucket rate limiter
├── middleware/         # aiohttp middleware (currently: rate limiting)
├── admin/               # runtime backend registration API
├── metrics/               # Prometheus collectors
├── proxy/                  # the reverse proxy handler tying it all together
└── main.py                   # composition root (wires everything together)
```

The `Balancer` is a `typing.Protocol` -- adding a 7th algorithm never
requires touching the proxy or any other package, only adding a new file
in `balancer/` and one line in `main.py`'s algorithm registry. This
Open/Closed design was exercised for real across this project: Phase 1
shipped with just Round Robin, and five more algorithms were added later
with zero changes to `proxy.py`.

## Features

| Feature | Status |
|---|---|
| Round Robin | ✅ |
| Weighted Round Robin (smooth, Nginx-style) | ✅ |
| Random | ✅ |
| IP Hash (client affinity) | ✅ |
| Power of Two Choices | ✅ |
| Least Connections | ✅ |
| Active health checks (periodic probing) | ✅ |
| Passive health checks (real-traffic-driven) | ✅ |
| Circuit breaker (CLOSED/OPEN/HALF_OPEN) | ✅ |
| Retries with exponential backoff + jitter | ✅ |
| Token bucket rate limiting | ✅ |
| Sticky sessions (cookie-based) | ✅ |
| Prometheus metrics + Grafana dashboard | ✅ |
| Dynamic backend registration (admin API) | ✅ |
| Autoscaling simulation (Docker container add/remove) | ✅ |
| Docker + Docker Compose | ✅ |
| k6 load testing | ✅ |
| GitHub Actions CI | ✅ |
| AWS EC2 deployment guide | ✅ |

## Quickstart

### Local (no Docker)

Requires Python 3.11+.

```bash
pip install -r requirements-dev.txt

# Terminal 1-3: start three demo backends
python3 -m demo.echobackend.main --port 9001 --id backend-1
python3 -m demo.echobackend.main --port 9002 --id backend-2
python3 -m demo.echobackend.main --port 9003 --id backend-3

# Terminal 4: start the load balancer
PYTHONPATH=src python3 -m lb.main --config configs/config.json --log-level info

# Terminal 5: hit it
curl http://localhost:8080/
```

### Docker Compose (load balancer + 3 backends + Prometheus + Grafana)

```bash
docker compose up --build -d
curl http://localhost:8080/            # the load balancer
open http://localhost:3000             # Grafana (admin/admin -- change this, see docs/deployment/aws-ec2.md)
open http://localhost:9090             # Prometheus
```

### Running the tests

```bash
pip install -r requirements-dev.txt
pytest --cov=lb --cov-report=term-missing tests/
```

133 tests, 97% coverage as of this writing. See [Testing](#testing) below.

## Configuration reference

Configuration is a single JSON file (`configs/config.json` for local dev,
`configs/config.docker.json` for the Compose stack), validated at startup
via Pydantic -- a malformed or out-of-range value fails fast with a clear
error rather than causing confusing behavior later.

```jsonc
{
  "listen_host": "0.0.0.0",
  "listen_port": 8080,
  "algorithm": "round_robin",       // see "Load balancing algorithms" below
  "client_timeout_seconds": 30,
  "backends": [
    { "url": "http://localhost:9001", "weight": 1 }
  ],
  "health_check": {                 // active health checking
    "enabled": true,
    "path": "/",
    "interval_seconds": 5,
    "timeout_seconds": 2,
    "unhealthy_threshold": 3,       // consecutive probe failures to mark dead
    "healthy_threshold": 2          // consecutive probe successes to mark alive again
  },
  "passive_health": {
    "failure_threshold": 3          // consecutive real-traffic failures to mark dead
  },
  "circuit_breaker": {
    "failure_threshold": 5,
    "recovery_timeout_seconds": 30
  },
  "retry": {
    "max_retries": 2,
    "base_delay_seconds": 0.1,
    "max_delay_seconds": 2.0
  },
  "rate_limit": {
    "enabled": false,
    "requests_per_second": 100,
    "burst": 200
  },
  "sticky_sessions": {
    "enabled": false,
    "cookie_name": "LB_STICKY_BACKEND",
    "cookie_max_age_seconds": 3600
  },
  "metrics": {
    "enabled": true,
    "path": "/metrics"
  },
  "admin": {
    "enabled": true,
    "path_prefix": "/admin"
  }
}
```

## Load balancing algorithms

Set via `"algorithm"` in the config. All six live in `src/lb/balancer/`,
each implementing the same `Balancer` protocol (`next_backend(request)`,
`name`).

- **`round_robin`** -- cycles through backends in order via an atomic
  counter. Assumes equal-capacity backends and equal-cost requests.
- **`weighted_round_robin`** -- the *smooth* weighted round-robin
  algorithm (same one Nginx/LVS use): weights 5:1:1 produce `A B A C A A A`,
  not the bursty `A A A A A B C` a naive expansion would give.
- **`random`** -- uniform random among healthy backends. Zero shared
  state, zero lock contention; converges to even distribution at scale.
- **`ip_hash`** -- `crc32(client_ip) % len(alive_backends)`, giving
  session affinity without cookies. Trade-off: backend list changes remap
  most clients (documented limitation; consistent hashing is the standard
  fix, listed under Future Improvements).
- **`power_of_two_choices`** -- samples 2 backends at random, routes to
  whichever has fewer active connections. O(1), avoids the herd effect of
  naive least-connections, and is what Envoy defaults to in production.
- **`least_connections`** -- the exact version: scans every alive backend
  for the true minimum active-connection count. O(n) but simple and
  deterministic; a useful ground-truth to compare against P2C.

## Health checking

Two independent mechanisms, deliberately not merged into one:

- **Active** (`src/lb/healthcheck/active_checker.py`): a background task
  probes every backend on a fixed interval (`GET health_check.path`),
  independent of real traffic. This is what lets a backend that's
  received zero traffic (because it's already marked dead) get tested and
  recover automatically -- passive checking alone can't do this, since it
  only observes backends that are actually receiving requests.
- **Passive** (`Backend.record_failure()` / `record_success()`, called
  from the proxy on every real request): a backend is marked dead after
  `passive_health.failure_threshold` consecutive failures *on real
  traffic*, faster than waiting for the next scheduled active probe.

Both use hysteresis (requiring multiple consecutive successes to trust
recovery, not just one) to avoid flapping a backend that's still warming up.

## Circuit breaker

A classic three-state breaker (`src/lb/circuitbreaker/circuit_breaker.py`),
one instance per backend: `CLOSED → OPEN` after
`circuit_breaker.failure_threshold` consecutive failures, `OPEN →
HALF_OPEN` after `recovery_timeout_seconds`, and exactly one trial request
allowed through in `HALF_OPEN` at a time (to avoid a thundering herd
re-overwhelming a recovering backend).

This is a distinct mechanism from health checking, answering a different
question: health checking asks "is this backend healthy enough for new
traffic at all?"; the circuit breaker asks "have we been hammering a
struggling backend with request after request, each paying a full
timeout, and should we stop doing that for a bit?" A backend can be
"alive" but still have an open circuit.

## Retries

Failed requests are retried against a *different* backend with exponential
backoff + jitter (`src/lb/proxy/proxy.py`), but **only for idempotent
methods** (`GET`, `HEAD`, `OPTIONS` by default) -- POST/PUT/PATCH/DELETE
are never automatically retried, since re-executing a request whose
first-attempt server-side effect is unknown (did it already process the
payment?) risks duplicating it. This is a deliberate, conservative safety
choice, not an oversight.

Retries only trigger on connection errors/timeouts or a configurable set
of retryable upstream statuses (502/503/504 by default) -- a legitimate
4xx from the backend is not retried, since retrying "the client sent bad
data" against a different backend would never help.

## Rate limiting

Token bucket, one bucket per client IP (`src/lb/ratelimit/`), applied as
an aiohttp middleware in front of everything (including `/admin` and
`/metrics`) so it's a uniform cross-cutting concern rather than logic
duplicated per route. Token bucket was chosen over fixed-window counters
because it naturally allows short legitimate bursts while still enforcing
a steady-state average rate, and avoids the well-known "2x rate at a
window boundary" problem fixed-window counters have.

## Sticky sessions

Cookie-based (`LB_STICKY_BACKEND` by default): on a successful response,
the proxy sets a cookie identifying the backend (a truncated SHA-256 of
its URL, stable across restarts -- deliberately not Python's built-in
`hash()`, which is randomized per-process). On the next request, if that
cookie names a backend that's still alive and whose circuit isn't open,
the request goes straight there, bypassing the configured balancer
entirely. If the sticky target is unavailable, the request falls back to
normal balancing rather than failing outright.

## Admin API

Used internally by the autoscaling simulation, and useful for manual
inspection/ops:

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/backends` | List all backends with live state (alive, active connections, consecutive failures) |
| `POST` | `/admin/backends` | Register a new backend: `{"url": "...", "weight": 1}` |
| `DELETE` | `/admin/backends/<url>` | Deregister a backend by its URL |

**⚠️ This API is intentionally unauthenticated** -- a deliberate scope cut
documented in `src/lb/admin/admin.py` and in `docs/deployment/aws-ec2.md`.
It lets any caller who can reach it change where production traffic is
routed. Fine on a private Docker network for a demo; must be firewalled
or authenticated before exposing it in anything beyond that.

## Metrics & Grafana

Prometheus metrics exposed at `/metrics` (`src/lb/metrics/metrics.py`):

- `lb_requests_total{backend, status}` -- counter
- `lb_request_duration_seconds{backend}` -- histogram
- `lb_backend_up{backend}` -- gauge (1/0)
- `lb_backend_active_connections{backend}` -- gauge
- `lb_circuit_breaker_state{backend}` -- gauge (0=closed, 1=half_open, 2=open)
- `lb_retries_total{backend}` -- counter
- `lb_rate_limit_rejections_total` -- counter
- `lb_backends_total` -- gauge (useful for watching autoscaling happen live)

A pre-built Grafana dashboard (`docker/grafana/provisioning/dashboards/l7-load-balancer.json`)
ships with the Compose stack and auto-provisions on startup: request rate
by backend and status, p50/p95/p99 latency, backend health over time,
active connections, circuit breaker state, retry rate, and rate-limit
rejections.

## Autoscaling simulation

`scripts/autoscaler.py` is a standalone controller process (separate from
the load balancer itself, mirroring how real cloud autoscaling is usually
external infrastructure, not the proxy process) that:

1. Polls `GET /admin/backends` every few seconds, using
   `active_connections` as its load signal.
2. If average load across the backends *it manages* exceeds a threshold
   and it's under `--max-backends`, it launches a new backend container
   via `docker run`, waits for it to become reachable, then registers it
   via `POST /admin/backends`.
3. If average load drops below a lower threshold and it's above
   `--min-backends`, it deregisters the least-loaded managed backend via
   `DELETE /admin/backends/<url>`, then stops and removes its container.

```bash
docker compose up --build -d
python3 scripts/autoscaler.py \
  --lb-admin-url http://localhost:8080/admin \
  --image l7-load-balancer-backend1 \
  --network l7-load-balancer_lb-network \
  --min-backends 0 --max-backends 5 \
  --scale-up-threshold 5 --scale-down-threshold 1
```

Watch `lb_backends_total` in Grafana climb and fall as you generate load
(`k6 run scripts/k6-throughput-test.js`) and then stop.

The autoscaler only ever touches backends it launched itself, never the
original `docker-compose.yml` backends -- this keeps its blast radius
contained and avoids the ambiguity of "which backend is safe to kill,"
which matters more for a clear demo than handling every edge case a real
production autoscaler would.

## Benchmarks

Measured with k6 v2.1.0 against the load balancer running locally
(round robin, 3 backends, health checks + circuit breaker + retry logic
all active, default config) -- see `scripts/k6-load-test.js` and
`scripts/k6-throughput-test.js`.

**Realistic mixed-traffic test** (ramping 20→100 VUs, 4 different paths,
randomized think-time between requests):

| Metric | Value |
|---|---|
| Total requests | 1,215 |
| Error rate | 0.00% |
| p50 latency | 1.23ms |
| p95 latency | 2.48ms |
| p99 latency | 25.55ms |
| Throughput | ~79 req/s (bounded by simulated think-time, not the LB) |

**Peak throughput test** (50 VUs, no think-time, hammering a single path):

| Metric | Value |
|---|---|
| Total requests | 30,156 in 15s |
| Error rate | 0.00% |
| Sustained throughput | **~2,009 req/s** |
| avg latency | 24.79ms |
| p90 latency | 30.83ms |
| p95 latency | 35.42ms |

Run it yourself:

```bash
docker compose up --build -d
k6 run scripts/k6-load-test.js                        # realistic mix
k6 run scripts/k6-throughput-test.js --env VUS=50      # peak throughput
```

These numbers were measured on a shared, modest sandbox VM, not
dedicated benchmark hardware -- treat them as directionally useful
(zero errors under load, low double-digit-ms p95 even at ~2k req/s) rather
than as an absolute ceiling for the architecture. A Go rewrite of the same
design would be expected to push higher raw throughput given goroutines'
lower per-connection overhead than asyncio -- a natural follow-up
comparison if you fork this project.

## Testing

- **Unit tests**: every algorithm, `Backend`, `CircuitBreaker`,
  `RateLimiter`/`TokenBucket`, and config validation, in isolation.
- **Integration tests**: real `aiohttp` test servers (actual TCP
  listeners) standing in for backends, exercising the full reverse-proxy
  path, retry logic, sticky sessions, passive health tripping, circuit
  breaking, the admin API, the rate-limit middleware, and full
  `create_app()` wiring -- not mocks, real network round-trips.
- **133 tests, 97% line coverage** as of this writing (`pytest --cov=lb`).
- **CI** (`.github/workflows/ci.yml`): lint (`ruff`) → unit/integration
  tests with coverage → Docker image builds → a full `docker compose up`
  smoke test that curls the running stack and checks `/metrics`.
- **Load testing**: see [Benchmarks](#benchmarks) above.

## Project structure

```
.
├── src/lb/                    # the load balancer itself (see Architecture)
├── tests/{unit,integration}/  # pytest suite
├── demo/echobackend/          # minimal demo backend used in local/Docker testing
├── configs/                   # config.json (local), config.docker.json (Compose)
├── docker/                    # Prometheus config, Grafana dashboards & provisioning
├── scripts/                   # autoscaler.py, k6 load test scripts
├── docs/deployment/           # AWS EC2 deployment guide
├── .github/workflows/ci.yml   # CI pipeline
├── Dockerfile                 # load balancer image
├── docker-compose.yml         # LB + 3 backends + Prometheus + Grafana
└── pyproject.toml             # packaging, pytest, ruff config
```

## Deployment

See [`docs/deployment/aws-ec2.md`](docs/deployment/aws-ec2.md) for a full
guide to deploying the Docker Compose stack to a single EC2 instance,
including security-hardening notes (the admin API and Grafana defaults in
particular need attention before any non-demo exposure) and what a
genuinely highly-available multi-instance setup would additionally need.

## Known limitations & future improvements

Documented in-line in the code at the point they're relevant, collected
here for visibility:

- **IP Hash uses modulo, not consistent hashing** -- any backend
  added/removed remaps most clients to a different backend. The standard
  fix is a hash ring (consistent hashing), which only remaps ~1/N of
  clients on a topology change. Good first contribution for this repo.
- **IP Hash / rate limiting trust `request.remote` directly** -- behind
  another proxy (e.g. an AWS ALB in front of this), that's the proxy's IP,
  not the real client's. A trusted-proxy + `X-Forwarded-For` policy would
  fix this, deliberately out of scope here to avoid the spoofing risk of
  blindly trusting a client-supplied header.
- **Rate limiter buckets are never evicted** -- acceptable at
  resume/demo scale, a real memory-growth/DoS concern with many unique
  client IPs in production. An LRU cap or periodic sweep is the fix.
- **Admin API is unauthenticated** -- see the Admin API section above.
- **Weighted Round Robin's per-backend scheduling state doesn't reset on
  recovery** -- a backend that was down for a while can get a small burst
  of extra traffic right after an active health check marks it alive
  again, since its `current_weight` picked up where it left off.
- **No TLS termination in the load balancer itself** -- expected to be
  handled by a fronting ALB/nginx/Caddy in any real deployment; see the
  AWS EC2 deployment doc.
- **Single-process** -- this load balancer runs as one asyncio event loop
  in one process. A production deployment would run several instances
  behind an L4 load balancer or DNS, both for availability and to use more
  than one CPU core (asyncio itself doesn't parallelize across cores).
- **A Go rewrite would likely out-throughput this Python/asyncio
  implementation** given goroutines' lower per-connection overhead --
  noted in Benchmarks as a natural comparison project.
