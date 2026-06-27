"""The ASGI application: wire the routes and the static UI over a SessionManager.

On startup it restores persisted sessions (AC-8); on shutdown it stops the actors cleanly. The
static UI is mounted last so it never shadows the `/api` routes.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from review_mate.host.config import build_provider_from_env
from review_mate.server.routes import build_routes
from review_mate.session.manager import SessionManager
from review_mate.workspace.manager import WorkspaceManager

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def build_manager_from_env():
    """Wire a live SessionManager when a host is configured; else the self-contained baseline.

    Host selection lives in the host layer (build_provider_from_env), so this composition root
    names no specific host.
    """
    provider, resolve_ref = build_provider_from_env()
    if provider is None:
        return SessionManager(), None, None
    manager = SessionManager(mr_source=provider, workspace=WorkspaceManager())
    return manager, resolve_ref, provider


def create_app(manager: SessionManager | None = None,
               static_dir: Path | None = None,
               with_mcp: bool = True,
               resolve_ref=None,
               provider=None) -> Starlette:
    if manager is None:
        manager, resolve_ref, provider = build_manager_from_env()
    web = static_dir or _WEB_DIR

    routes = build_routes(manager, resolve_ref=resolve_ref, provider=provider)

    mcp_app = None
    if with_mcp:
        from review_mate.mcp.bridge import AgentBridge
        from review_mate.mcp.server import build_mcp_server
        mcp_app = build_mcp_server(AgentBridge(manager), mountable=True).streamable_http_app()
        routes.append(Mount("/mcp", app=mcp_app))  # the agent seam (shares this manager)

    # static UI mounted last so it never shadows /api or /mcp
    routes.append(Mount("/", app=StaticFiles(directory=str(web), html=True), name="ui"))

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await manager.restore_all()
        if mcp_app is not None:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        else:
            yield
        await manager.shutdown()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.manager = manager
    return app
