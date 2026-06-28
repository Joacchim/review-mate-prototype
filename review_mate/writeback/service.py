"""Writeback — post the reviewer's own comment, anchored to a highlighted zone (D14).

The body is the reviewer's text (they may have drawn on the agent's card, but the card is never
posted). The anchor — file, new-side line, MR sha — comes from the session highlight, so the
comment lands exactly where the reviewer was looking.
"""
from __future__ import annotations

from review_mate.host.base import HostWriter
from review_mate.seams import MRRef
from review_mate.session.manager import SessionManager


class Writeback:
    def __init__(self, manager: SessionManager, writer: HostWriter):
        self._m = manager
        self._writer = writer

    async def post_comment(self, session_id: str, highlight_id: str | None, body: str,
                           ref: MRRef) -> dict:
        actor = self._m.get(session_id)
        if actor is None:
            raise KeyError(session_id)
        snap = actor.snapshot()
        if highlight_id is None:  # an MR-level review comment — a general note, no diff position
            return await self._writer.post_mr_comment(ref, body)
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
        }
        return await self._writer.post_comment(ref, position, body)
