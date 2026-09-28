"""The view-protocol client: one websocket subscription, one command post.

Holds no review logic of any kind. It keeps the latest view per topic and calls back when one
changes; every field it hands the renderer was decided server-side. A reconnect re-subscribes
and is sent each topic's current view, so there is no local state to reconcile.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import websockets

RECONNECT_DELAYS = (0.5, 1.0, 2.0, 5.0)


class ViewClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765") -> None:
        self.base_url = base_url.rstrip("/")
        self._socket = None
        self.views: dict[str, dict[str, Any]] = {}
        self.seqs: dict[str, int] = {}
        self.errors: dict[str, str] = {}
        self.status = "connecting"          # connecting | live | reconnecting | offline
        self.last_command_error = ""
        self._topics: list[str] = []
        self._on_change: Callable[[], None] = lambda: None

    @property
    def topics(self) -> list[str]:
        """What the client is subscribed to, across reconnects."""
        return list(self._topics)

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        return f"{scheme}://{self.base_url.split('://', 1)[1]}/api/stream"

    async def run(self, topics: list[str], on_change: Callable[[], None]) -> None:
        """Stay subscribed to `topics` for as long as the caller runs, reconnecting as needed."""
        self._topics = list(topics)
        self._on_change = on_change
        attempt = 0
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    attempt = 0
                    self._socket = ws
                    self.status = "live"
                    # a reconnect re-subscribes to everything currently wanted, and each topic's
                    # present view arrives again — there is no local state to reconcile
                    await ws.send(json.dumps({"action": "subscribe", "topics": self._topics}))
                    on_change()
                    async for raw in ws:
                        self._absorb(raw)
                        on_change()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._socket = None
                self.status = "reconnecting" if attempt < len(RECONNECT_DELAYS) else "offline"
                self.last_command_error = f"{type(exc).__name__}: {exc}"
                on_change()
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def watch(self, topics: list[str]) -> None:
        """Add topics to the subscription, now and across any later reconnect."""
        fresh = [topic for topic in topics if topic not in self._topics]
        if not fresh:
            return
        self._topics.extend(fresh)
        if self._socket is not None:
            try:
                await self._socket.send(json.dumps({"action": "subscribe", "topics": fresh}))
            except Exception:
                pass          # the reconnect will re-subscribe the whole set

    async def unwatch(self, topics: list[str]) -> None:
        drop = [topic for topic in topics if topic in self._topics]
        if not drop:
            return
        self._topics = [topic for topic in self._topics if topic not in drop]
        for topic in drop:
            self.views.pop(topic, None)
            self.errors.pop(topic, None)
        if self._socket is not None:
            try:
                await self._socket.send(json.dumps({"action": "unsubscribe", "topics": drop}))
            except Exception:
                pass

    def _absorb(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return                       # a frame we cannot read is not a reason to drop the stream
        kind = message.get("type")
        topic = message.get("topic")
        if kind == "topic" and isinstance(topic, str):
            self.views[topic] = message.get("view") or {}
            self.seqs[topic] = message.get("seq", 0)
            self.errors.pop(topic, None)
        elif kind == "error":
            self.errors[topic or ""] = message.get("reason", "unknown error")

    async def session_command(self, session: str, command: dict) -> bool:
        """A command about the contents of one review — a highlight, a draft, a message.

        Separate from `command` because the two write paths address different things: that one
        addresses the set of reviews and the host, this one addresses what is inside one.
        """
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as http:
                response = await http.post(f"/api/sessions/{session}/commands", json=command)
        except Exception as exc:
            self.last_command_error = f"{command.get('type')}: {type(exc).__name__}: {exc}"
            return False
        if response.status_code == 200 and response.json().get("ok"):
            self.last_command_error = ""
            return True
        try:
            reason = response.json().get("reason", response.text)
        except ValueError:
            reason = response.text
        self.last_command_error = f"{command.get('type')}: {reason}"
        return False

    async def command(self, cmd: str, **args: Any) -> bool:
        """Send one command. Returns whether it was accepted, and records why if it was not."""
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as http:
                response = await http.post("/api/cmd", json={"cmd": cmd, "args": args})
        except Exception as exc:
            self.last_command_error = f"{cmd}: {type(exc).__name__}: {exc}"
            return False
        if response.status_code == 200 and response.json().get("ok"):
            self.last_command_error = ""
            return True
        try:
            reason = response.json().get("reason", response.text)
        except ValueError:
            reason = response.text
        self.last_command_error = f"{cmd}: {reason}"
        return False
