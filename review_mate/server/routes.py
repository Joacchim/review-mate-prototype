"""HTTP + WebSocket handlers — thin: translate requests into manager/actor calls.

The browser is the only HTTP caller, so HTTP commands run with `Origin.BROWSER`; the authority
matrix in the core rejects anything it may not do. The agent reaches the session in-process
(the `mcp-bridge` seam), not through these routes.
"""
from __future__ import annotations

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from review_mate.seams import MRRef
from review_mate.session.commands import ApplyThread, MarkDraftPosted, parse_command
from review_mate.session.manager import SessionManager
from review_mate.session.state import DraftStatus, Origin

# server-side long-poll ceiling for GET /api/activity: under common idle cutoffs, and short enough
# that the coordinator gets a regular tick (to re-evaluate the idle-reap bound) even when quiet.
ACTIVITY_TIMEOUT = 50.0


def build_routes(manager: SessionManager, resolve_ref=None, provider=None, broker=None,
                 writeback=None, activity_broker=None) -> list:
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

    async def review_queue(request: Request) -> JSONResponse:
        if provider is None or not hasattr(provider, "review_queue_items"):
            return JSONResponse([])  # no host configured → empty queue (baseline still runs)
        try:
            return JSONResponse(await provider.review_queue_items())
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    async def search(request: Request) -> JSONResponse:
        q = request.query_params.get("q", "").strip()
        if provider is None or not q or not hasattr(provider, "search"):
            return JSONResponse([])  # no host / empty query → no suggestions
        try:
            return JSONResponse(await provider.search(q))
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

    async def poll_lookup(request: Request) -> JSONResponse:
        if broker is None:
            return JSONResponse({"error": "lookup unavailable"}, status_code=400)
        req = await broker.wait_for_answer(request.path_params["id"], timeout=25.0)
        if req is None:
            return JSONResponse({"error": "unknown lookup"}, status_code=404)
        return JSONResponse(req.model_dump(mode="json"))

    async def repo_tree(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        mr = actor.snapshot().mr
        if provider is None or mr is None or not hasattr(provider, "get_repo_tree"):
            return JSONResponse([])
        try:
            return JSONResponse(await provider.get_repo_tree(mr.project, mr.sha))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    async def get_file(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        mr = actor.snapshot().mr
        path = request.query_params.get("path", "")
        if provider is None or mr is None or not path or not hasattr(provider, "get_file"):
            return JSONResponse({"error": "unavailable"}, status_code=400)
        try:
            return JSONResponse({"path": path, "content": await provider.get_file(mr.project, path, mr.sha)})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    async def list_sessions(request: Request) -> JSONResponse:
        return JSONResponse([s.model_dump(mode="json") for s in manager.list()])

    async def get_session(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        return JSONResponse(actor.snapshot().model_dump(mode="json"))

    async def submit_command(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        try:
            command = parse_command(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse({"error": "malformed command"}, status_code=400)
        result = await actor.submit(command, Origin.BROWSER)
        if not result.ok:
            return JSONResponse({"ok": False, "reason": result.reason}, status_code=400)
        return JSONResponse({"ok": True, "seq": result.seq})

    async def submit_review(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if writeback is None:
            return JSONResponse({"error": "review posting unavailable"}, status_code=400)
        snap = actor.snapshot()
        if snap.mr is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        ref = MRRef(host=snap.mr.host, project=snap.mr.project, iid=snap.mr.iid)
        pending = [d for d in snap.drafts if d.status is DraftStatus.DRAFT]
        results = []
        for d in pending:
            try:
                res = await writeback.post_comment(snap.id, d.highlight_id, d.body, ref)
                # anchored comments return a discussion {notes:[…]}; an MR-level note returns the note
                note = (res.get("notes") or [res])[0] if isinstance(res, dict) else {}
                url = (f"{snap.mr.url}#note_{note.get('id')}"
                       if note.get("id") and snap.mr.url else None)
                await actor.submit(MarkDraftPosted(highlight_id=d.highlight_id, url=url),
                                   Origin.BROWSER)
                results.append({"highlight_id": d.highlight_id, "ok": True, "url": url})
            except Exception as exc:  # one bad anchor shouldn't sink the rest of the review
                results.append({"highlight_id": d.highlight_id, "ok": False, "error": str(exc)})
        posted = sum(1 for r in results if r["ok"])
        approved = False
        approve_error = None
        body = await _maybe_json(request)
        if isinstance(body, dict) and body.get("approve"):
            try:
                await writeback.approve(ref)   # capability-gated in the writer
                approved = True
            except Exception as exc:
                approve_error = str(exc)
        return JSONResponse({"posted": posted, "total": len(pending), "results": results,
                             "approved": approved, "approve_error": approve_error})

    async def _remirror_threads(actor, ref) -> list:
        """Re-pull the MR's discussions from the host and re-mirror them into session state
        (host is the single source of truth for threads). No-op without a provider."""
        if provider is None or not hasattr(provider, "fetch_threads"):
            return []
        threads = await provider.fetch_threads(ref)
        for t in threads:
            await actor.submit(ApplyThread(thread=t), Origin.SYSTEM)
        return threads

    def _thread_ref(actor):
        snap = actor.snapshot()
        if snap.mr is None:
            return None
        return MRRef(host=snap.mr.host, project=snap.mr.project, iid=snap.mr.iid)

    async def reply_thread(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if writeback is None:
            return JSONResponse({"error": "review posting unavailable"}, status_code=400)
        ref = _thread_ref(actor)
        if ref is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        body = await _maybe_json(request)
        text = body.get("body", "").strip() if isinstance(body, dict) else ""
        if not text:
            return JSONResponse({"error": "empty reply"}, status_code=400)
        try:
            await writeback.reply(ref, request.path_params["tid"], text)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await _remirror_threads(actor, ref)
        return JSONResponse({"ok": True})

    async def resolve_thread(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if writeback is None:
            return JSONResponse({"error": "review posting unavailable"}, status_code=400)
        ref = _thread_ref(actor)
        if ref is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        body = await _maybe_json(request)
        resolved = bool(body.get("resolved", True)) if isinstance(body, dict) else True
        try:
            await writeback.resolve(ref, request.path_params["tid"], resolved)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await _remirror_threads(actor, ref)
        return JSONResponse({"ok": True, "resolved": resolved})

    async def context(request: Request) -> JSONResponse:
        """The cheap, deterministic context tier for a highlighted line range (D21): host-computed
        last-touch + linked issues, no agent. Each source degrades independently (best-effort)."""
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        snap = actor.snapshot()
        if snap.mr is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        file = request.query_params.get("file", "")
        try:
            start = int(request.query_params.get("start", "0"))
            end = int(request.query_params.get("end", str(start)))
        except ValueError:
            start = end = 0
        out: dict = {"blame": [], "linked_issues": []}
        if provider is not None and file and hasattr(provider, "blame"):
            out["blame"] = await provider.blame(snap.mr.project, file, snap.mr.sha, start, end)
        if provider is not None and hasattr(provider, "linked_issues"):
            out["linked_issues"] = await provider.linked_issues(snap.mr.project, snap.mr.iid)
        return JSONResponse(out)

    async def refresh_threads(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        ref = _thread_ref(actor)
        if ref is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        threads = await _remirror_threads(actor, ref)
        return JSONResponse({"threads": len(threads)})

    async def end_session(request: Request) -> JSONResponse:
        try:
            await manager.end(request.path_params["id"])
        except KeyError:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        return JSONResponse({"ok": True})

    async def stream(ws: WebSocket) -> None:
        await ws.accept()
        actor = manager.get(ws.path_params["id"])
        if actor is None:
            await ws.close(code=4404)
            return
        try:
            since = int(ws.query_params.get("since", "0"))
        except ValueError:
            since = 0
        try:
            async for event in actor.subscribe(since=since):
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
        Route("/api/queue", review_queue, methods=["GET"]),
        Route("/api/search", search, methods=["GET"]),
        Route("/api/lookup", open_lookup, methods=["POST"]),
        Route("/api/lookup/{id}", poll_lookup, methods=["GET"]),
        Route("/api/activity", activity, methods=["GET"]),
        Route("/api/sessions/{id}/repo-tree", repo_tree, methods=["GET"]),
        Route("/api/sessions/{id}/file", get_file, methods=["GET"]),
        Route("/api/sessions", create_session, methods=["POST"]),
        Route("/api/sessions", list_sessions, methods=["GET"]),
        Route("/api/sessions/{id}", get_session, methods=["GET"]),
        Route("/api/sessions/{id}", end_session, methods=["DELETE"]),
        Route("/api/sessions/{id}/commands", submit_command, methods=["POST"]),
        Route("/api/sessions/{id}/submit-review", submit_review, methods=["POST"]),
        Route("/api/sessions/{id}/threads/{tid}/reply", reply_thread, methods=["POST"]),
        Route("/api/sessions/{id}/threads/{tid}/resolve", resolve_thread, methods=["POST"]),
        Route("/api/sessions/{id}/refresh-threads", refresh_threads, methods=["POST"]),
        Route("/api/sessions/{id}/context", context, methods=["GET"]),
        WebSocketRoute("/api/sessions/{id}/stream", stream),
    ]


async def _maybe_json(request: Request):
    try:
        return await request.json()
    except Exception:
        return {}
