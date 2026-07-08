"""Session state — the canonical contract every review-mate unit reads and writes.

The six document types named by the review-mate design (MR metadata, files, highlights, cards,
access-requests, review threads) plus a small envelope. Pure data: no IO, no host/MCP/UI logic.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ENDED = "ended"


class Side(str, Enum):
    OLD = "old"
    NEW = "new"


class Origin(str, Enum):
    """Who is acting — the axis the write-authority partitioning turns on."""
    BROWSER = "browser"
    AGENT = "agent"
    SYSTEM = "system"  # the host/workspace seams (loader)


class ChangeType(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class CardStatus(str, Enum):
    STREAMING = "streaming"
    COMPLETE = "complete"


class AccessStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class HighlightStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class LineRange(BaseModel):
    start: int
    end: int


class MRMetadata(BaseModel):
    host: str
    project: str
    iid: int
    title: str
    source_branch: str
    target_branch: str
    sha: str
    author: str
    url: str
    clone_url: str = ""                # repo clone URL — lets the server materialize a checkout (diff-versions)
    # host-neutral capability advertisement (design D6) — what the active provider supports
    capabilities: dict[str, bool] = Field(default_factory=dict)
    # diff version anchors (base/head/start sha) for precise write-back positions
    diff_refs: dict[str, str] = Field(default_factory=dict)


class FileEntry(BaseModel):
    path: str
    old_path: str | None = None
    change_type: ChangeType
    language: str | None = None
    hunks: list[dict] = Field(default_factory=list)


class Highlight(BaseModel):
    id: str
    file: str
    side: Side
    line_range: LineRange
    anchor: str | None = None          # selected text / blob anchor (not raw editor offset)
    question: str | None = None
    author: Origin = Origin.BROWSER
    created_at: str = ""
    created_sha: str | None = None     # MR head SHA when made — flags "older version" after a push
    status: HighlightStatus = HighlightStatus.OPEN
    context_requested: bool = False    # the reviewer escalated this to the agent tier (D21)


class Card(BaseModel):
    id: str
    highlight_id: str | None = None    # the pivot anchor; None = an MR-level (unanchored) insight
    body: str                          # markdown
    citations: list[str] = Field(default_factory=list)
    author: Origin = Origin.AGENT
    status: CardStatus = CardStatus.COMPLETE
    created_at: str = ""


class AccessRequest(BaseModel):
    id: str
    repo: str
    reason: str
    status: AccessStatus = AccessStatus.PENDING
    decided_at: str | None = None


class ThreadComment(BaseModel):
    id: str
    author: str
    body: str
    created_at: str = ""


class ReviewThread(BaseModel):
    id: str
    anchor: dict | None = None         # {file, side, line} or None for an MR-level thread
    comments: list[ThreadComment] = Field(default_factory=list)
    resolved: bool = False
    capabilities: dict[str, bool] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    id: str
    role: str                          # "user" (browser) or "agent"
    body: str
    created_at: str = ""


class DraftStatus(str, Enum):
    DRAFT = "draft"
    POSTED = "posted"


class DraftComment(BaseModel):
    """A reviewer-authored review comment, prepared locally and posted on submit (D14).

    The body is the reviewer's own text — never the agent's card. Anchored to a highlight, whose
    file/line + the MR's diff_refs give the host the exact position at submit time. A `None` anchor
    is an MR-level comment (a review summary), posted as a general note — mirrors `Card.highlight_id`.
    """
    id: str
    highlight_id: str | None = None    # the anchor (one draft per highlight); None = MR-level
    body: str
    suggestion: str | None = None      # optional suggested-change replacement lines (coexists with body)
    status: DraftStatus = DraftStatus.DRAFT
    url: str | None = None             # the posted comment's URL, set on submit
    thread_id: str | None = None       # the discussion this draft became, set on submit (draft-as-thread)
    created_at: str = ""


class SessionState(BaseModel):
    id: str
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: str = ""
    seq: int = 0                       # last applied event sequence
    mr: MRMetadata | None = None
    checkout_path: str | None = None   # on-disk worktree of the MR (for code-graph / LSP / grep)
    files: list[FileEntry] = Field(default_factory=list)
    highlights: list[Highlight] = Field(default_factory=list)
    cards: list[Card] = Field(default_factory=list)
    access_requests: list[AccessRequest] = Field(default_factory=list)
    threads: list[ReviewThread] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    drafts: list[DraftComment] = Field(default_factory=list)


class SessionSummary(BaseModel):
    """A light listing entry (no document bodies) — enough for the queue page's session list."""
    id: str
    status: SessionStatus
    created_at: str
    seq: int
    title: str | None = None
    project: str | None = None
    iid: int | None = None
    highlights: int = 0
    cards: int = 0
    drafts_pending: int = 0
    drafts_posted: int = 0
