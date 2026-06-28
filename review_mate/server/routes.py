"""HTTP + WebSocket handlers — thin: translate requests into manager/actor calls.

The browser is the only HTTP caller, so HTTP commands run with `Origin.BROWSER`; the authority
matrix in the core rejects anything it may not do. The agent reaches the session in-process
(the `mcp-bridge` seam), not through these routes.
"""
from __future__ import annotations

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from review_mate.seams import MRRef
from review_mate.session.commands import MarkDraftPosted, parse_command
from review_mate.session.manager import SessionManager
from review_mate.session.state import DraftStatus, Origin


def build_routes(manager: SessionManager, resolve_ref=None, provider=None, broker=None,
                 writeback=None) -> list:
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
        return JSONResponse({"id": req.id, "seq": req.seq})

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
                note = (res.get("notes") or [{}])[0] if isinstance(res, dict) else {}
                url = (f"{snap.mr.url}#note_{note.get('id')}"
                       if note.get("id") and snap.mr.url else None)
                await actor.submit(MarkDraftPosted(highlight_id=d.highlight_id, url=url),
                                   Origin.BROWSER)
                results.append({"highlight_id": d.highlight_id, "ok": True, "url": url})
            except Exception as exc:  # one bad anchor shouldn't sink the rest of the review
                results.append({"highlight_id": d.highlight_id, "ok": False, "error": str(exc)})
        posted = sum(1 for r in results if r["ok"])
        return JSONResponse({"posted": posted, "total": len(pending), "results": results})

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
        Route("/api/sessions/{id}/repo-tree", repo_tree, methods=["GET"]),
        Route("/api/sessions/{id}/file", get_file, methods=["GET"]),
        Route("/api/sessions", create_session, methods=["POST"]),
        Route("/api/sessions", list_sessions, methods=["GET"]),
        Route("/api/sessions/{id}", get_session, methods=["GET"]),
        Route("/api/sessions/{id}", end_session, methods=["DELETE"]),
        Route("/api/sessions/{id}/commands", submit_command, methods=["POST"]),
        Route("/api/sessions/{id}/submit-review", submit_review, methods=["POST"]),
        WebSocketRoute("/api/sessions/{id}/stream", stream),
    ]


async def _maybe_json(request: Request):
    try:
        return await request.json()
    except Exception:
        return {}
