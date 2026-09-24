"""The `tree` and `commits` scopes: the repository around the change, and how it got here.

Both answer a question a reviewer asks only sometimes — what else is in this repo, and what were
the individual steps — and both cost a host read to answer. So both follow the shape the hub
established: `build` reports what is known, an explicit fetch goes and asks, and the fetch runs
when someone starts watching rather than on every rebuild.

That gating is the point of putting them here at all. A reviewer who never opens the file browser
never pays for the tree, and one who never reviews per commit never pays for the commit list —
which a route cannot arrange, because a route is asked whether anyone is looking or not.

A tree is read at a sha and a sha's contents cannot change, so what is cached never needs
invalidating. The commit list can change under a session whose head moved, so re-syncing drops it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from review_mate.seams import MRRef
from review_mate.session.state import SessionStatus


class TreeView(BaseModel):
    session: str
    state: str = "idle"            # idle | loading | ready | unavailable | error | unknown-session
    sha: str = ""
    paths: list[str] = Field(default_factory=list)
    error: str = ""


class CommitRow(BaseModel):
    sha: str
    short_id: str = ""
    title: str = ""
    message: str = ""
    author: str = ""
    created_at: str = ""


class CommitsView(BaseModel):
    session: str
    state: str = "idle"            # idle | loading | ready | unavailable | error | unknown-session
    commits: list[CommitRow] = Field(default_factory=list)
    error: str = ""


class BrowseScopes:
    """Builds both, and owns what each has read.

    Neither `build` reaches the host. `fetch_tree` and `fetch_commits` do, once, and republish when
    the answer lands — so a view says `loading` while it is on its way rather than blocking on it.
    """

    def __init__(self, manager, provider=None, publish=None) -> None:
        self._manager = manager
        self._provider = provider
        self._publish = publish              # publish(scope) -> awaitable
        self._trees: dict[str, list[str]] = {}     # sha -> paths
        self._tree_state: dict[str, str] = {}      # sha -> loading | ready | error
        self._tree_error: dict[str, str] = {}
        self._commits: dict[str, list[dict]] = {}  # session id -> rows
        self._commits_state: dict[str, str] = {}
        self._commits_error: dict[str, str] = {}

    # --- the repository's files ----------------------------------------------

    async def build_tree(self, session_id: str) -> dict:
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return TreeView(session=session_id, state="unknown-session").model_dump(mode="json")
        sha = snapshot.mr.sha if snapshot.mr else ""
        if not sha or not self._can("get_repo_tree"):
            return TreeView(session=session_id, state="unavailable").model_dump(mode="json")
        state = self._tree_state.get(sha, "idle")
        return TreeView(session=session_id, state=state, sha=sha,
                        paths=list(self._trees.get(sha) or []),
                        error=self._tree_error.get(sha, "")).model_dump(mode="json")

    async def fetch_tree(self, session_id: str) -> None:
        snapshot = self._snapshot(session_id)
        if snapshot is None or snapshot.mr is None or self._provider is None:
            return
        sha = snapshot.mr.sha
        if not sha or not self._can("get_repo_tree") or sha in self._tree_state:
            return                            # already asked, or nothing to ask about
        self._tree_state[sha] = "loading"
        await self._republish(f"tree:{session_id}")
        try:
            self._trees[sha] = list(await self._provider.get_repo_tree(snapshot.mr.project, sha))
            self._tree_state[sha] = "ready"
        except Exception as exc:
            self._tree_state[sha] = "error"
            self._tree_error[sha] = str(exc)
        await self._republish(f"tree:{session_id}")

    # --- the change's commits -------------------------------------------------

    async def build_commits(self, session_id: str) -> dict:
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return CommitsView(session=session_id, state="unknown-session").model_dump(mode="json")
        if not self._commits_supported(snapshot):
            return CommitsView(session=session_id, state="unavailable").model_dump(mode="json")
        return CommitsView(
            session=session_id, state=self._commits_state.get(session_id, "idle"),
            commits=[CommitRow(**row) for row in (self._commits.get(session_id) or [])],
            error=self._commits_error.get(session_id, ""),
        ).model_dump(mode="json")

    async def fetch_commits(self, session_id: str) -> None:
        snapshot = self._snapshot(session_id)
        if snapshot is None or snapshot.mr is None or self._provider is None:
            return
        if not self._commits_supported(snapshot):
            return
        if self._commits_state.get(session_id) in ("loading", "ready"):
            return
        self._commits_state[session_id] = "loading"
        await self._republish(f"commits:{session_id}")
        ref = MRRef(host=snapshot.mr.host, project=snapshot.mr.project, iid=snapshot.mr.iid)
        try:
            rows = await self._provider.commits(ref)
            self._commits[session_id] = [self._row(r) for r in rows]
            self._commits_state[session_id] = "ready"
        except Exception as exc:
            self._commits_state[session_id] = "error"
            self._commits_error[session_id] = str(exc)
        await self._republish(f"commits:{session_id}")

    def forget_commits(self, session_id: str) -> None:
        """A head that moved has a different list, so a re-sync drops what was read."""
        self._commits.pop(session_id, None)
        self._commits_state.pop(session_id, None)
        self._commits_error.pop(session_id, None)

    # --- plumbing -------------------------------------------------------------

    @staticmethod
    def _row(raw) -> dict:
        row = dict(raw) if isinstance(raw, dict) else {}
        return {field: row.get(field, "") for field in CommitRow.model_fields}

    def _commits_supported(self, snapshot) -> bool:
        cap = bool(snapshot.mr and (snapshot.mr.capabilities or {}).get("commits", False))
        return bool(snapshot.mr) and cap and self._can("commits")

    def _can(self, method: str) -> bool:
        return self._provider is not None and hasattr(self._provider, method)

    async def _republish(self, scope: str) -> None:
        if self._publish is not None:
            await self._publish(scope)

    def _snapshot(self, session_id: str):
        actor = self._manager.get(session_id)
        if actor is None:
            return None
        snapshot = actor.snapshot()
        return snapshot if snapshot.status is SessionStatus.ACTIVE else None
