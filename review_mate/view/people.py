"""The `people` topic: who a reviewer can put a name to.

A mention only reaches anyone if the handle is exactly right, so the point of this is to insert a
real one rather than to let somebody type a guess. Two sources, and they are not the same kind of
answer:

**Who is already on the change** — the author, and everyone who has written in a discussion. These
are exact forge handles the session already holds, they cost nothing, and they cannot go stale.
They are also most of what a reviewer mentions: the author, and whoever is on the thread.

**Who is in the project** — a host read, and the only way to pull in somebody not yet involved. It
is read the first time a reviewer starts a mention and not before, the same bargain the file tree
and the commit list make; and it is often refused outright, because GitHub asks for push access to
say who a repository's collaborators are. So it rides behind the first source rather than replacing
it, and its state is reported separately: a reviewer whose forge will not answer still gets the
people on the change, and nothing above here has to pretend the list is complete when it is not.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from review_mate.contracts import ref_of
from review_mate.forges import Forges
from review_mate.session.state import SessionStatus


class Person(BaseModel):
    username: str
    name: str = ""
    on_change: bool = False      # the author, or has written in a discussion here


class PeopleView(BaseModel):
    session: str
    state: str = "ready"         # ready | unknown-session
    people: list[Person] = Field(default_factory=list)
    # the project's own list, separately: it is the half that can be missing, and a client that
    # cannot tell "still coming" from "refused" will either warn too early or never warn at all
    members: str = "idle"        # idle | loading | ready | unavailable | error
    error: str = ""


class PeopleTopic:
    def __init__(self, manager, provider=None, publish=None) -> None:
        self._manager = manager
        self._forges = Forges.of(provider)
        self._publish = publish
        self._members: dict[tuple, list[dict]] = {}    # (host, project) -> rows
        self._state: dict[tuple, str] = {}
        self._error: dict[tuple, str] = {}

    async def build(self, session_id: str) -> dict:
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return PeopleView(session=session_id, state="unknown-session").model_dump(mode="json")
        key = self._key(snapshot)
        here = self._on_change(snapshot)
        people = [Person(username=u, name=n, on_change=True) for u, n in here.items()]
        for row in self._members.get(key) or []:
            if row["username"] not in here:
                people.append(Person(username=row["username"], name=row.get("name", "")))
        state = "unavailable" if not self._supported(snapshot) else self._state.get(key, "idle")
        return PeopleView(session=session_id, people=people, members=state,
                          error=self._error.get(key, "")).model_dump(mode="json")

    async def fetch(self, session_id: str) -> None:
        """Read the project's members, once. Called when a reviewer starts a mention."""
        snapshot = self._snapshot(session_id)
        if snapshot is None or not self._supported(snapshot):
            return
        key = self._key(snapshot)
        if self._state.get(key) in ("loading", "ready"):
            return
        self._state[key] = "loading"
        await self._republish(session_id)
        try:
            self._members[key] = list(await self._forge(snapshot).members(ref_of(snapshot)))
            self._state[key] = "ready"
        except Exception as exc:
            # The ordinary case on GitHub, not an accident: a reviewer without push access cannot
            # be told who the collaborators are. The people on the change still stand.
            self._state[key] = "error"
            self._error[key] = f"{type(exc).__name__}: {exc}"
        await self._republish(session_id)

    async def refresh(self, session_id: str) -> None:
        """Read the member list again, because the reviewer says somebody is missing.

        Asked for and never inferred. A list goes out of date exactly when somebody joins, which
        is also when a mention of them fails — but the tool cannot tell that from a typo, and
        guessing wrong either spends a call on every slip or leaves the reviewer with a list they
        have no reason to distrust. So the reviewer is given the control and told where it is.
        """
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return
        key = self._key(snapshot)
        self._members.pop(key, None)
        self._state.pop(key, None)
        self._error.pop(key, None)
        await self.fetch(session_id)

    def reset(self) -> None:
        """Forget every project's member list. For a test harness that swaps the forge under a
        long-lived server — in a real one the cache is the point, and nothing calls this."""
        self._members.clear()
        self._state.clear()
        self._error.clear()

    # --- what the session already knows -------------------------------------

    @staticmethod
    def _on_change(snapshot) -> dict[str, str]:
        """Handle -> display name, for everyone already part of this change.

        The author first, because they are who a review comment is usually addressed to, and a
        dict keeps that order while making the membership test the thing it reads as.
        """
        here: dict[str, str] = {}
        if snapshot.mr and snapshot.mr.author:
            here[snapshot.mr.author] = ""
        for thread in snapshot.threads or []:
            for comment in thread.comments or []:
                if comment.author:
                    here.setdefault(comment.author, "")
        return here

    @staticmethod
    def _key(snapshot) -> tuple:
        mr = snapshot.mr
        return ((mr.host if mr else ""), (mr.project if mr else ""))

    def _forge(self, snapshot):
        return self._forges.for_session(snapshot) if self._forges else None

    def _supported(self, snapshot) -> bool:
        capabilities = (snapshot.mr.capabilities or {}) if snapshot.mr else {}
        forge = self._forge(snapshot)
        return bool(snapshot.mr) and bool(capabilities.get("members")) \
            and forge is not None and hasattr(forge, "members")

    async def _republish(self, session_id: str) -> None:
        if self._publish is not None:
            await self._publish(f"people:{session_id}")

    def _snapshot(self, session_id: str):
        writer = self._manager.get(session_id)
        if writer is None:
            return None
        snapshot = writer.snapshot()
        return snapshot if snapshot.status is SessionStatus.ACTIVE else None
