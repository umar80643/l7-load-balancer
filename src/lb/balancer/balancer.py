"""The Balancer abstraction and its implementations.

This is the heart of the Open/Closed Principle in this project: the proxy
layer depends only on the `Balancer` Protocol below, so adding a new
algorithm (Phase 2: weighted round robin, random, IP hash, power of two
choices, ...) never requires touching proxy code -- only adding a new class
here that satisfies this Protocol.

We use `typing.Protocol` (structural typing) rather than `abc.ABC` (nominal
typing / inheritance): any class with a matching `next_backend` method and
`name` property satisfies the interface, with no forced base class. This
keeps individual algorithm implementations simple and independently
testable.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from aiohttp.web import Request

from lb.backend import Backend


@runtime_checkable
class Balancer(Protocol):
    """Selects which backend should handle a given HTTP request."""

    def next_backend(self, request: Request) -> Backend | None:
        """Select a backend for the given request.

        The request is passed in (not just the backend list) because some
        algorithms need request data: IP Hash needs the client's remote
        address, sticky sessions (Phase 6) need a cookie. Keeping this in
        the interface now avoids a breaking signature change later.

        Returns None if no backend is available (e.g. all backends are
        unhealthy) -- callers must handle that case explicitly.
        """
        ...

    @property
    def name(self) -> str:
        """Short identifier for logging/metrics, e.g. 'round_robin'."""
        ...
