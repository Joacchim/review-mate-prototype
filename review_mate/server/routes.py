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

from review_mate.session.commands import parse_command
from review_mate.session.manager import SessionManager
from review_mate.session.state import Origin


def build_routes(manager: SessionManager) -> list:
    async def create_session(request: Request) -> JSONResponse:
        body = await _maybe_json(request)
        ref = body.get("ref") if isinstance(body, dict) else None
        sid = await manager.create(ref=ref)
        return JSONResponse({"id": sid})

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
            await ws.close()
        except WebSocketDisconnect:
            pass

    return [
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
