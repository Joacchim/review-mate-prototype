"""What the reviewer is still waiting on the agent for, and what that means about the agent.

One owner for a predicate that had two: the browser derived it to draw its indicator, and
`/api/outstanding` derived it again so an agent whose notification was dropped by a restart could
re-find its work. Two copies of a rule that is about to get harder — a chat is outstanding when
the reviewer spoke last, which is per chat now rather than per review.

Nothing here reads the host or the clock beyond `now`, so it is cheap enough to run on every build.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from review_mate.session.state import Subject, SubjectKind

# How long an ask may sit with an agent attached before "working on it" stops being the likely
# reading. Well past normal card latency (the coordinator's long-poll ceiling is 50s), well under
# the many minutes it takes a reviewer to get suspicious on their own.
STALE_AFTER = 300.0


class Ask(BaseModel):
    """One thing the agent owes an answer on."""
    kind: str                          # chat | context | insights | check
    subject: Subject | None = None     # where the answer belongs; None = the review as a whole
    since: str = ""                    # when it was asked — the oldest ask ages the indicator
    # What to verify, for a check. A comment is not a subject the protocol knows, so a doubt about
    # one is recorded against the subject it sits on and carries the words themselves. Without
    # them the agent is told that something is owed and never what — which is what it was being
    # told, because this was recorded and then not published.
    note: str = ""


class AgentState(BaseModel):
    """What a client renders as a word, rather than deriving from presence and asks itself."""
    state: str = "off"                 # working | stalled | watching | off
    stale: bool = False                # attached, but this ask has sat too long for that to explain
    since: str | None = None           # when the oldest outstanding ask was made
    attached: bool = False
    parked: bool = False
    last_seen: str | None = None
    asks: list[Ask] = Field(default_factory=list)


def _key(anchor: Subject | None) -> tuple[str, str]:
    return ("review", "") if anchor is None else (anchor.kind.value, anchor.id)


def outstanding(snapshot) -> list[Ask]:
    """Every ask this review is waiting on, oldest first.

    Four shapes, and they are not interchangeable: a chat where the reviewer spoke last, a
    highlight escalated past the host context with no card yet, a request for insights on the change
    as a whole that nothing has answered, and something the reviewer asked to have verified.

    An agent's own question back to the reviewer is not here. Nothing distinguishes a question from
    a statement in a message body, and inventing the distinction would report the reviewer's silence
    as the agent's debt.
    """
    asks: list[Ask] = []
    last_of: dict[tuple[str, str], object] = {}
    for message in snapshot.messages:
        last_of[_key(message.anchor)] = message
    for (kind, ident), message in last_of.items():
        if message.role != "user":
            continue
        anchor = None if kind == "review" else Subject(kind=SubjectKind(kind), id=ident)
        asks.append(Ask(kind="chat", subject=anchor, since=message.created_at))

    carded = {c.highlight_id for c in snapshot.cards if c.highlight_id}
    for highlight in snapshot.highlights:
        if highlight.context_requested and highlight.id not in carded:
            asks.append(Ask(kind="context",
                            subject=Subject(kind=SubjectKind.HIGHLIGHT, id=highlight.id),
                            since=highlight.context_requested_at or highlight.created_at))

    if snapshot.insights_requested and not any(c.highlight_id is None for c in snapshot.cards):
        asks.append(Ask(kind="insights", since=snapshot.insights_requested_at))

    for check in snapshot.checks:
        if not _answered(snapshot, check):
            asks.append(Ask(kind="check", subject=check.subject, since=check.requested_at,
                            note=check.note))

    return sorted(asks, key=lambda a: a.since or "")


def _answered(snapshot, check) -> bool:
    """Whether the agent has said anything about a check's subject since it was asked.

    Deliberately that loose. Adding a verb for the agent to close a check with would be a second
    way of saying what a message already says, and an agent that answered without remembering to
    call it would leave the reviewer waiting on work that was done. The cost is that *any* later
    word on that subject closes it — see docs/surprising-behaviors.md.
    """
    for message in snapshot.messages:
        if message.role == "user" or message.anchor != check.subject:
            continue
        if (message.created_at or "") > (check.requested_at or ""):
            return True
    return False


def agent_state(asks: list[Ask], watcher: dict | None, *, now: datetime | None = None) -> AgentState:
    """Join presence with what is outstanding — the fact neither half carries alone.

    Presence answers "is anyone listening"; a watcher parked in `wait()` is idle by definition. What
    a reviewer needs to know is whether their ask is being worked on, which is this join.
    """
    watcher = watcher or {}
    attached = bool(watcher.get("attached"))
    common = dict(attached=attached, parked=bool(watcher.get("parked")),
                  last_seen=watcher.get("last_seen"), asks=list(asks))
    if not asks:
        return AgentState(state="watching" if attached else "off", **common)
    since = asks[0].since or None
    return AgentState(state="working" if attached else "stalled", since=since,
                      stale=attached and _age(since, now) > STALE_AFTER, **common)


def _age(since: str | None, now: datetime | None) -> float:
    if not since:
        return 0.0
    try:
        asked = datetime.fromisoformat(since)
    except ValueError:
        return 0.0
    if asked.tzinfo is None:
        asked = asked.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - asked).total_seconds()
