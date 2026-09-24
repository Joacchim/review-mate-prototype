"""The ASGI application: wire the routes and the static UI over a SessionManager.

On startup it restores persisted sessions (AC-8); on shutdown it stops the actors cleanly. The
static UI is mounted last so it never shadows the `/api` routes.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
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
from review_mate.seams import RepoRef
from review_mate.workspace.manager import WorkspaceManager
from review_mate.writeback.service import Writeback

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# How often a presence-bearing scope is rebuilt while watched. The watcher TTL is 90s, so
# this bounds how long a view can claim an agent is attached after it stopped listening.
PRESENCE_TICK = 5.0

# The scope families named after a session, rather than after the fleet. A review's lifetime is
# decided by whether any of these is being read, so a new one belongs here and nowhere else.
SESSION_FAMILIES = ("diff", "blob", "rail", "chat", "review", "threads", "access",
                    "tree", "commits")


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
    from review_mate.view.chat import ChatScopes
    from review_mate.view.diffscope import BlobScopes, DiffScopes
    from review_mate.view.rail import RailScope
    from review_mate.view.review import ReviewScope
    from review_mate.view.access import AccessScope
    from review_mate.view.browse import BrowseScopes
    from review_mate.view.threads import ThreadsScope
    from review_mate.writeback.submit import ReviewSubmitter
    from review_mate.writeback.threads import ThreadVerbs
    from review_mate.view.hub import HubScope
    from review_mate.view.protocol import HUB
    # a session's reading scopes are rebuilt when its state changes, but only while a client is
    # looking: the bus says when a scope gains its first watcher and loses its last, and the tail
    # on that session's events runs exactly between those two moments.
    session_pumps: dict[str, asyncio.Task] = {}
    # the consent watch on a session, running for as long as a client is reading its access scope
    access_watches: dict[str, asyncio.Task] = {}
    presence_task: asyncio.Task | None = None

    def _session_of(scope: str) -> str | None:
        kind, sep, rest = scope.partition(":")
        return rest.partition(":")[0] if sep and kind in SESSION_FAMILIES else None

    def _session_scopes(session_id: str) -> set[str]:
        """Every scope a client can be holding for one session.

        One list, because three callers ask the same question — is anyone still reading this
        review, what must be rebuilt when it changes, and which session a scope belongs to — and a
        family added to only two of them goes stale in a way nothing fails on.
        """
        held: set[str] = set()
        for family in SESSION_FAMILIES:
            # diff and blob name a file after the session, so they match on a prefix
            held |= bus.watched(f"{family}:{session_id}:") | bus.watched(f"{family}:{session_id}")
        return held

    def _still_watched(session_id: str) -> bool:
        return bool(_session_scopes(session_id))

    async def _tail(session_id: str) -> None:
        actor = manager.get(session_id)
        if actor is None:
            return
        async for _event in actor.subscribe(since=actor.snapshot().seq):
            await republish_session(session_id)

    def _carries_presence(scope: str) -> bool:
        """The scopes whose view can change with no event behind it — see `_presence_tick`."""
        return scope == HUB or (scope.startswith("chat:") and scope.count(":") == 1)

    async def _presence_tick() -> None:
        """Republish what presence rides on, while someone is watching it.

        `attached` lapses by clock: a watcher that stops long-polling leaves no event, so a view
        stating "Claude is working" would keep saying it. Rebuilding on a timer is what makes the
        answer current; the bus sends nothing when the rebuilt view is the same, which is what
        makes rebuilding it this often affordable.
        """
        while True:
            await asyncio.sleep(PRESENCE_TICK)
            for scope in bus.watched("chat:") | ({HUB} if bus.watchers(HUB) else set()):
                if _carries_presence(scope):
                    await bus.publish(scope)

    def _ensure_ticker() -> None:
        nonlocal presence_task
        if presence_task is None or presence_task.done():
            presence_task = asyncio.create_task(_presence_tick())

    def _stop_ticker_if_idle() -> None:
        nonlocal presence_task
        if presence_task is None:
            return
        if bus.watchers(HUB) or any(_carries_presence(s) for s in bus.watched("chat:")):
            return
        presence_task.cancel()
        presence_task = None

    def _on_first_watch(scope: str) -> None:
        if _carries_presence(scope):
            _ensure_ticker()
        # both browse scopes cost a host read, so they are asked for exactly while someone looks
        if scope.startswith("tree:") and scope.count(":") == 1:
            asyncio.create_task(_quietly(browse.fetch_tree(scope.partition(":")[2])))
        if scope.startswith("commits:") and scope.count(":") == 1:
            asyncio.create_task(_quietly(browse.fetch_commits(scope.partition(":")[2])))
        if crossrepo is not None and scope.startswith("access:") and scope.count(":") == 1:
            # a decision can only come from a client that is reading this, so watching exactly then
            # misses nothing — and the watch sweeps for approvals a restart left unhonoured
            sid = scope.partition(":")[2]
            if sid not in access_watches:
                access_watches[sid] = asyncio.create_task(_quietly(crossrepo.watch(sid)))
        if scope.startswith("review:") and scope.count(":") == 1:
            # who approved is a host fact the bar shows, so it is worth asking for exactly while
            # someone is reading it — and once, rather than on every rebuild
            asyncio.create_task(_warm_approval(scope.partition(":")[2]))
        session_id = _session_of(scope)
        if session_id is None or session_id in session_pumps:
            return
        task = asyncio.create_task(_tail(session_id))
        session_pumps[session_id] = task
        task.add_done_callback(lambda finished: _pump_done(session_id, finished))

    def _pump_done(session_id: str, task: asyncio.Task) -> None:
        session_pumps.pop(session_id, None)
        if not task.cancelled():
            task.exception()   # a dead tail must not darken the session silently

    def _on_last_watch(scope: str) -> None:
        if _carries_presence(scope):
            _stop_ticker_if_idle()
        if scope.startswith("access:") and scope.count(":") == 1:
            watch = access_watches.pop(scope.partition(":")[2], None)
            if watch is not None:
                watch.cancel()      # a clone already under way is its own task and survives this
        session_id = _session_of(scope)
        if session_id is None or _still_watched(session_id):
            return
        task = session_pumps.pop(session_id, None)
        if task is not None:
            task.cancel()

    bus = ViewBus(on_first_watch=_on_first_watch, on_last_watch=_on_last_watch)
    def watcher() -> dict:
        """Who is listening, read when a scope is built rather than captured when it is wired."""
        return activity_broker.watcher() if activity_broker is not None else {}

    hub = HubScope(manager, provider=provider, kb=kb,
                   user=getattr(provider, "username", "") or "", watcher=watcher)
    bus.register(HUB, hub.build)
    async def publish_mode(session_id: str, mode: str) -> None:
        """Republish the scopes of one session-and-mode — the list and whatever files are open —
        once a resolution that serves all of them lands."""
        listing = f"diff:{session_id}:{mode}"
        for scope in bus.watched(f"diff:{session_id}:"):
            if scope == listing or scope.startswith(listing + ":"):
                await bus.publish(scope)

    diff_scopes = DiffScopes(manager, provider=provider,
                             workspace=getattr(manager, "_workspace", None), kb=kb,
                             publish=publish_mode)
    bus.register_family("diff", diff_scopes.build)
    blob_scopes = BlobScopes(manager, provider=provider, publish=bus.publish)
    bus.register_family("blob", blob_scopes.build)
    rail_scope = RailScope(manager, provider=provider,
                           publish=lambda session_id: bus.publish(f"rail:{session_id}"))
    bus.register_family("rail", rail_scope.build)
    chat_scopes = ChatScopes(manager, watcher=watcher)
    bus.register_family("chat", chat_scopes.build)
    review_scope = ReviewScope(manager, provider=provider, kb=kb)
    bus.register_family("review", review_scope.build)
    threads_scope = ThreadsScope(manager, user=getattr(provider, "username", "") or "")
    bus.register_family("threads", threads_scope.build)
    access_scope = AccessScope(manager)
    bus.register_family("access", access_scope.build)

    # Consent's other half. The scope shows what the agent asked for and what the reviewer answered;
    # this is what makes an approval mean something — it materializes the repository and records
    # where it landed, so "approved" stops being a note nothing acts on.
    crossrepo = None
    workspace = getattr(manager, "_workspace", None)
    if workspace is not None and provider is not None and hasattr(provider, "locate_repo"):
        from review_mate.crossrepo.broker import CrossRepoBroker

        async def _locate(name: str):
            found = await provider.locate_repo(name)
            if found is None:
                return None
            return (RepoRef(host=found["host"], project=found["project"],
                            clone_url=found["clone_url"]), found["ref"])

        crossrepo = CrossRepoBroker(manager, workspace, kb, _locate)
    browse = BrowseScopes(manager, provider=provider, publish=bus.publish)
    bus.register_family("tree", browse.build_tree)
    bus.register_family("commits", browse.build_commits)

    async def _quietly(coro) -> None:
        """A background read that reports its own failure through the view it is filling."""
        with suppress(Exception):
            await coro

    async def _warm_approval(session_id: str) -> None:
        """Ask the host who approved, then republish so the bar stops saying it does not know.

        Best-effort on purpose: a host that will not answer leaves the bar reading `checked: false`,
        which is what it should read, rather than taking the review down with it.
        """
        with suppress(Exception):
            await review_scope.refresh(session_id)
            await bus.publish(f"review:{session_id}")

    async def republish_session(session_id: str) -> None:
        """Rebuild the reading scopes a client currently holds for one session.

        Called where a session's files actually change — a host re-sync — rather than on every
        event, and only for scopes someone is watching, so a review nobody has open costs nothing.
        A client watching only the hub sees a session's counts move on the presence tick instead,
        since no tail runs for a review nobody has open.
        Blobs are included: a re-sync can move the head, and a blob reads at whatever sha its mode
        resolves to.
        """
        held = _session_scopes(session_id)
        if bus.watchers(HUB):
            # the hub folds per-session facts too — counts, and what each review is waiting on
            held = held | {HUB}
        for scope in held:
            await bus.publish(scope)

    async def _stop_pumps() -> None:
        for task in list(session_pumps.values()) + list(access_watches.values()):
            task.cancel()
        session_pumps.clear()
        access_watches.clear()
        nonlocal presence_task
        if presence_task is not None:
            presence_task.cancel()
            presence_task = None

    submitter = ReviewSubmitter(manager, writeback, provider=provider, kb=kb)
    thread_verbs = ThreadVerbs(manager, writeback, provider=provider)
    routes = build_routes(manager, resolve_ref=resolve_ref, provider=provider, broker=broker,
                          activity_broker=activity_broker)
    # registered before the static mount so `/api/stream` and `/api/cmd` are never shadowed by the UI
    routes.extend(build_view_routes(manager, bus, hub, resolve_ref=resolve_ref,
                                    submitter=submitter, review=review_scope, kb=kb,
                                    threads=thread_verbs, browse=browse, diffs=diff_scopes))

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
        await diff_scopes.aclose()
        await blob_scopes.aclose()
        await rail_scope.aclose()
        await _stop_pumps()
        await manager.shutdown()

    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(_NoCacheUI)])
    app.state.manager = manager
    app.state.broker = broker
    app.state.activity_broker = activity_broker
    app.state.kb = kb
    app.state.bus = bus
    app.state.hub = hub
    app.state.diff_scopes = diff_scopes
    app.state.blob_scopes = blob_scopes
    app.state.rail_scope = rail_scope
    app.state.chat_scopes = chat_scopes
    # whether the presence ticker is running — a test asserts it starts and stops with the
    # watching, which is otherwise invisible from outside
    app.state.presence_running = lambda: presence_task is not None and not presence_task.done()
    return app
