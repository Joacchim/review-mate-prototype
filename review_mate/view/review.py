"""The `review` scope: what the reviewer has prepared, and what it would take to send it.

One scope per session, carrying the drafts a reviewer is writing, whether the change has moved past
the version they reviewed, and whether they have approved it. Three facts that arrive together in a
single bar and were three separate reads before this — so a client painted it in three stages, and a
second client would have had to reproduce the same assembly.

Drafts live here rather than on the rail because the rail answers "what did I ask about" and this
answers "what am I about to send". A row appears in both, and each carries what its own surface
renders: the rail shows a comment's *state* to colour a chip, this carries its text and its fate.

Approval is a host fact, so it follows the shape the hub established rather than being read on
every build: `refresh` asks once and caches, `build` reports what it knows. An unasked approval is
`checked: false`, which is not the same as an MR that cannot be approved (`available: false`).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from review_mate.contracts import MRRef
from review_mate.session.state import DraftStatus, SessionStatus


class DraftRow(BaseModel):
    """One prepared comment. `highlight_id` is its anchor; None is the MR-level summary."""
    id: str
    highlight_id: str | None = None
    body: str = ""
    suggestion: str | None = None
    status: str = "draft"              # draft | posted
    url: str = ""                      # where it landed, once posted
    thread_id: str = ""                # the discussion it became, once posted
    created_at: str = ""


class ApprovalView(BaseModel):
    """Whether this reviewer has approved, and who else has.

    `available` is the host's capability; `checked` says whether anyone has asked yet. A client
    renders an unchecked approval as unknown rather than as "nobody has approved".
    """
    available: bool = False
    checked: bool = False
    you_approved: bool = False
    approved_by: list[str] = Field(default_factory=list)


class VersionView(BaseModel):
    """Where the reviewer's attention stopped, against where the change now is."""
    head: str = ""
    watermark: str | None = None
    behind: bool = False


class ReviewView(BaseModel):
    session: str
    state: str = "ready"               # ready | unknown-session
    drafts: list[DraftRow] = Field(default_factory=list)
    pending: int = 0
    posted: int = 0
    approval: ApprovalView = Field(default_factory=ApprovalView)
    version: VersionView = Field(default_factory=VersionView)


class ReviewScope:
    """Builds the review bar, and owns the approval cache.

    `build` never calls the host. The approval is fetched by `refresh`, which the command handler
    runs when a review opens and again after a submit, since submitting is what changes it.
    """

    def __init__(self, manager, provider=None, kb=None) -> None:
        self._manager = manager
        self._provider = provider
        self._kb = kb
        self._approval: dict[str, dict] = {}       # session id -> what the host last said

    async def build(self, session_id: str) -> dict:
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return ReviewView(session=session_id, state="unknown-session").model_dump(mode="json")
        drafts = [self._row(d) for d in snapshot.drafts]
        return ReviewView(
            session=session_id,
            drafts=drafts,
            pending=sum(1 for d in drafts if d.status != "posted"),
            posted=sum(1 for d in drafts if d.status == "posted"),
            approval=self._approval_view(session_id, snapshot),
            version=self._version(snapshot),
        ).model_dump(mode="json")

    async def refresh(self, session_id: str) -> None:
        """Ask the host who has approved, and remember the answer.

        A failure propagates — the caller decides whether a reviewer needs telling — and leaves the
        cache as it was, so a bar already showing an answer keeps showing it rather than blinking
        to unknown because one refresh did not land.
        """
        snapshot = self._snapshot(session_id)
        if snapshot is None or snapshot.mr is None or not self._can_approve(snapshot):
            return
        if self._provider is None or not hasattr(self._provider, "approvals"):
            return
        ref = MRRef(host=snapshot.mr.host, project=snapshot.mr.project, iid=snapshot.mr.iid)
        answer = await self._provider.approvals(ref)
        if answer is not None:
            self._approval[session_id] = dict(answer)

    # --- the parts -----------------------------------------------------------

    @staticmethod
    def _row(draft) -> DraftRow:
        return DraftRow(
            id=draft.id, highlight_id=draft.highlight_id, body=draft.body,
            suggestion=draft.suggestion,
            status="posted" if draft.status is DraftStatus.POSTED else "draft",
            url=draft.url or "", thread_id=draft.thread_id or "",
            created_at=draft.created_at,
        )

    @staticmethod
    def _can_approve(snapshot) -> bool:
        return bool(snapshot.mr and (snapshot.mr.capabilities or {}).get("approvals", False))

    def _approval_view(self, session_id: str, snapshot) -> ApprovalView:
        if not self._can_approve(snapshot):
            return ApprovalView(available=False, checked=True)
        known = self._approval.get(session_id)
        if known is None:
            return ApprovalView(available=True, checked=False)
        return ApprovalView(available=True, checked=True,
                            you_approved=bool(known.get("you_approved")),
                            approved_by=list(known.get("approved_by") or []))

    def _version(self, snapshot) -> VersionView:
        if snapshot.mr is None:
            return VersionView()
        mark = None
        if self._kb is not None:
            mark = self._kb.get_watermark(snapshot.mr.host, snapshot.mr.project, snapshot.mr.iid)
        return VersionView(head=snapshot.mr.sha, watermark=mark,
                           behind=bool(mark and mark != snapshot.mr.sha))

    def _snapshot(self, session_id: str):
        writer = self._manager.get(session_id)
        if writer is None:
            return None
        snapshot = writer.snapshot()
        return snapshot if snapshot.status is SessionStatus.ACTIVE else None
