"""The ASGI application: wire the routes and the static UI over a SessionManager.

On startup it restores persisted sessions (AC-8); on shutdown it stops the actors cleanly. The
static UI is mounted last so it never shadows the `/api` routes.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles


class _NoCacheUI(BaseHTTPMiddleware):
    """Serve the UI assets uncached so a browser never runs a stale app.js/index.html."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if not path.startswith("/api") and not path.startswith("/mcp"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

from review_mate.host.config import build_provider_from_env, build_writer_from_env
from review_mate.server.routes import build_routes
from review_mate.session.manager import SessionManager
from review_mate.workspace.manager import WorkspaceManager
from review_mate.writeback.service import Writeback

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def build_manager_from_env(activity_broker=None):
    """Wire a live SessionManager when a host is configured; else the self-contained baseline.

    Host selection lives in the host layer (build_provider_from_env), so this composition root
    names no specific host. The activity broker (review-fleet notification spine) is threaded in
    so the manager owns it from construction, before restore_all attaches the republishers.
    """
    provider, resolve_ref = build_provider_from_env()
    if provider is None:
        return SessionManager(activity_broker=activity_broker), None, None, None
    manager = SessionManager(mr_source=provider, workspace=WorkspaceManager(),
                             activity_broker=activity_broker)
    writer = build_writer_from_env()
    writeback = Writeback(manager, writer) if writer is not None else None
    return manager, resolve_ref, provider, writeback


def create_app(manager: SessionManager | None = None,
               static_dir: Path | None = None,
               with_mcp: bool = True,
               resolve_ref=None,
               provider=None,
               writeback=None,
               kb=None) -> Starlette:
    # the activity channel — ephemeral notification spine; one watcher covers every session
    # (review-fleet). The composition root owns it, wired in before restore_all attaches the
    # per-actor republishers.
    from review_mate.activity.broker import ActivityBroker
    activity_broker = ActivityBroker()

    if manager is None:
        manager, resolve_ref, provider, writeback = build_manager_from_env(activity_broker)
    else:
        # externally-injected manager (tests): adopt the broker it brought, else give it ours.
        activity_broker = manager._activity_broker or activity_broker
        manager._activity_broker = activity_broker
    web = static_dir or _WEB_DIR

    # the MR-discovery channel — ephemeral, shared by the browser routes and the agent bridge
    from review_mate.lookup.broker import LookupBroker
    broker = LookupBroker()

    # the user-wide review knowledge base — here it holds the per-MR reviewed watermark (diff-versions)
    if kb is None:
        from review_mate.kb.store import ReviewKB
        kb = ReviewKB()

    # the view plane — server-folded state, one scope at a time. Clients subscribe to scopes and
    # render what they carry; none of them re-derive review state.
    from review_mate.server.view_routes import build_view_routes
    from review_mate.view.bus import ViewBus
    from review_mate.view.diffscope import BlobScopes, DiffScopes
    from review_mate.view.hub import HubScope
    from review_mate.view.protocol import HUB
    bus = ViewBus()
    hub = HubScope(manager, provider=provider, kb=kb,
                   user=getattr(provider, "username", "") or "")
    bus.register(HUB, hub.build)
    diff_scopes = DiffScopes(manager)
    bus.register_family("diff", diff_scopes.build)
    blob_scopes = BlobScopes(manager, provider=provider, publish=bus.publish)
    bus.register_family("blob", blob_scopes.build)

    async def republish_session(session_id: str) -> None:
        """Rebuild the reading scopes a client currently holds for one session.

        Called where a session's files actually change — a host re-sync — rather than on every
        event, and only for scopes someone is watching, so a review nobody has open costs nothing.
        Blobs are included: a re-sync can move the head, and a blob reads at whatever sha its mode
        resolves to.
        """
        held = bus.watched(f"diff:{session_id}:") | bus.watched(f"blob:{session_id}:")
        for scope in held:
            await bus.publish(scope)

    routes = build_routes(manager, resolve_ref=resolve_ref, provider=provider, broker=broker,
                          writeback=writeback, activity_broker=activity_broker, kb=kb,
                          republish_session=republish_session)
    # registered before the static mount so `/api/stream` and `/api/cmd` are never shadowed by the UI
    routes.extend(build_view_routes(manager, bus, hub, resolve_ref=resolve_ref))

    mcp_app = None
    if with_mcp:
        from review_mate.mcp.bridge import AgentBridge
        from review_mate.mcp.server import build_mcp_server
        bridge = AgentBridge(manager, broker=broker, provider=provider)
        mcp_app = build_mcp_server(bridge, mountable=True).streamable_http_app()
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
        await hub.aclose()
        await blob_scopes.aclose()
        await manager.shutdown()

    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(_NoCacheUI)])
    app.state.manager = manager
    app.state.broker = broker
    app.state.activity_broker = activity_broker
    app.state.kb = kb
    app.state.bus = bus
    app.state.hub = hub
    return app
