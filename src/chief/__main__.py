"""``python -m chief`` / ``chief`` — run the service locally."""

from __future__ import annotations

import argparse
import os

import uvicorn

from .app import create_app_at


def main() -> None:
    parser = argparse.ArgumentParser(prog="chief", description="Run the Chief API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--allow-host",
        action="append",
        metavar="NAME",
        help="an extra Host header value to serve artifact file content on; loopback is "
             "always allowed. Only needed when the UI is reached under a name rather than "
             "localhost (a tunnel endpoint, a container alias). Repeatable.",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="chief.sqlite3", help="SQLite file path")
    args = parser.parse_args()
    # The MCP transport's Host allowlist is built from this; see create_app.
    os.environ.setdefault("CHIEF_HOST", args.host)
    if args.allow_host:
        os.environ.setdefault("CHIEF_ALLOW_HOSTS", ",".join(args.allow_host))
    app = create_app_at(args.db)
    # One process, one port: API, docs and the web UI (REQ-16, REQ-21). Uvicorn only
    # announces the bind address, so name what is behind it.
    origin = f"http://{args.host}:{args.port}"
    print(
        f"Chief  ui {origin}/  ·  api {origin}/v1  ·  docs {origin}/docs  ·  db {args.db}",
        flush=True,  # otherwise it sits in the buffer behind a long-running server
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
