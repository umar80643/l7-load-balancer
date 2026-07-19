"""A trivial HTTP server used only for local end-to-end testing and demos:
it identifies itself in every response so you can visually confirm the load
balancer is distributing traffic. This is test tooling, not part of the
production load balancer.
"""
from __future__ import annotations

import argparse

from aiohttp import web


def make_app(backend_id: str) -> web.Application:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=f"Hello from {backend_id} (path={request.path})\n")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--id", default="backend-1")
    args = parser.parse_args()

    print(f'echo backend "{args.id}" listening on :{args.port}')
    web.run_app(make_app(args.id), host="0.0.0.0", port=args.port, print=None)


if __name__ == "__main__":
    main()
