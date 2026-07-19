"""IP Hash balancer: deterministically routes a given client IP to the same
backend on every request, without needing cookies.

Design decisions:

1. Hash function: we use zlib.crc32, NOT Python's built-in hash(). Python
   randomizes str hashing per-process (PYTHONHASHSEED) as a security
   measure against hash-flooding attacks, which means hash("1.2.3.4") would
   return a *different* value every time the process restarts -- breaking
   the "same client always goes to the same backend" guarantee across
   restarts. crc32 is fast, deterministic, and good enough for load
   distribution (we don't need cryptographic properties here).

2. Modulo over the current alive backend list: idx = crc32(ip) % len(alive).
   Known trade-off, stated plainly: if the alive backend count changes
   (a backend goes down, a new one is added), the modulo operation remaps
   most clients to a different backend -- not just the fraction that
   "should" move. The standard fix is consistent hashing (a hash ring),
   which only remaps ~1/N of clients when N changes. That's flagged here
   as a deliberate scope cut for this phase and a good future enhancement,
   not an oversight.

3. Client IP source: request.remote is the direct TCP peer address as seen
   by this process. If the load balancer sits behind another proxy (e.g. a
   cloud LB doing TLS termination upstream of us), request.remote would be
   that proxy's IP, not the real client's -- at that point you'd want to
   trust a X-Forwarded-For header instead, but only from known/trusted
   proxies (blindly trusting a client-supplied header lets any client spoof
   its hash bucket). That trusted-proxy logic is out of scope for this
   phase and noted for later.
"""
from __future__ import annotations

import zlib

from aiohttp.web import Request

from lb.backend import Backend


class IPHash:
    def __init__(self, backends: list[Backend]) -> None:
        self._backends = backends

    def next_backend(self, request: Request) -> Backend | None:
        alive = [b for b in self._backends if b.is_alive]
        if not alive:
            return None

        client_ip = request.remote or "unknown"
        idx = zlib.crc32(client_ip.encode("utf-8")) % len(alive)
        return alive[idx]

    @property
    def name(self) -> str:
        return "ip_hash"
