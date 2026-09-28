"""The `chat` scope family: the chats a review is holding with the agent.

`chat:<sid>` is an index — one summary row per chat that has anything in it, plus the
review's own, and the agent state this session is in. `chat:<sid>:review` and
`chat:<sid>:<kind>:<id>` carry one chat's messages.

Split for the same reason the diff is: a client subscribes to the chat it has open, so a
message in one does not republish the others, and the index it always holds stays small enough to
arrive on every turn. Measured against carrying every chat in one view, or carrying them on
the rail, on a review-sized load: 2.9 KB here for a message in the open subject and 1.0 KB for one
elsewhere, against 8.6 KB and 14.7 KB.

The agent state rides the index rather than a scope of its own because it is what the chat surface
renders, and because presence alone does not answer the reviewer's question — see `view.asks`.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from review_mate.session.state import SessionStatus, Subject, SubjectKind
from review_mate.view.asks import AgentState, agent_state, outstanding

REVIEW = "review"      # the chat about the change as a whole, anchored to nothing


class ChatMessageView(BaseModel):
    id: str
    role: str
    body: str
    created_at: str = ""


class ChatRow(BaseModel):
    """A chat, as the index lists it."""
    kind: str                      # review | highlight | insight | thread
    id: str = ""                   # the subject's id; empty for the review's own
    scope: str                     # the name to subscribe to for its messages
    count: int = 0
    last_role: str = ""
    last_at: str = ""
    preview: str = ""
    owed: bool = False             # the reviewer spoke last, so an answer is expected
    checking: bool = False         # a doubt about this subject the agent has not spoken to yet


class ChatIndexView(BaseModel):
    session: str
    state: str = "ready"           # ready | unknown-session
    agent: AgentState = Field(default_factory=AgentState)
    chats: list[ChatRow] = Field(default_factory=list)


class ChatView(BaseModel):
    session: str
    state: str = "ready"           # ready | unknown-session | malformed-name
    kind: str = REVIEW
    id: str = ""
    owed: bool = False
    checking: bool = False
    messages: list[ChatMessageView] = Field(default_factory=list)


def _being_checked(asks) -> set[tuple[str, str]]:
    """Which subjects have a doubt the agent has not spoken to, addressed as the index addresses them.

    Read off the same outstanding list the agent works from, so what a reviewer sees waiting and
    what the agent is told it owes cannot drift apart.
    """
    return {_address(ask.subject) for ask in asks if ask.kind == "check"}


def _first_line(body: str, width: int = 80) -> str:
    line = next((ln for ln in (body or "").splitlines() if ln.strip()), "")
    return line if len(line) <= width else line[: width - 1] + "…"


def _address(anchor: Subject | None) -> tuple[str, str]:
    return (REVIEW, "") if anchor is None else (anchor.kind.value, anchor.id)


def scope_name(session_id: str, anchor: Subject | None) -> str:
    kind, ident = _address(anchor)
    return f"chat:{session_id}:{kind}" if ident == "" else f"chat:{session_id}:{kind}:{ident}"


class ChatScopes:
    """Builds the index and each chat. Never reads the host; `watcher` is a local fact."""

    def __init__(self, manager, watcher=None) -> None:
        self._manager = manager
        self._watcher = watcher      # callable returning the activity stream's watcher dict

    async def build(self, argument: str) -> dict:
        session_id, _, rest = argument.partition(":")
        if not rest:
            return self._index(session_id)
        kind, _, ident = rest.partition(":")
        if kind == REVIEW and ident == "":
            return self._conversation(session_id, None)
        if ident and kind in {k.value for k in SubjectKind}:
            return self._conversation(session_id, Subject(kind=SubjectKind(kind), id=ident))
        return ChatView(session=session_id, state="malformed-name").model_dump(mode="json")

    # --- the index -----------------------------------------------------------

    def _index(self, session_id: str) -> dict:
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return ChatIndexView(session=session_id, state="unknown-session").model_dump(mode="json")
        asks = outstanding(snapshot)
        checking = _being_checked(asks)
        by_address: dict[tuple[str, str], list] = {(REVIEW, ""): []}
        for message in snapshot.messages:
            by_address.setdefault(_address(message.anchor), []).append(message)
        for address in checking:            # a doubt with nothing said yet still has to be visible
            by_address.setdefault(address, [])
        rows = []
        for (kind, ident), messages in by_address.items():
            last = messages[-1] if messages else None
            rows.append(ChatRow(
                kind=kind, id=ident,
                scope=(f"chat:{session_id}:{kind}" if ident == ""
                       else f"chat:{session_id}:{kind}:{ident}"),
                count=len(messages),
                last_role=last.role if last else "",
                last_at=last.created_at if last else "",
                preview=_first_line(last.body) if last else "",
                owed=bool(last and last.role == "user"),
                checking=(kind, ident) in checking,
            ))
        # the review's own first, then by most recent — a client renders the order it is given
        rows.sort(key=lambda r: (r.kind != REVIEW, _descending(r.last_at)))
        return ChatIndexView(session=session_id, agent=agent_state(asks, self._watcher_now()),
                             chats=rows).model_dump(mode="json")

    # --- one chat ----------------------------------------------------

    def _conversation(self, session_id: str, anchor: Subject | None) -> dict:
        snapshot = self._snapshot(session_id)
        kind, ident = _address(anchor)
        if snapshot is None:
            return ChatView(session=session_id, state="unknown-session", kind=kind,
                                    id=ident).model_dump(mode="json")
        messages = [m for m in snapshot.messages if _address(m.anchor) == (kind, ident)]
        return ChatView(
            session=session_id, kind=kind, id=ident,
            owed=bool(messages and messages[-1].role == "user"),
            checking=(kind, ident) in _being_checked(outstanding(snapshot)),
            messages=[ChatMessageView(id=m.id, role=m.role, body=m.body, created_at=m.created_at)
                      for m in messages],
        ).model_dump(mode="json")

    # --- plumbing ------------------------------------------------------------

    def _watcher_now(self) -> dict | None:
        return self._watcher() if self._watcher is not None else None

    def _snapshot(self, session_id: str):
        actor = self._manager.get(session_id)
        if actor is None:
            return None
        snapshot = actor.snapshot()
        return snapshot if snapshot.status is SessionStatus.ACTIVE else None


def _descending(value: str) -> tuple:
    """Sort key that puts the most recent first, with never-used chats last."""
    return (value == "", tuple(-ord(c) for c in value))
