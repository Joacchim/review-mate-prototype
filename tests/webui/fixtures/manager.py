"""A session manager the tests can set directly.

The fixture server is the production `create_app`; only this sits underneath it. Every frame the
browser receives is therefore built by the real scope builders and validated by the real models,
so there is no second description of the protocol to drift from the first.

Scenarios are built from the real state models, never from dicts.
"""
from __future__ import annotations

from review_mate.session.state import SessionState, SessionStatus, SessionSummary

# What the application reaches for on a manager. Both this fake and the real SessionManager must
# carry all of it — a rename on either side has to break a test, not a page.
MANAGER_SURFACE = ("get", "list", "create", "end", "restore_all", "shutdown",
                   "_activity_broker", "_workspace")


class FakeActor:
    def __init__(self, state: SessionState) -> None:
        self._state = state

    def snapshot(self) -> SessionState:
        return self._state

    async def subscribe(self, since: int = 0):
        """The per-session event stream. A staged session never changes, so it yields nothing and
        holds the connection open — which is what the page expects while it is idle."""
        import asyncio
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable, present to make this an async generator


class FakeManager:
    """Holds sessions as state objects. `create` mints an empty one unless a test staged it."""

    def __init__(self) -> None:
        self._states: dict[str, SessionState] = {}
        self._activity_broker = None
        self._workspace = None
        self.created: list = []
        self.ended: list[str] = []
        self.next_id = "staged-session"

    # --- staging ----------------------------------------------------------

    def put(self, state: SessionState) -> str:
        self._states[state.id] = state
        return state.id

    def reset(self) -> None:
        self._states.clear()
        self.created.clear()
        self.ended.clear()

    # --- the surface the application uses ----------------------------------

    def get(self, session_id: str):
        state = self._states.get(session_id)
        return FakeActor(state) if state is not None else None

    def list(self) -> list[SessionSummary]:
        return [
            SessionSummary(
                id=state.id, status=state.status, created_at=state.created_at, seq=state.seq,
                title=state.mr.title if state.mr else None,
                project=state.mr.project if state.mr else None,
                iid=state.mr.iid if state.mr else None,
                url=state.mr.url if state.mr else None,
                highlights=len(state.highlights), cards=len(state.cards),
                drafts_pending=sum(1 for d in state.drafts if d.status.value == "draft"),
                drafts_posted=sum(1 for d in state.drafts if d.status.value == "posted"),
            )
            for state in self._states.values()
        ]

    async def create(self, ref=None) -> str:
        self.created.append(ref)
        state = SessionState(id=self.next_id, status=SessionStatus.ACTIVE,
                             created_at="2026-01-01T00:00:00+00:00", seq=0)
        self._states[state.id] = state
        return state.id

    async def end(self, session_id: str) -> None:
        if session_id not in self._states:
            raise KeyError(session_id)
        self.ended.append(session_id)
        self._states.pop(session_id)

    async def restore_all(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None
