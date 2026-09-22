"""Transport for the view protocol: one stream out, one command endpoint in.

These routes carry no domain logic. The stream hands a client whatever the bus built for the
scopes it named, and the command endpoint translates a named command into a manager call and
republishes the scopes it invalidated. Everything a client renders is decided server-side, so a
second client is a renderer, not a second implementation of the review model.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from review_mate.seams import MRRef
from review_mate.view.protocol import HUB, ScopeError, Subscribe, parse_client_message


def build_view_routes(manager, bus, hub, resolve_ref=None, submitter=None,
                      review=None, kb=None) -> list:
    async def _publish_hub() -> None:
        await bus.publish(HUB)

    async def cmd(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "reason": "malformed body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "reason": "malformed body"}, status_code=400)
        name = body.get("cmd")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return JSONResponse({"ok": False, "reason": "malformed args"}, status_code=400)
        handler = _HANDLERS.get(name)
        if handler is None:
            return JSONResponse({"ok": False, "reason": f"unknown command: {name!r}"},
                                status_code=400)
        return await handler(args)

    async def _session_open(args: dict) -> JSONResponse:
        """Open a review. The queue's 'track' affordance is this same command — the difference
        is whether the client then navigates, which is the client's business, not the core's."""
        raw = args.get("ref")
        ref: MRRef | None = None
        if raw is not None and not isinstance(raw, (dict, str)):
            return JSONResponse({"ok": False, "reason": f"unusable ref: {raw!r}"}, status_code=400)
        if isinstance(raw, dict):
            try:
                ref = MRRef(**raw)
            except (TypeError, ValidationError) as exc:
                return JSONResponse({"ok": False, "reason": f"unusable ref: {exc}"},
                                    status_code=400)
        elif isinstance(raw, str) and raw.strip():
            if resolve_ref is None:
                return JSONResponse({"ok": False, "reason": "no host configured"}, status_code=400)
            ref = resolve_ref(raw)
            if ref is None:
                return JSONResponse({"ok": False, "reason": f"could not parse reference: {raw!r}"},
                                    status_code=400)
        try:
            sid = await manager.create(ref=ref)
        except Exception as exc:
            return JSONResponse({"ok": False, "reason": f"failed to load MR: {exc}"},
                                status_code=502)
        await _publish_hub()
        return JSONResponse({"ok": True, "session": sid})

    async def _session_close(args: dict) -> JSONResponse:
        sid = args.get("id")
        if not isinstance(sid, str) or not sid:
            return JSONResponse({"ok": False, "reason": "missing session id"}, status_code=400)
        try:
            await manager.end(sid)
        except KeyError:
            return JSONResponse({"ok": False, "reason": "unknown session"}, status_code=404)
        hub.forget(sid)
        await _publish_hub()
        return JSONResponse({"ok": True})

    async def _hub_refresh(args: dict) -> JSONResponse:
        """The manual check for updates. Fans out across open reviews, then republishes once."""
        hub.invalidate_queue()
        try:
            await hub.refresh()
        except Exception as exc:
            return JSONResponse({"ok": False, "reason": f"{type(exc).__name__}: {exc}"},
                                status_code=502)
        hub.ensure_queue(_publish_hub)
        await _publish_hub()
        return JSONResponse({"ok": True})

    def _session_arg(args: dict) -> str | None:
        sid = args.get("session")
        return sid if isinstance(sid, str) and sid else None

    async def _review_submit(args: dict) -> JSONResponse:
        """Send the prepared review. The same sequence the REST route runs, so whichever client
        a reviewer sends from, the same comments land in the same order."""
        sid = _session_arg(args)
        if sid is None:
            return JSONResponse({"ok": False, "reason": "no session"}, status_code=400)
        if submitter is None:
            return JSONResponse({"ok": False, "reason": "review posting unavailable"},
                                status_code=400)
        result = await submitter.submit(sid, approve=bool(args.get("approve")))
        if "error" in result:
            return JSONResponse({"ok": False, "reason": result["error"]},
                                status_code=404 if result["error"] == "unknown session" else 400)
        if review is not None and args.get("approve"):
            # approving is the one thing that changes what the host would say about approvals
            with suppress(Exception):
                await review.refresh(sid)
        await bus.publish(f"review:{sid}")
        await _publish_hub()
        return JSONResponse({"ok": True, **result})

    async def _review_mark_reviewed(args: dict) -> JSONResponse:
        """Advance the reviewed watermark without sending anything — "I have read up to here"."""
        sid = _session_arg(args)
        actor = manager.get(sid) if sid else None
        if actor is None:
            return JSONResponse({"ok": False, "reason": "unknown session"}, status_code=404)
        snapshot = actor.snapshot()
        if snapshot.mr is None or kb is None:
            return JSONResponse({"ok": False, "reason": "unavailable"}, status_code=400)
        kb.set_watermark(snapshot.mr.host, snapshot.mr.project, snapshot.mr.iid, snapshot.mr.sha)
        await bus.publish(f"review:{sid}")
        await _publish_hub()
        return JSONResponse({"ok": True, "watermark": snapshot.mr.sha})

    _HANDLERS = {
        "session.open": _session_open,
        "session.close": _session_close,
        "hub.refresh": _hub_refresh,
        "review.submit": _review_submit,
        "review.mark_reviewed": _review_mark_reviewed,
    }

    async def stream(ws: WebSocket) -> None:
        """One connection: a send pump over the subscription, and this loop reading client frames.

        The client's disconnect ends the read loop, which cancels the pump — the handler then
        returns and Starlette closes the socket. Closing it here as well would be a send on an
        already-departed peer.
        """
        await ws.accept()
        async with bus.connect() as sub:
            async def pump() -> None:
                async for message in sub.drain():
                    await ws.send_text(message.model_dump_json())

            sender = asyncio.create_task(pump())
            try:
                while True:
                    try:
                        raw = await ws.receive_json()
                    except (WebSocketDisconnect, RuntimeError):
                        return       # peer gone, or the transport already torn down
                    try:
                        message = parse_client_message(raw)
                    except ValueError as exc:   # a client bug — answer it, keep the stream up
                        sub.offer(ScopeError(reason=str(exc)))
                        continue
                    if isinstance(message, Subscribe):
                        if HUB in message.scopes:
                            # start the queue read first, so the very first view a client sees
                            # already says the queue is in flight rather than idle
                            hub.ensure_queue(_publish_hub)
                        await bus.subscribe(sub, message.scopes)
                    else:
                        bus.unsubscribe(sub, message.scopes)
            finally:
                sender.cancel()
                with suppress(asyncio.CancelledError):
                    await sender

    return [
        Route("/api/cmd", cmd, methods=["POST"]),
        WebSocketRoute("/api/stream", stream),
    ]
