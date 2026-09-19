"""Named states a test can stage. Extend this rather than building state inline."""
from __future__ import annotations

from review_mate.session.state import (
    ChangeType, DraftComment, DraftStatus, FileEntry, MRMetadata, SessionState, SessionStatus,
)

DIFF_A = """@@ -44,4 +44,6 @@ class Scheduler:
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
