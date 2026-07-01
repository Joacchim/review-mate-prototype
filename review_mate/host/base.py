"""Host-neutral provider contract + the MR reference parser.

`HostProvider` is the read-side interface the rest of review-mate sees; a host (GitLab first)
implements it. The capability set (D6) lets the UI light up only what a host supports.
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from review_mate.seams import MRPayload, MRRef

# The superset of review features review-mate models (D6). A provider advertises its subset.
GITLAB_CAPABILITIES: dict[str, bool] = {
    "inline_comments": True,
    "multiline_comments": True,
    "file_comments": True,
    "mr_comments": True,
    "threads": True,
    "suggestions": True,
    "approvals": True,
    "draft_reviews": True,
    "diff_versions": True,
    "reactions": True,
    "labels": True,
    "reviewers": True,
}

_MR_URL = re.compile(r"/-/merge_requests/(\d+)")


def parse_reference(s: str, default_host: str) -> MRRef | None:
    """Parse a full MR URL or a `group/proj!iid` shorthand into an MRRef; None if unparseable."""
    s = (s or "").strip()
    if not s:
        return None
    if "://" in s or s.startswith("http"):
        u = urlparse(s if "://" in s else "https://" + s)
        m = _MR_URL.search(u.path)
        if not m:
            return None
        project = u.path.split("/-/merge_requests")[0].strip("/")
        if not project:
            return None
        return MRRef(host=u.netloc, project=project, iid=int(m.group(1)))
    if "!" in s:
        left, _, right = s.partition("!")
        if "/" in left and right.isdigit():
            return MRRef(host=default_host, project=left.strip("/"), iid=int(right))
    return None


@runtime_checkable
class HostProvider(Protocol):
    def capabilities(self) -> dict[str, bool]: ...
    async def load(self, ref: MRRef) -> MRPayload: ...
    async def review_queue(self) -> list[MRRef]: ...
    async def issue_related_mrs(self, project: str, issue_iid: int) -> list[MRRef]: ...


class CapabilityError(Exception):
    """Raised when a write operation is attempted that the host does not advertise."""


@runtime_checkable
class HostWriter(Protocol):
    """Write side of the review model (D6) — each op gated on the host's capabilities."""
    def capabilities(self) -> dict[str, bool]: ...
    async def post_comment(self, ref: MRRef, position: dict, body: str) -> dict: ...
    async def post_mr_comment(self, ref: MRRef, body: str) -> dict: ...
    async def reply(self, ref: MRRef, discussion_id: str, body: str) -> dict: ...
    async def resolve(self, ref: MRRef, discussion_id: str, resolved: bool = True) -> dict: ...
    async def approve(self, ref: MRRef) -> dict: ...
    async def suggest(self, ref: MRRef, position: dict, suggestion: str) -> dict: ...
