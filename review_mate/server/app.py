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

from review_mate.server.routes import build_routes
from review_mate.session.manager import SessionManager

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(manager: SessionManager | None = None,
               static_dir: Path | None = None,
               with_mcp: bool = True) -> Starlette:
    manager = manager or SessionManager()
    web = static_dir or _WEB_DIR

    routes = build_routes(manager)

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
