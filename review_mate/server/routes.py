"""HTTP + WebSocket handlers — thin: translate requests into manager/writer calls.

The browser is the only HTTP caller, so HTTP commands run with `Origin.BROWSER`; the authority
matrix in the core rejects anything it may not do. The agent reaches the session in-process
(the `mcp-bridge` contract), not through these routes.
"""
from __future__ import annotations

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from review_mate.contracts import MRRef, RepoRef
from review_mate.view.asks import outstanding as outstanding_asks
from review_mate.session.commands import parse_command
from review_mate.session.manager import SessionManager
from review_mate.forges import Forges
from review_mate.session.state import Origin, SessionStatus

# server-side long-poll ceiling for GET /api/activity: under common idle cutoffs, and short enough
# that the coordinator gets a regular tick (to re-evaluate the idle-reap bound) even when quiet.
ACTIVITY_TIMEOUT = 50.0


def build_routes(manager: SessionManager, resolve_ref=None, provider=None, broker=None,
                 activity_broker=None) -> list:
    forges = Forges.of(provider)   # one forge or several, asked the same way

    async def create_session(request: Request) -> JSONResponse:
        body = await _maybe_json(request)
        raw = body.get("ref") if isinstance(body, dict) else None
        ref: MRRef | None = None
        if isinstance(raw, dict):
            ref = MRRef(**raw)
        elif isinstance(raw, str) and raw.strip() and resolve_ref is not None:
            ref = resolve_ref(raw)
            if ref is None:
                return JSONResponse({"error": f"could not parse reference: {raw!r}"}, status_code=400)
        try:
            sid = await manager.create(ref=ref)
        except Exception as exc:  # a bad ref / GitLab failure shouldn't 500 the UI
            return JSONResponse({"error": f"failed to load MR: {exc}"}, status_code=502)
        return JSONResponse({"id": sid, "ref_resolved": ref is not None})

    async def search(request: Request) -> JSONResponse:
        q = request.query_params.get("q", "").strip()
        if forges is None or not q:
            return JSONResponse([])  # no forge / empty query → no suggestions
        try:
            return JSONResponse(await forges.search(q))   # every forge, merged
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    async def open_lookup(request: Request) -> JSONResponse:
        body = await _maybe_json(request)
        query = (body.get("query") if isinstance(body, dict) else "") or ""
        if broker is None or not query.strip():
            return JSONResponse({"error": "lookup unavailable"}, status_code=400)
        req = broker.create(query.strip())
        if activity_broker is not None:  # surface the lookup on the agent's activity stream (D16)
            activity_broker.publish("lookup_opened", lookup_id=req.id, query=query.strip())
        return JSONResponse({"id": req.id, "seq": req.seq})

    async def activity(request: Request) -> Response:
        if activity_broker is None:
            return Response(status_code=204)  # baseline: no activity channel configured
        try:
            since = int(request.query_params.get("since", "0"))
        except ValueError:
            since = 0
        event = await activity_broker.wait(since, timeout=ACTIVITY_TIMEOUT)
        if event is None:
            return Response(status_code=204)  # timed out — the caller re-polls
        return JSONResponse(event.model_dump(mode="json"))

    async def outstanding(request: Request) -> JSONResponse:
        """Every ask the reviewer is still waiting on the agent for, across all active sessions.

        The activity stream is deliberately ephemeral (see `ActivityBroker`): a restart drops
        in-flight notifications, and the safety argument for that rests on the agent re-deriving
        outstanding work from durable state rather than only reacting to events. This route is that
        derivation, over the same predicate the chat topic publishes — `view.asks` owns it, so an
        agent re-finding its work and a reviewer watching for an answer cannot disagree.

        Snapshot reads only, no host I/O, so it stays cheap enough to poll — unlike the hub's
        `hub.refresh`, which fans out host calls per session by design (D19).
        """
        sessions = []
        for summ in manager.list():
            if summ.status is not SessionStatus.ACTIVE:
                continue
            writer = manager.get(summ.id)
            if writer is None:
                continue
            snap = writer.snapshot()
            by_id = {h.id: h for h in snap.highlights}
            asks = []
            for ask in outstanding_asks(snap):
                row: dict = {"kind": ask.kind, "since": ask.since}
                if ask.subject is not None:
                    row["subject"] = ask.subject.model_dump(mode="json")
                    highlight = by_id.get(ask.subject.id)
                    if highlight is not None:
                        # the file is what an agent needs to open the thing being asked about
                        row["highlight_id"] = highlight.id
                        row["file"] = highlight.file
                asks.append(row)
            if not asks:
                continue   # only sessions needing attention — this is a work list, not a census
            sessions.append({"session_id": summ.id, "project": summ.project, "iid": summ.iid,
                             "title": summ.title, "asks": asks})
        sessions.sort(key=lambda s: s["asks"][0].get("since") or "")
        return JSONResponse({"sessions": sessions,
                             "total": sum(len(s["asks"]) for s in sessions)})

    async def poll_lookup(request: Request) -> JSONResponse:
        if broker is None:
            return JSONResponse({"error": "lookup unavailable"}, status_code=400)
        req = await broker.wait_for_answer(request.path_params["id"], timeout=25.0)
        if req is None:
            return JSONResponse({"error": "unknown lookup"}, status_code=404)
        return JSONResponse(req.model_dump(mode="json"))

    async def list_sessions(request: Request) -> JSONResponse:
        return JSONResponse([s.model_dump(mode="json") for s in manager.list()])

    async def get_session(request: Request) -> JSONResponse:
        writer = manager.get(request.path_params["id"])
        if writer is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        return JSONResponse(writer.snapshot().model_dump(mode="json"))

    async def submit_command(request: Request) -> JSONResponse:
        writer = manager.get(request.path_params["id"])
        if writer is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        try:
            command = parse_command(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse({"error": "malformed command"}, status_code=400)
        result = await writer.submit(command, Origin.BROWSER)
        if not result.ok:
            return JSONResponse({"ok": False, "reason": result.reason}, status_code=400)
        return JSONResponse({"ok": True, "seq": result.seq})

    async def end_session(request: Request) -> JSONResponse:
        try:
            await manager.end(request.path_params["id"])
        except KeyError:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        return JSONResponse({"ok": True})

    async def stream(ws: WebSocket) -> None:
        await ws.accept()
        writer = manager.get(ws.path_params["id"])
        if writer is None:
            await ws.close(code=4404)
            return
        try:
            since = int(ws.query_params.get("since", "0"))
        except ValueError:
            since = 0
        try:
            async for event in writer.subscribe(since=since):
                await ws.send_text(event.model_dump_json())
        except WebSocketDisconnect:
            return
        except Exception:
            pass  # transport boundary — don't let a send/serialize error escape as a task crash
        try:
            await ws.close()
        except Exception:  # pragma: no cover - already closing/closed
            pass

    return [
        Route("/api/search", search, methods=["GET"]),
        Route("/api/lookup", open_lookup, methods=["POST"]),
        Route("/api/lookup/{id}", poll_lookup, methods=["GET"]),
        Route("/api/activity", activity, methods=["GET"]),
        Route("/api/outstanding", outstanding, methods=["GET"]),
        Route("/api/sessions", create_session, methods=["POST"]),
        Route("/api/sessions", list_sessions, methods=["GET"]),
        Route("/api/sessions/{id}", get_session, methods=["GET"]),
        Route("/api/sessions/{id}", end_session, methods=["DELETE"]),
        Route("/api/sessions/{id}/commands", submit_command, methods=["POST"]),
        WebSocketRoute("/api/sessions/{id}/stream", stream),
    ]


async def _maybe_json(request: Request):
    try:
        return await request.json()
    except Exception:
        return {}
