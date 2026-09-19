"""Named states a test can stage. Extend this rather than building state inline."""
from __future__ import annotations

from review_mate.session.state import (
    ChangeType, DraftComment, DraftStatus, FileEntry, MRMetadata, SessionState, SessionStatus,
)

DIFF_A = """@@ -44,3 +44,4 @@ class Scheduler:
     def reserve(self, pu):
-        if pu.legacy:
+        if pu.fleet == LEGACY:
+            q = self._legacy
         return q.take(pu.size)
"""
DIFF_B = "@@ -12,1 +12,1 @@\n-TIMEOUT = 30\n+TIMEOUT = 120\n"

QUEUE = [
    {"host": "gitlab", "project": "platform/virtu/control-plane", "iid": 137,
     "title": "reserve Scheduler capacity", "url": "https://gitlab.example/mr/137"},
    {"host": "gitlab", "project": "platform/orchestration/dr-house", "iid": 86,
     "title": "allow transfer with no user", "url": "https://gitlab.example/mr/86"},
]


def mr(project="platform/virtu/control-plane", iid=137, title="reserve Scheduler capacity",
       sha="abc123") -> MRMetadata:
    return MRMetadata(host="gitlab", project=project, iid=iid, title=title,
                      source_branch="feat/x", target_branch="main", sha=sha, author="luigi",
                      url=f"https://gitlab.example/mr/{iid}")


def session(session_id="s1", files=None, drafts=None, **kwargs) -> SessionState:
    return SessionState(id=session_id, status=SessionStatus.ACTIVE,
                        created_at="2026-01-01T00:00:00+00:00", seq=1,
                        mr=kwargs.pop("mr", mr()), files=files or [], drafts=drafts or [],
                        **kwargs)


def two_file_review(session_id="s1") -> SessionState:
    return session(session_id, files=[
        FileEntry(path="scheduler/capacity.py", change_type=ChangeType.MODIFIED,
                  language="python", hunks=[{"diff": DIFF_A}]),
        FileEntry(path="scheduler/config.py", change_type=ChangeType.MODIFIED,
                  language="python", hunks=[{"diff": DIFF_B}]),
    ])


def review_with_drafts(session_id="s1") -> SessionState:
    return session(session_id, drafts=[
        DraftComment(id="d1", highlight_id=None, body="an unsubmitted thought", status=DraftStatus.DRAFT),
    ])


# a file whose body is long enough for the unfold bands to appear between hunks
CAPACITY_BODY = "\n".join(
    [f"# line {n}" for n in range(1, 44)]
    + ["    def reserve(self, pu):", "        if pu.fleet == LEGACY:",
       "            q = self._legacy", "        return q.take(pu.size)"]
    + [f"# tail {n}" for n in range(1, 10)]
)

MARKDOWN_DIFF = "@@ -1,1 +1,2 @@\n # Title\n+some *emphasis* here\n"


def markdown_review(session_id="s1") -> SessionState:
    return session(session_id, files=[
        FileEntry(path="README.md", change_type=ChangeType.MODIFIED, language="markdown",
                  hunks=[{"diff": MARKDOWN_DIFF}]),
    ])


SINCE_DIFF = """diff --git a/scheduler/capacity.py b/scheduler/capacity.py
--- a/scheduler/capacity.py
+++ b/scheduler/capacity.py
@@ -45,1 +45,2 @@
         if pu.fleet == LEGACY:
+            # added since you last looked
"""

COMMIT_DIFF = "@@ -1,1 +1,2 @@\n first\n+second\n"

COMMITS = [
    {"sha": "aaaa111", "short_id": "aaaa111", "title": "first commit", "message": ""},
    {"sha": "bbbb222", "short_id": "bbbb222", "title": "second commit", "message": ""},
]


def reviewed_then_advanced(session_id="s1") -> SessionState:
    """A review whose watermark is an earlier version, so "since last review" has something to say."""
    state = two_file_review(session_id)
    state.mr.capabilities = {"diff_versions": True, "commits": True}
    return state


class StubWorkspace:
    """since_diff, as the workspace would answer it."""

    def __init__(self, diff=SINCE_DIFF, clean=True):
        self.calls = []
        self.diff = diff
        self.clean = clean

    async def since_diff(self, repo, old_base, old_head, new_base, new_head):
        self.calls.append((old_base, old_head, new_base, new_head))
        return {"diff": self.diff, "clean": self.clean}
