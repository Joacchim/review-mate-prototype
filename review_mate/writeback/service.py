"""Writeback — post the reviewer's own comment, anchored to a highlighted zone (D14).

The body is the reviewer's text (they may have drawn on the agent's card, but the card is never
posted). The anchor — file, new-side line, MR sha — comes from the session highlight, so the
comment lands exactly where the reviewer was looking.
"""
from __future__ import annotations

from review_mate.contracts import MRRef
from review_mate.view.diffdoc import parse
from review_mate.forges import Forges
from review_mate.host.base import HostWriter
from review_mate.session.manager import SessionManager


def _old_side(entry, new_line: int) -> dict:
    """`old_path` and `old_line` for a line, as far as the diff can say.

    An added line has no old side and the keys are left out; an unchanged line has one and the
    forge needs it. `old_path` travels with either, because a renamed file is a different name on
    the two sides and the one a position is read against is the old one.
    """
    if entry is None:
        return {}
    old_path = getattr(entry, "old_path", None) or entry.path
    for hunk in parse("\n".join(h.get("diff", "") for h in (entry.hunks or []))):
        for line in hunk.lines:
            if line.new == new_line:
                return ({"old_path": old_path, "old_line": line.old}
                        if line.old is not None else {"old_path": old_path})
    return {"old_path": old_path}


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
        entry = next((f for f in (snap.files or []) if f.path == hl.file), None)
        position = {
            "new_path": hl.file,
            "new_line": hl.line_range.start,
            # Where that line was before the change, when it was there before the change at all.
            # A forge builds its line identifier from both sides, and for a line the change did
            # not touch it cannot infer the old one — so a comment anchored on an unchanged line,
            # which is most of what a reviewer marks when quoting the code around a problem, was
            # refused outright while one on an added line went through.
            **_old_side(entry, hl.line_range.start),
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
