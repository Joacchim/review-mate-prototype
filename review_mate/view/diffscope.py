"""The `diff` and `blob` scopes: reading a change.

One family, addressed by how much of the change is being asked for:

    diff:<sid>:<mode>            the file list and the MR — small, and it moves when the review does
    diff:<sid>:<mode>:<path>     one file's hunks and tokens

A file's name is the list's name with a path appended, so a client that holds one can address the
other by concatenation rather than by assembling a second name and keeping it consistent. They stay
separate scopes because the unit of change is the unit of transfer: re-reading one file moves that
file and not the other thirty-nine.

**Mode is part of the name rather than state.** Which version of the change a reviewer is reading —
the whole thing, only what arrived since they last looked, a single commit — is a property of the
reader, not of the session. Putting it in the name means two clients can read the same review at
different modes without contending over one field, and switching mode is a subscription rather than
a command.

Unfolding the context between hunks is served by a second family:

    blob:<sid>:<mode>:<path>     the whole file at the resolved sha, numbered and tokenized

The content a reviewer unfolds into is immutable at a sha — it is not reader state. What *is*
reader state is how much of it they chose to reveal, and that is presentation, so the client slices
the lines it wants out of a blob it already holds. Line numbers are the diff's new-side numbers,
so a client splices revealed lines straight into a gap without translating anything.

Names are validated rather than trusted. A path may contain colons and a session id or mode may
not, so a malformed name would otherwise mis-split into a plausible-looking path and be answered
with a confident "no such file".
"""
from __future__ import annotations

import asyncio
import re
from contextlib import suppress

from pydantic import BaseModel, Field

from review_mate.session.state import SessionStatus
from review_mate.view.diffdoc import build as build_hunks
from review_mate.view.tokens import tokenize

FULL = "full"
SINCE = "since"
COMMIT_PREFIX = "commit@"

# A session id is opaque but must not carry the separator; a mode is a closed set, with the commit
# form using "@" precisely so that a sha needs no second colon.
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MODE = re.compile(r"^(?:full|since|commit@[0-9a-fA-F]{7,40})$")


class Address:
    """A parsed `diff` scope name. `path` is None for the file list."""

    __slots__ = ("session", "mode", "path")

    def __init__(self, session: str, mode: str, path: str | None) -> None:
        self.session, self.mode, self.path = session, mode, path


def parse_address(argument: str) -> Address | None:
    """The address a `diff:` scope name carries, or None when the name is not well formed."""
    session, separator, rest = argument.partition(":")
    if not separator:
        return None
    mode, has_path, path = rest.partition(":")
    if not _SESSION.match(session) or not _MODE.match(mode):
        return None
    if has_path and not path:
        return None                      # a trailing separator is a malformed name, not the list
    return Address(session, mode, path if has_path else None)


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

    # --- diff:<sid>:<mode>[:<path>] ---------------------------------------

    async def build(self, argument: str) -> dict:
        """One entry point for the family: the arity of the name says which view is wanted."""
        address = parse_address(argument)
        if address is None:
            return DiffView(session="", state="malformed-name").model_dump(mode="json")
        if address.path is None:
            return await self._build_list(address)
        return await self._build_file(address)

    async def _build_list(self, address: Address) -> dict:
        session_id, mode = address.session, address.mode
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

    async def _build_file(self, address: Address) -> dict:
        session_id, mode, path = address.session, address.mode, address.path
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


class BlobLine(BaseModel):
    n: int
    text: str = ""
    tokens: list[list] = Field(default_factory=list)


class BlobView(BaseModel):
    session: str
    mode: str = FULL
    path: str = ""
    sha: str = ""
    language: str | None = None
    state: str = "loading"     # loading | ready | unknown-session | unknown-file | error
                               # | unsupported-mode | malformed-name | unavailable
    error: str = ""
    lines: list[BlobLine] = Field(default_factory=list)


class BlobScopes:
    """Whole-file content for unfolding, keyed by the sha a mode resolves to.

    Reading a blob is a host call, so `build` never performs one: it reports `loading` and starts a
    one-shot fetch that republishes when it lands — the same shape the hub's queue uses. Content at
    a fixed sha cannot change, so what it caches never needs invalidating.
    """

    def __init__(self, manager, provider=None, publish=None) -> None:
        self._manager = manager
        self._provider = provider
        self._publish = publish
        self._content: dict[tuple[str, str], str] = {}     # (sha, path) -> text
        self._failed: dict[tuple[str, str], str] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    async def build(self, argument: str) -> dict:
        address = parse_address(argument)
        if address is None or address.path is None:
            return BlobView(session="", state="malformed-name").model_dump(mode="json")
        session_id, mode, path = address.session, address.mode, address.path
        actor = self._manager.get(session_id)
        snapshot = actor.snapshot() if actor is not None else None
        if snapshot is None or snapshot.status is not SessionStatus.ACTIVE or snapshot.mr is None:
            return BlobView(session=session_id, mode=mode, path=path,
                            state="unknown-session").model_dump(mode="json")
        sha = self._sha_for(mode, snapshot)
        if sha is None:
            return BlobView(session=session_id, mode=mode, path=path,
                            state="unsupported-mode").model_dump(mode="json")
        language = next((f.language for f in (snapshot.files or []) if f.path == path), None)
        view = BlobView(session=session_id, mode=mode, path=path, sha=sha, language=language)
        key = (sha, path)
        if key in self._failed:
            view.state, view.error = "error", self._failed[key]
            return view.model_dump(mode="json")
        text = self._content.get(key)
        if text is None:
            if self._provider is None or not hasattr(self._provider, "get_file"):
                view.state = "unavailable"          # no host configured to read a blob from
                return view.model_dump(mode="json")
            self._start(key, snapshot.mr.project, f"blob:{argument}")
            return view.model_dump(mode="json")     # state stays "loading"
        spans = tokenize(text, path, language)
        view.lines = [BlobLine(n=index + 1, text=line, tokens=line_spans)
                      for index, (line, line_spans) in enumerate(zip(text.split("\n"), spans))]
        view.state = "ready"
        return view.model_dump(mode="json")

    def _sha_for(self, mode: str, snapshot) -> str | None:
        """The commit a mode reads its content at.

        `since` shares the full mode's sha deliberately: since_diff keeps the MR head as its new
        side, so its line numbers are head coordinates and the content to unfold into is the head's.
        """
        if mode in (FULL, SINCE):
            return snapshot.mr.sha
        if mode.startswith(COMMIT_PREFIX):
            return mode[len(COMMIT_PREFIX):]
        return None

    def _start(self, key: tuple[str, str], project: str, scope: str) -> None:
        if key in self._tasks:
            return
        task = asyncio.create_task(self._fetch(key, project, scope))
        self._tasks[key] = task
        task.add_done_callback(lambda finished: self._done(key, finished))

    def _done(self, key, task) -> None:
        self._tasks.pop(key, None)
        if not task.cancelled():
            task.exception()      # retrieve it; failures are already recorded in the view

    async def _fetch(self, key: tuple[str, str], project: str, scope: str) -> None:
        sha, path = key
        try:
            self._content[key] = await self._provider.get_file(project, path, sha)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed[key] = f"{type(exc).__name__}: {exc}"
        if self._publish is not None:
            await self._publish(scope)

    async def aclose(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
