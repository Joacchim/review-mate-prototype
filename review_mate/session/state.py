"""Session state — the canonical contract every review-mate unit reads and writes.

The six document types named by the review-mate design (MR metadata, files, highlights, cards,
access-requests, review threads) plus a small envelope. Pure data: no IO, no host/MCP/UI logic.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, computed_field


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
    SYSTEM = "system"  # the host/workspace contracts (loader)


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
    ref_mark: str = "!"                # how this forge writes a change's number: `!12`, `#12`
    clone_url: str = ""                # repo clone URL — lets the server materialize a checkout (diff-versions)
    # host-neutral capability advertisement (design D6) — what the active provider supports
    capabilities: dict[str, bool] = Field(default_factory=dict)
    # diff version anchors (base/head/start sha) for precise write-back positions
    diff_refs: dict[str, str] = Field(default_factory=dict)

    @computed_field
    @property
    def label(self) -> str:
        """How a reviewer refers to this change, in one line.

        Derived here rather than in each client, and not because it is styling — it is what the
        change is *called*, and four clients each deciding that is four chances to disagree. A
        branch that never left this machine has no merge-request number, so naming it by one would
        put back the fiction `LocalRef` exists to keep out of the model: it is named by where it is
        going, which is the only thing that identifies it.

        The mark before the number is the forge's, not ours. GitLab's users write `!12` and
        GitHub's write `#12`, and telling either that their change is the other is wrong in the one
        place a reviewer looks to check they opened the right thing. The forge supplies it, because
        a hostname does not: an Enterprise install is not called github.com.
        """
        if self.host == "local":
            return f"{self.source_branch} → {self.target_branch}" if self.target_branch \
                else self.source_branch
        return f"{self.project} {self.ref_mark}{self.iid}"


class FileEntry(BaseModel):
    path: str
    old_path: str | None = None
    change_type: ChangeType
    language: str | None = None
    hunks: list[dict] = Field(default_factory=list)


class Highlight(BaseModel):
    id: str
    ordinal: int = 0                   # the "#N" a reviewer refers to it by, fixed at creation
    file: str
    side: Side
    line_range: LineRange
    anchor: str | None = None          # selected text / blob anchor (not raw editor offset)
    question: str | None = None
    author: Origin = Origin.BROWSER
    created_at: str = ""
    created_sha: str | None = None     # MR head SHA when made — flags "older version" after a push
    status: HighlightStatus = HighlightStatus.OPEN
    context_requested: bool = False    # the reviewer escalated this to the agent (D21)
    context_requested_at: str = ""     # when they escalated — the UI ages the "Claude is working" cue


class Theme(str, Enum):
    """What an insight is about. Closed, because an open list is a filter row nobody can rely on:
    `perf` and `performance` would both appear and neither would find the other's cards."""
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    TEST = "test"
    DOCS = "docs"
    STYLE = "style"
    NAMING = "naming"
    COMPLEXITY = "complexity"


class Criticality(str, Enum):
    """How much an insight matters. Three levels, not four: the gap between "nit" and "low" is the
    difference between *do not act* and *maybe act*, and that is a reviewer's call to make. An
    agent's findings are advisory — it blocks nothing — so a level encoding "you may ignore this"
    would be a signal it is not entitled to send. A nit is `style` at `low`, and composes."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Label(BaseModel):
    """What an insight is about, how much it matters, and who says so.

    `about` is the free line the enums cannot carry — "the retry path", "only on the cold start".
    The pair is what filters and sorts; the line is what makes a row worth reading before opening
    it. `by` records whose claim this is: the agent's when it emitted the card, the reviewer's once
    they disagreed, which is a different thing to show than a label nobody has questioned."""
    theme: Theme
    criticality: Criticality
    about: str = ""
    by: Origin = Origin.AGENT


class Card(BaseModel):
    id: str
    highlight_id: str | None = None    # the pivot anchor; None = an MR-level (unanchored) insight
    body: str                          # markdown
    citations: list[str] = Field(default_factory=list)
    author: Origin = Origin.AGENT
    status: CardStatus = CardStatus.COMPLETE
    label: "Label | None" = None       # unlabelled is legal: never inferred, rendered as unlabelled
    created_at: str = ""


class Addressed(BaseModel):
    """The agent changed the code in answer to something the reviewer said.

    The other kind of answer. A card explains, a message replies, and neither is what a reviewer
    means when they say "this retry is unbounded" — they mean fix it. Recording the change against
    the subject it answers is what lets an annotation say *addressed at abc123* instead of leaving five
    open comments and one new commit for the reviewer to match up themselves.

    It is also what keeps a moving head readable. Elsewhere `created_sha != head` means "these lines
    may have moved, read warily"; here the head moved *because* the agent fixed it. Same fact,
    opposite meaning, and the record is what tells them apart.
    """
    subject: "Subject"
    sha: str                           # what the code became
    summary: str = ""                  # one line: what was changed, in the agent's words
    at: str = ""


class Grant(BaseModel):
    """What an approval actually produced: a checkout on disk, or why there is none yet.

    A second axis from the decision, not more values on it. A request is pending, approved or
    denied — that is the reviewer's answer and it is final. Whether the repository has been
    materialized is a server-side outcome of an approval that takes seconds and can fail, and
    folding it into `status` would make "approved" mean two different things depending on when it
    was read. Absent until something starts the work, so "approved and nothing is materializing it"
    stays distinguishable from "a clone is running".
    """
    state: str = "materializing"       # materializing | ready | failed
    path: str | None = None            # where it was checked out, once ready
    error: str = ""
    at: str = ""


class AccessRequest(BaseModel):
    id: str
    repo: str
    reason: str
    status: AccessStatus = AccessStatus.PENDING
    decided_at: str | None = None
    grant: Grant | None = None         # only ever set on an approved request


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


class CheckRequest(BaseModel):
    """Something the reviewer asked the agent to verify — their own words, or the agent's.

    A comment is not a subject of its own, so double-checking one checks the highlight it sits on
    and carries the comment's text as what to verify. The sha says which code it was about, the way
    a highlight and a review pass both do.
    """
    id: str
    subject: "Subject"
    note: str = ""
    requested_at: str = ""
    sha: str | None = None


class SubjectKind(str, Enum):
    """What a chat can be about, beyond the review itself."""
    HIGHLIGHT = "highlight"
    INSIGHT = "insight"
    THREAD = "thread"


class Subject(BaseModel):
    """A chat's subject: an annotation row, addressed by kind and id.

    The kinds are exactly what a client can open a detail panel on, so a chat lives where
    its subject already renders. An id is unique on its own, but the kind travels with it: a client
    resolves the row without guessing which list to look in.
    """
    kind: SubjectKind
    id: str


class ChatMessage(BaseModel):
    id: str
    role: str                          # "user" (browser) or "agent"
    body: str
    anchor: Subject | None = None      # what it is about; None = the review as a whole
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
    # Highlight numbering is a reference a reviewer uses in conversation, so it must not move when
    # one is removed. This counts every highlight ever added, and never goes down: removing #2
    # leaves #1 and #3, the way issue numbers behave.
    highlights_created: int = 0
    cards: list[Card] = Field(default_factory=list)
    access_requests: list[AccessRequest] = Field(default_factory=list)
    threads: list[ReviewThread] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    # the MR-level counterpart of Highlight.context_requested: the reviewer asked for insights on
    # the change as a whole, so an answer is expected without any line range having been marked.
    # The sha says which code was asked about — a pass the change has moved past reads as being
    # about an earlier version, the way a highlight does, rather than quietly disappearing
    insights_requested: bool = False
    insights_requested_at: str = ""
    insights_requested_sha: str | None = None
    checks: list[CheckRequest] = Field(default_factory=list)
    addressed: list[Addressed] = Field(default_factory=list)
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
    url: str | None = None
    highlights: int = 0
    cards: int = 0
    drafts_pending: int = 0
    drafts_posted: int = 0
