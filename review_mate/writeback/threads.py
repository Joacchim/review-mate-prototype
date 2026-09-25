"""Answering a discussion on the merge request: reply, resolve, edit, delete — and re-syncing.

Each verb is the same three steps in the same order: check the host will take it, do it, then
re-pull the discussions so what the reviewer sees is what the merge request says rather than what
this process guessed it would say. The host is the single source of truth for threads, so the
re-pull reconciles wholesale — a discussion it stops reporting is gone here too.

`Writeback` stays the thin seam onto the host. This is the sequence over it, so a second client
answering a discussion cannot get the sequence subtly different.
"""
from __future__ import annotations

from review_mate.seams import MRRef, ref_of
from review_mate.session.commands import ApplyFiles, ApplyMRMetadata, ReplaceThreads
from review_mate.session.state import Origin


class ThreadVerbs:
    def __init__(self, manager, writeback, provider=None) -> None:
        self._manager = manager
        self._writeback = writeback
        self._provider = provider

    def _source(self, ref):
        """The source that can re-read this session's change.

        Asked of the manager rather than kept here: which source understands a reference is one
        decision, and a second copy of it is a second thing to get wrong when a third kind arrives.
        A manager that names no source for this kind falls back to the one handed in, which is how
        an application can be given a provider its manager was never told about.
        """
        chosen = getattr(self._manager, "source_for", None)
        return (chosen(ref) if chosen is not None else None) or self._provider

    async def reply(self, session_id: str, thread_id: str, body: str) -> dict:
        text = (body or "").strip()
        if not text:
            return {"error": "empty reply"}
        return await self._verb(session_id,
                                lambda ref: self._writeback.reply(ref, thread_id, text))

    async def resolve(self, session_id: str, thread_id: str, resolved: bool = True) -> dict:
        answer = await self._verb(
            session_id, lambda ref: self._writeback.resolve(ref, thread_id, resolved))
        return answer if "error" in answer else {**answer, "resolved": resolved}

    async def edit_note(self, session_id: str, thread_id: str, note_id: str, body: str) -> dict:
        text = (body or "").strip()
        if not text:
            return {"error": "empty note"}
        return await self._verb(
            session_id, lambda ref: self._writeback.edit_note(ref, thread_id, note_id, text))

    async def delete_note(self, session_id: str, thread_id: str, note_id: str) -> dict:
        return await self._verb(
            session_id, lambda ref: self._writeback.delete_note(ref, thread_id, note_id))

    async def resync(self, session_id: str) -> dict:
        """Re-pull the whole change from the host: head, files and discussions.

        Threads alone is not enough and used to be what this did. A head left frozen at the moment
        the session opened compares equal to the reviewed watermark for ever, so a merge request
        that moved on never read as moved on and reviewing only the new part never engaged.
        """
        actor, ref, failure = self._target(session_id)
        if actor is None or ref is None:
            return failure or {"error": "unknown session"}
        source = self._source(ref)
        if source is not None and hasattr(source, "load"):
            payload = await source.load(ref)
            await actor.submit(ApplyMRMetadata(mr=payload.mr), Origin.SYSTEM)
            await actor.submit(ApplyFiles(files=payload.files), Origin.SYSTEM)
            await actor.submit(ReplaceThreads(threads=payload.threads), Origin.SYSTEM)
            return {"ok": True, "head": payload.mr.sha, "threads": len(payload.threads)}
        # a host that cannot re-read the whole change can still re-read the discussions
        mirrored = await self._remirror(actor, ref)
        snapshot = actor.snapshot()
        return {"ok": True, "head": snapshot.mr.sha if snapshot.mr else "", "threads": mirrored}

    # --- the shape every verb shares -----------------------------------------

    async def _verb(self, session_id: str, act) -> dict:
        actor, ref, failure = self._target(session_id, needs_writer=True)
        if actor is None or ref is None:
            return failure or {"error": "unknown session"}
        try:
            await act(ref)
        except Exception as exc:
            return {"error": str(exc)}
        await self._remirror(actor, ref)
        return {"ok": True}

    def _target(self, session_id: str, needs_writer: bool = False):
        """The session and the change a verb acts on, or why it cannot."""
        actor = self._manager.get(session_id)
        if actor is None:
            return None, None, {"error": "unknown session"}
        if needs_writer and self._writeback is None:
            return None, None, {"error": "review posting unavailable"}
        snapshot = actor.snapshot()
        if snapshot.mr is None:
            return None, None, {"error": "no MR loaded"}
        ref = ref_of(snapshot)
        if needs_writer and not isinstance(ref, MRRef):
            # every verb but re-syncing writes to a merge request, and a branch that has not left
            # this machine has none — refusing says that, where reaching for the writer anyway
            # would send a repository path to a forge and report whatever it made of it
            return None, None, {"error": "this review has no merge request to write to"}
        return actor, ref, None

    async def _remirror(self, actor, ref) -> int:
        """Re-mirror the discussions and say how many came back."""
        source = self._source(ref)
        if source is None or not hasattr(source, "fetch_threads"):
            return 0
        threads = await source.fetch_threads(ref)
        await actor.submit(ReplaceThreads(threads=threads), Origin.SYSTEM)
        return len(threads)
