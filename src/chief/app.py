"""Application factory."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from .api.routes import get_service, router
from .domain.service import Chief
from .errors import ChiefError
from .mcp_server import build_mcp
from .storage import Store

API_VERSION = "v1"
DEFAULT_DB = "chief.sqlite3"
WEB_ROOT = Path(__file__).parent / "web"


class RevalidatingStatic(StaticFiles):
    """Static files that are always revalidated before being reused from cache.

    Without this a browser applies its own heuristics to a module script with no
    ``Cache-Control``, and happily keeps serving a stale ``app.js`` across reloads. That is
    wrong for this app specifically: the page and the server are versioned independently —
    editing the UI does not restart Chief, and restarting Chief does not reload the page — so
    a silently stale script shows up as the UI misbehaving against an API that is fine.

    ``no-cache`` does not mean "do not cache": the file is still stored and still answered
    with a 304 when unchanged, thanks to the ETag starlette already sends. It only means the
    browser must ask first.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = "no-cache"
        return response


def _clean_errors(errors: list[dict]) -> list[dict]:
    """Pydantic puts the raw exception object in ``ctx``; make the payload JSON-safe."""
    cleaned = []
    for error in errors:
        entry = {k: v for k, v in error.items() if k not in ("ctx", "url", "input")}
        ctx = error.get("ctx")
        if isinstance(ctx, dict):
            entry["ctx"] = {k: str(v) for k, v in ctx.items()}
        cleaned.append(entry)
    return cleaned


def allowed_hosts() -> set[str]:
    """Host names this server answers file content on.

    Loopback plus whatever ``--host`` was given, and ``CHIEF_ALLOW_HOSTS`` for the case the
    UI is reached under a name — a tunnel endpoint, a container alias. Kept here rather than
    in the route so there is one answer for the whole process, and deliberately narrow: this
    is the DNS-rebinding defence for the one route that reads the disk.
    """
    hosts = {"localhost", "127.0.0.1", "::1", os.environ.get("CHIEF_HOST", "127.0.0.1")}
    extra = os.environ.get("CHIEF_ALLOW_HOSTS", "")
    hosts.update(h.strip().lower() for h in extra.split(",") if h.strip())
    return hosts


def create_app(store: Store | None = None) -> FastAPI:
    owned = store is None
    store = store or Store(os.environ.get("CHIEF_DB", DEFAULT_DB))
    service = Chief(store)

    # REQ-2: the MCP surface runs on this app, over the same service instance, so both
    # transports share one Store, one connection and one lock (see mcp_server.py).
    mcp = build_mcp(service)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # A mounted Starlette sub-app does not get its lifespan run, and the streamable-HTTP
        # session manager has to be running before it can answer anything. Host it here.
        async with mcp.session_manager.run():
            yield
        if owned:
            store.close()

    app = FastAPI(
        lifespan=lifespan,
        title="Chief",
        version="1.0.0",
        summary="Chief API & Data Contract v1 — adaptive agentic workflow tracker",
        description=(
            "Tracks workflow plans and execution state reported by external agentic "
            "harnesses. The Chief never executes a step itself."
        ),
    )
    app.state.store = store
    app.state.service = service
    app.dependency_overrides[get_service] = lambda: service
    # REQ-22: the version lives in the path so a v2 can be added without breaking clients.
    app.include_router(router, prefix=f"/{API_VERSION}")
    app.include_router(router)

    @app.exception_handler(ChiefError)
    async def _domain_error(_: Request, exc: ChiefError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _schema_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Every document is schema-validated before acceptance (REQ-34).
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_failed",
                    "message": "the submitted document failed schema validation",
                    "details": _clean_errors(exc.errors()),
                }
            },
        )

    # REQ-16: the browser UI. Static files only — it reads the same public REST API as any
    # other client (REQ-18), so mounting it grants it nothing the API does not already offer.
    if WEB_ROOT.is_dir():
        app.mount("/ui", RevalidatingStatic(directory=WEB_ROOT, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def _root() -> RedirectResponse:
            return RedirectResponse("/ui/")

    app.state.mcp = mcp
    # The MCP transport checks the Host header to block DNS rebinding, against an allowlist
    # derived from this value. It defaults to loopback, which is wrong the moment Chief is
    # bound anywhere else: MCP clients get a 421 while REST carries on working, which reads
    # as "MCP is broken" rather than "the host does not match". CHIEF_HOST is set by
    # __main__ from --host.
    app.mount(
        "/mcp",
        mcp.streamable_http_app(
            streamable_http_path="/", host=os.environ.get("CHIEF_HOST", "127.0.0.1")
        ),
    )

    return app


def create_app_at(path: str | Path) -> FastAPI:
    return create_app(Store(path))
