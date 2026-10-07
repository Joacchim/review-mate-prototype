"""Writeback — post the reviewer's own comment, anchored to a highlighted zone (D14).

The body is the reviewer's text (they may have drawn on the agent's card, but the card is never
posted). The anchor — file, new-side line, MR sha — comes from the session highlight, so the
comment lands exactly where the reviewer was looking.
"""
from __future__ import annotations

from review_mate.contracts import MRRef
from review_mate.forges import Forges
from review_mate.host.base import HostWriter
from review_mate.session.manager import SessionManager


class Writeback:
    def __init__(self, manager: SessionManager, writer: HostWriter):
        self._m = manager
        # keyed by host, like the read side: a review is written back to the forge it came from,
        # and a lone writer that names no host still answers for everything
        self._writers = Forges.of(writer)

    def _for(self, ref: MRRef):
        writer = self._writers.pick(getattr(ref, "host", None)) if self._writers else None
        if writer is None:
            raise LookupError(f"nothing configured to write to {getattr(ref, 'host', None)!r}")
        return writer

    async def post_comment(self, session_id: str, highlight_id: str | None, body: str,
                           ref: MRRef) -> dict:
        writer = self._m.get(session_id)
        if writer is None:
            raise KeyError(session_id)
        snap = writer.snapshot()
        if highlight_id is None:  # an MR-level review comment — a general note, no diff position
            return await self._for(ref).post_mr_comment(ref, body)
        hl = next((h for h in snap.highlights if h.id == highlight_id), None)
        if hl is None:
            raise KeyError(highlight_id)
        refs = snap.mr.diff_refs if snap.mr else {}
        position = {
            "new_path": hl.file,
            "new_line": hl.line_range.start,
            "base_sha": refs.get("base_sha"),
            "head_sha": refs.get("head_sha"),
            "start_sha": refs.get("start_sha"),
            "sha": snap.mr.sha if snap.mr else None,  # fallback when diff_refs absent
            # A mark made while reading an intermediate commit is about that commit, and its line
            # numbers are that commit's. Posting it against the head would land on whatever is at
            # that number now, which is the wrong line and sometimes someone else's code. The
            # forges express "on this commit" differently, so the position says which commit and
            # each adapter decides how to say it.
            "commit_sha": hl.commit_sha,
        }
        return await self._for(ref).post_comment(ref, position, body)

    async def reply(self, ref: MRRef, thread_id: str, body: str) -> dict:
        """Reply to an existing discussion thread (capability: threads)."""
        return await self._for(ref).reply(ref, thread_id, body)

    async def resolve(self, ref: MRRef, thread_id: str, resolved: bool = True) -> dict:
        """Resolve or unresolve a discussion thread — the "validate a comment" primitive."""
        return await self._for(ref).resolve(ref, thread_id, resolved)

    async def approve(self, ref: MRRef) -> dict:
        """Approve the MR (capability: approvals)."""
        return await self._for(ref).approve(ref)

    async def edit_note(self, ref: MRRef, thread_id: str, note_id: str, body: str) -> dict:
        """Edit one of the reviewer's own notes (host enforces ownership; capability: threads)."""
        return await self._for(ref).edit_note(ref, thread_id, note_id, body)

    async def delete_note(self, ref: MRRef, thread_id: str, note_id: str) -> dict:
        """Delete one of the reviewer's own notes (host enforces ownership; capability: threads)."""
        return await self._for(ref).delete_note(ref, thread_id, note_id)
