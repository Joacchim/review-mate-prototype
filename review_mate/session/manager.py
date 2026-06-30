"""SessionManager — session lifecycle and discovery.

Owns the set of live `SessionActor`s keyed by id; creates them (seeding the first
`SessionCreated` event), restores them on startup by replaying their logs, lists and ends them.
Persistence lives under the `~/.review-mate/sessions/<id>/` workspace boundary.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from review_mate.config import sessions_dir
from review_mate.seams import MRRef
from review_mate.session import events as ev
from review_mate.session.actor import SessionActor
from review_mate.session.commands import (
    ApplyFiles, ApplyMRMetadata, ApplyThread, EndSession,
)
from review_mate.session.eventlog import EventLog
from review_mate.session.reducer import fold
from review_mate.session.state import (
    DraftStatus, Origin, SessionState, SessionStatus, SessionSummary,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionManager:
    def __init__(self, root: Path | str | None = None, mr_source=None, workspace=None,
                 activity_broker=None):
        self.root = Path(root) if root is not None else sessions_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self._actors: dict[str, SessionActor] = {}
        self._mr_source = mr_source   # MRSource seam (optional, injected) — host-adapter impl
        self._workspace = workspace   # Workspace seam (optional, injected) — workspace-manager impl
        self._activity_broker = activity_broker  # ActivityBroker (optional) — review-fleet notify spine
        self._republishers: list[asyncio.Task] = []  # per-actor taps feeding the activity channel

    # --- lifecycle ----------------------------------------------------------

    async def create(self, ref=None) -> str:
        sid = uuid.uuid4().hex
        sdir = self.root / sid
        sdir.mkdir(parents=True, exist_ok=True)
        created = _now()
        self._write_meta(sdir, sid, created, SessionStatus.ACTIVE)

        log = EventLog(sdir / "events.jsonl")
        state = SessionState(id=sid, created_at=created)
        created_event = ev.SessionCreated(ts=created, origin=Origin.SYSTEM)
        log.append(created_event)              # seq 1
        state = fold(state, [created_event])

        actor = SessionActor(sid, log, state)
        actor.start()
        self._actors[sid] = actor

        if ref is not None and self._mr_source is not None:
            try:
                await self.load(sid, ref)
            except Exception:
                await self._discard(sid)  # don't leave an orphaned, MR-less ACTIVE session
                raise
        self._attach_republisher(actor, sid)
        return sid

    def _attach_republisher(self, actor: SessionActor, sid: str) -> None:
        """Tap the actor's event stream and republish highlight/message events to the activity
        channel, so one watcher covers every session (review-fleet). No-op without a broker.
        Subscribes from the current seq, so a restored session's historical events are not
        re-announced as fresh activity. Ends when the actor closes subscribers on SessionEnded."""
        broker = self._activity_broker
        if broker is None:
            return
        since = actor.snapshot().seq

        async def _pump() -> None:
            async for event in actor.subscribe(since=since):
                if isinstance(event, ev.HighlightAdded):
                    broker.publish("highlight_added", session_id=sid)
                elif isinstance(event, ev.MessagePosted):
                    broker.publish("message_posted", session_id=sid)

        self._republishers.append(asyncio.create_task(_pump()))

    async def _discard(self, session_id: str) -> None:
        actor = self._actors.pop(session_id, None)
        if actor is not None:
            await actor.stop()
        shutil.rmtree(self.root / session_id, ignore_errors=True)

    async def load(self, session_id: str, ref: MRRef) -> None:
        """Populate a session from the host seam (SYSTEM origin). No-op if no MRSource injected."""
        actor = self._actors.get(session_id)
        if actor is None:
            raise KeyError(session_id)
        if self._mr_source is None:
            raise RuntimeError("no MRSource configured")
        payload = await self._mr_source.load(ref)
        await actor.submit(ApplyMRMetadata(mr=payload.mr), Origin.SYSTEM)
        await actor.submit(ApplyFiles(files=payload.files), Origin.SYSTEM)
        for thread in payload.threads:
            await actor.submit(ApplyThread(thread=thread), Origin.SYSTEM)

    async def restore_all(self) -> None:
        for sdir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            log_path = sdir / "events.jsonl"
            if not log_path.exists() or sdir.name in self._actors:
                continue
            try:
                meta = self._read_meta(sdir)
                log = EventLog(log_path)
                state = fold(SessionState(id=sdir.name, created_at=meta.get("created_at", "")),
                             list(log.replay()))
                actor = SessionActor(sdir.name, log, state)
                actor.start()
                self._actors[sdir.name] = actor
                self._attach_republisher(actor, sdir.name)
            except Exception:
                # one unreadable/corrupt session must not stop the server from booting
                logger.warning("skipping unrestorable session %s", sdir.name, exc_info=True)

    async def end(self, session_id: str) -> None:
        actor = self._actors.get(session_id)
        if actor is None:
            raise KeyError(session_id)
        await actor.submit(EndSession(), Origin.BROWSER)
        self._write_meta(self.root / session_id, session_id,
                         actor.snapshot().created_at, SessionStatus.ENDED)

    async def shutdown(self) -> None:
        for task in self._republishers:
            task.cancel()
        self._republishers.clear()
        for actor in list(self._actors.values()):
            await actor.stop()
        self._actors.clear()

    # --- reads --------------------------------------------------------------

    def get(self, session_id: str) -> SessionActor | None:
        return self._actors.get(session_id)

    def list(self) -> list[SessionSummary]:
        out: list[SessionSummary] = []
        for actor in self._actors.values():
            s = actor.snapshot()
            out.append(SessionSummary(
                id=s.id, status=s.status, created_at=s.created_at, seq=s.seq,
                title=s.mr.title if s.mr else None,
                project=s.mr.project if s.mr else None,
                iid=s.mr.iid if s.mr else None,
                highlights=len(s.highlights), cards=len(s.cards),
                drafts_pending=sum(1 for d in s.drafts if d.status is DraftStatus.DRAFT),
                drafts_posted=sum(1 for d in s.drafts if d.status is DraftStatus.POSTED),
            ))
        return out

    # --- meta --------------------------------------------------------------

    @staticmethod
    def _write_meta(sdir: Path, sid: str, created: str, status: SessionStatus) -> None:
        (sdir / "meta.json").write_text(
            json.dumps({"id": sid, "created_at": created, "status": status.value})
        )

    @staticmethod
    def _read_meta(sdir: Path) -> dict:
        p = sdir / "meta.json"
        return json.loads(p.read_text()) if p.exists() else {}
