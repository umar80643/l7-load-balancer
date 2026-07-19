#!/usr/bin/env python3
"""Autoscaling simulation for the load balancer.

This is a standalone process, deliberately separate from the load balancer
itself -- in a real cloud deployment, autoscaling is normally handled by
infrastructure (an ASG, a Kubernetes HPA, Nomad, etc.), not by the proxy
process. Keeping it as its own script mirrors that separation of concerns
and demonstrates the same pattern: an external controller observes load
and drives infrastructure changes, while the load balancer just exposes
the hooks (the admin API) that make it controllable.

How it works:
  1. Poll GET {lb_admin_url}/backends every `poll_interval` seconds. Each
     entry includes active_connections, which we use as our load signal.
  2. Compute average active connections per *managed* backend (backends
     this script itself launched -- see NOTE below).
  3. If average load > scale_up_threshold and we're under max_backends:
     launch a new backend container via `docker run`, wait for it to
     become reachable, then POST it to the admin API to register it.
  4. If average load < scale_down_threshold and we're over min_backends:
     pick the least-loaded managed backend, DELETE it from the admin API
     (so the balancer stops routing to it immediately), then `docker stop`
     + `docker rm` its container.

NOTE on scope: this script only ever scales backends *it itself* launched
(tracked in `self._managed`), never the original backends from
docker-compose.yml. This avoids the ambiguity of "which backend is safe to
kill" and keeps the simulation's blast radius contained and easy to reason
about, which matters more for a demo/resume project than handling every
edge case a production autoscaler would need to.

Usage:
    python3 scripts/autoscaler.py \\
        --lb-admin-url http://localhost:8080/admin \\
        --image l7lb-echobackend \\
        --network l7-load-balancer_lb-network \\
        --min-backends 0 --max-backends 5 \\
        --scale-up-threshold 5 --scale-down-threshold 1
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import subprocess
import sys
import time

import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("autoscaler")


class ManagedBackend:
    def __init__(self, container_name: str, url: str) -> None:
        self.container_name = container_name
        self.url = url


class Autoscaler:
    def __init__(
        self,
        lb_admin_url: str,
        image: str,
        network: str,
        min_backends: int,
        max_backends: int,
        scale_up_threshold: float,
        scale_down_threshold: float,
        poll_interval: float,
        host_port_start: int,
    ) -> None:
        self.lb_admin_url = lb_admin_url.rstrip("/")
        self.image = image
        self.network = network
        self.min_backends = min_backends
        self.max_backends = max_backends
        self.scale_up_threshold = scale_up_threshold
        self.scale_down_threshold = scale_down_threshold
        self.poll_interval = poll_interval
        self._port_counter = itertools.count(host_port_start)
        self._managed: list[ManagedBackend] = []
        self._container_seq = itertools.count(1)

    async def run_forever(self) -> None:
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await self._tick(session)
                except Exception:  # noqa: BLE001 - keep the control loop alive
                    log.exception("autoscaler tick failed")
                await asyncio.sleep(self.poll_interval)

    async def _tick(self, session: aiohttp.ClientSession) -> None:
        async with session.get(f"{self.lb_admin_url}/backends") as resp:
            data = await resp.json()

        all_backends = data["backends"]
        managed_urls = {m.url for m in self._managed}
        managed_entries = [b for b in all_backends if b["url"] in managed_urls]

        if not self._managed:
            avg_load = sum(b["active_connections"] for b in all_backends) / max(len(all_backends), 1)
        else:
            avg_load = sum(b["active_connections"] for b in managed_entries) / len(managed_entries) \
                if managed_entries else 0

        log.info(
            "poll: total_backends=%d managed_backends=%d avg_managed_load=%.2f",
            len(all_backends), len(self._managed), avg_load,
        )

        if avg_load > self.scale_up_threshold and len(self._managed) < self.max_backends:
            await self._scale_up(session)
        elif avg_load < self.scale_down_threshold and len(self._managed) > self.min_backends:
            await self._scale_down(session, managed_entries)

    async def _scale_up(self, session: aiohttp.ClientSession) -> None:
        seq = next(self._container_seq)
        container_name = f"autoscaled-backend-{seq}"
        host_port = next(self._port_counter)
        backend_id = f"autoscaled-{seq}"

        log.info("scaling up: launching container %s on host port %d", container_name, host_port)
        result = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", container_name,
                "--network", self.network,
                "-p", f"{host_port}:9000",
                self.image,
                "--port", "9000",
                "--id", backend_id,
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.error("docker run failed: %s", result.stderr.strip())
            return

        # Backends on the same Docker network can reach each other by
        # container name, which is what we register with the load
        # balancer (host_port is only for the operator's convenience in
        # inspecting the container directly from the host).
        internal_url = f"http://{container_name}:9000"
        if not await self._wait_until_reachable(session, internal_url):
            log.error("new backend %s never became reachable, removing container", container_name)
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            return

        async with session.post(
            f"{self.lb_admin_url}/backends", json={"url": internal_url, "weight": 1}
        ) as resp:
            if resp.status == 201:
                self._managed.append(ManagedBackend(container_name, internal_url))
                log.info("registered new backend %s with load balancer", internal_url)
            else:
                body = await resp.text()
                log.error("failed to register backend with LB: %s %s", resp.status, body)
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    async def _wait_until_reachable(
        self, session: aiohttp.ClientSession, url: str, timeout: float = 10.0
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=1)) as resp:
                    if resp.status < 500:
                        return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        return False

    async def _scale_down(self, session: aiohttp.ClientSession, managed_entries: list[dict]) -> None:
        if not managed_entries:
            return
        least_loaded = min(managed_entries, key=lambda b: b["active_connections"])
        target = next((m for m in self._managed if m.url == least_loaded["url"]), None)
        if target is None:
            return

        log.info("scaling down: deregistering and stopping %s", target.container_name)
        async with session.delete(f"{self.lb_admin_url}/backends/{target.url}") as resp:
            if resp.status not in (204, 404):
                body = await resp.text()
                log.error("failed to deregister backend: %s %s", resp.status, body)
                return

        subprocess.run(["docker", "stop", target.container_name], capture_output=True)
        subprocess.run(["docker", "rm", target.container_name], capture_output=True)
        self._managed.remove(target)


def main() -> None:
    parser = argparse.ArgumentParser(description="Autoscaling simulation for the L7 load balancer")
    parser.add_argument("--lb-admin-url", default="http://localhost:8080/admin")
    parser.add_argument("--image", default="l7lb-echobackend", help="Docker image for new backends")
    parser.add_argument("--network", default="l7-load-balancer_lb-network")
    parser.add_argument("--min-backends", type=int, default=0)
    parser.add_argument("--max-backends", type=int, default=5)
    parser.add_argument("--scale-up-threshold", type=float, default=5.0, help="avg active connections to trigger scale-up")
    parser.add_argument("--scale-down-threshold", type=float, default=1.0, help="avg active connections to trigger scale-down")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--host-port-start", type=int, default=9100)
    args = parser.parse_args()

    autoscaler = Autoscaler(
        lb_admin_url=args.lb_admin_url,
        image=args.image,
        network=args.network,
        min_backends=args.min_backends,
        max_backends=args.max_backends,
        scale_up_threshold=args.scale_up_threshold,
        scale_down_threshold=args.scale_down_threshold,
        poll_interval=args.poll_interval,
        host_port_start=args.host_port_start,
    )
    log.info("autoscaler starting: min=%d max=%d up>%.1f down<%.1f",
              args.min_backends, args.max_backends, args.scale_up_threshold, args.scale_down_threshold)
    try:
        asyncio.run(autoscaler.run_forever())
    except KeyboardInterrupt:
        log.info("autoscaler stopped")


if __name__ == "__main__":
    main()
