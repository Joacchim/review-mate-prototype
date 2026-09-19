"""The `diff` and `file` scopes: reading a change.

Two scopes, because the unit of change is the unit of transfer. `diff:<sid>:<mode>` carries the
file list and the MR — small, and it changes when the review does. `file:<sid>:<mode>:<path>`
carries one file's hunks and tokens, so unfolding or re-reading a file moves that file and not the
other thirty-nine.

**Mode is part of the name rather than state.** Which version of the change a reviewer is reading —
the whole thing, only what arrived since they last looked, a single commit — is a property of the
reader, not of the session. Putting it in the name means two clients can read the same review at
different modes without contending over one field, and switching mode is a subscription rather than
a command.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from review_mate.session.state import SessionStatus
from review_mate.view.diffdoc import build as build_hunks

FULL = "full"


class FileRow(BaseModel):
    path: str
    old_path: str | None = None
    change_type: str = ""
    language: str | None = None
    additions: int = 0
    deletions: int = 0
    has_diff: bool = True


class DiffView(BaseModel):
    session: str
    mode: str = FULL
    state: str = "ready"               # ready | unknown-session | unsupported-mode
    mr: dict = Field(default_factory=dict)
    files: list[FileRow] = Field(default_factory=list)


class FileView(BaseModel):
    session: str
    mode: str = FULL
    path: str = ""
    old_path: str | None = None
    change_type: str = ""
    language: str | None = None
    state: str = "ready"               # ready | unknown-session | unknown-file | unsupported-mode
    hunks: list[dict] = Field(default_factory=list)


def _counts(diff_text: str) -> tuple[int, int]:
    """Additions and deletions, without parsing the whole file — the list view only needs numbers."""
    additions = deletions = 0
    for line in (diff_text or "").split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def _diff_text(entry) -> str:
    return "\n".join(hunk.get("diff", "") for hunk in (entry.hunks or []))


class DiffScopes:
    """Builders for both scopes. Reads session state only — no host call, no git."""

    def __init__(self, manager) -> None:
        self._manager = manager

    # --- diff:<sid>:<mode> ------------------------------------------------

    async def build_diff(self, argument: str) -> dict:
        session_id, _, mode = argument.partition(":")
        mode = mode or FULL
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return DiffView(session=session_id, mode=mode, state="unknown-session").model_dump(mode="json")
        if mode != FULL:
            return DiffView(session=session_id, mode=mode,
                            state="unsupported-mode").model_dump(mode="json")
        rows = []
        for entry in snapshot.files or []:
            additions, deletions = _counts(_diff_text(entry))
            rows.append(FileRow(path=entry.path, old_path=entry.old_path,
                                change_type=getattr(entry.change_type, "value", "") or "",
                                language=entry.language, additions=additions, deletions=deletions,
                                has_diff=bool(_diff_text(entry).strip())))
        mr = snapshot.mr.model_dump(mode="json") if snapshot.mr else {}
        return DiffView(session=session_id, mode=mode, mr=mr, files=rows).model_dump(mode="json")

    # --- file:<sid>:<mode>:<path> -----------------------------------------

    async def build_file(self, argument: str) -> dict:
        parts = argument.split(":", 2)
        if len(parts) < 3:
            return FileView(session=parts[0] if parts else "", state="unknown-file").model_dump(mode="json")
        session_id, mode, path = parts
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return FileView(session=session_id, mode=mode, path=path,
                            state="unknown-session").model_dump(mode="json")
        if mode != FULL:
            return FileView(session=session_id, mode=mode, path=path,
                            state="unsupported-mode").model_dump(mode="json")
        entry = next((f for f in (snapshot.files or []) if f.path == path), None)
        if entry is None:
            return FileView(session=session_id, mode=mode, path=path,
                            state="unknown-file").model_dump(mode="json")
        hunks = build_hunks(_diff_text(entry), entry.path, entry.language)
        return FileView(session=session_id, mode=mode, path=entry.path, old_path=entry.old_path,
                        change_type=getattr(entry.change_type, "value", "") or "",
                        language=entry.language,
                        hunks=[hunk.model_dump(mode="json") for hunk in hunks]).model_dump(mode="json")

    # --- internals ---------------------------------------------------------

    def _snapshot(self, session_id: str):
        actor = self._manager.get(session_id)
        if actor is None:
            return None
        snapshot = actor.snapshot()
        return snapshot if snapshot.status is SessionStatus.ACTIVE else None
