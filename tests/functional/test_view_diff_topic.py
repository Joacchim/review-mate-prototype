"""The reading topics over the real transport: a file list, one file's hunks, and their blast radius.

The property that matters is isolation — re-reading one file must not move the others — because it
is the whole reason the diff is split into topics at all.
"""
import asyncio
import json
from pathlib import Path
from time import monotonic

from starlette.testclient import TestClient

from conftest import HostStub, next_frame
from review_mate.contracts import MRPayload, MRRef, RepoUnreadable
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.session.state import ChangeType, FileEntry, MRMetadata
from review_mate.view.difftopic import _FAILED_TTL

DIFF_A = """@@ -1,3 +1,4 @@ def reserve(self, pu):
 def reserve(self, pu):
-    if pu.legacy:
+    if pu.fleet == LEGACY:
+        q = self._legacy
     return q
"""
DIFF_B = """@@ -10,2 +10,2 @@
-old = 1
+new = 2
"""

# Same shape as their originals — one removal, the same additions — so a re-read changes the file's
# own topic and leaves the listing, which carries counts and not bodies, identical.
DIFF_A_EDITED = """@@ -1,3 +1,4 @@ def reserve(self, pu):
 def reserve(self, pu):
-    if pu.legacy:
+    if pu.fleet is LEGACY:
+        q = self._fleet
     return q
"""
DIFF_B_EDITED = """@@ -10,2 +10,2 @@
-old = 1
+new = 3
"""


class TwoFileHost(HostStub):
    """A host whose files a test can edit between loads, as a push to the MR would."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.diffs = {"a.py": DIFF_A, "pkg/b.py": DIFF_B}
        # a real push moves the head as well as the content, and a test that leaves it fixed
        # cannot tell "the head moved" apart from "this file changed" — which is the whole
        # question the reviewed marks turn on
        self.sha = "abc"

    async def load(self, ref: MRRef) -> MRPayload:
        return MRPayload(
            mr=MRMetadata(host="gitlab", project=ref.project, iid=ref.iid, title="T",
                          source_branch="x", target_branch="main", sha=self.sha,
                          # a real change can be cloned, and everything in this file that reads the
                          # repository rather than the forge says nothing without it
                          clone_url=f"git@gitlab.com:{ref.project}.git",
                          author="dev", url="http://x"),
            files=[
                FileEntry(path="a.py", change_type=ChangeType.MODIFIED, language="python",
                          hunks=[{"diff": self.diffs["a.py"]}]),
                FileEntry(path="pkg/b.py", change_type=ChangeType.MODIFIED, language="python",
                          hunks=[{"diff": self.diffs["pkg/b.py"]}]),
            ],
            threads=[],
        )


def build(tmp_path, provider=None):
    provider = provider or TwoFileHost()
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider)
    return create_app(manager=manager, provider=provider, with_mcp=False,
                      resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))


def read_topic(ws, topic, limit=8):
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        if msg.get("topic") == topic:
            return msg
    raise AssertionError(f"no message for {topic}")


def open_session(tc):
    return tc.post("/api/cmd", json={"cmd": "session.open",
                                     "args": {"ref": "g/p!1"}}).json()["session"]


def test_the_diff_topic_lists_files_without_their_bodies(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:full"]})
            view = read_topic(ws, f"diff:{sid}:full")["view"]
            assert view["state"] == "ready" and view["mr"]["project"] == "g/p"
            assert [f["path"] for f in view["files"]] == ["a.py", "pkg/b.py"]
            assert view["files"][0]["additions"] == 2 and view["files"][0]["deletions"] == 1
            assert "hunks" not in json.dumps(view)     # bodies belong to the file topics


def test_a_file_topic_carries_numbered_lines_and_tokens(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:full:a.py"]})
            view = read_topic(ws, f"diff:{sid}:full:a.py")["view"]
            assert view["state"] == "ready" and view["language"] == "python"
            lines = view["hunks"][0]["lines"]
            assert [line["side"] for line in lines] == \
                ["context", "removed", "added", "added", "context"]
            assert lines[0]["new"] == 1 and lines[2]["new"] == 2
            assert any(kind == "keyword" for line in lines for _, _, kind in line["tokens"])


def test_rereading_one_file_leaves_the_others_untouched(tmp_path):
    """The point of splitting the diff: a per-file republish moves one topic's seq, not every one.

    A re-sync rebuilds every topic the session holds, so isolation is not about what is rebuilt —
    it is about what a client is told. Each step edits one file and reads exactly one frame: a
    republished listing or sibling would arrive as an extra frame and take one of these reads.
    """
    host = TwoFileHost()
    with TestClient(build(tmp_path, host)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [
                f"diff:{sid}:full", f"diff:{sid}:full:a.py", f"diff:{sid}:full:pkg/b.py"]})
            for topic in (f"diff:{sid}:full", f"diff:{sid}:full:a.py", f"diff:{sid}:full:pkg/b.py"):
                assert read_topic(ws, topic)["seq"] == 0

            host.diffs["a.py"] = DIFF_A_EDITED
            tc.post("/api/cmd", json={"cmd": "session.resync", "args": {"session": sid}})
            moved = json.loads(ws.receive_text())
            assert moved["topic"] == f"diff:{sid}:full:a.py" and moved["seq"] == 1

            host.diffs["pkg/b.py"] = DIFF_B_EDITED
            tc.post("/api/cmd", json={"cmd": "session.resync", "args": {"session": sid}})
            moved = json.loads(ws.receive_text())
            # its own counter: b.py moves to 1 while a.py stays where it was
            assert moved["topic"] == f"diff:{sid}:full:pkg/b.py" and moved["seq"] == 1


def test_a_path_the_change_does_not_touch_says_so(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:full:nope.py"]})
            view = read_topic(ws, f"diff:{sid}:full:nope.py")["view"]
            assert view["state"] == "unknown-file" and view["hunks"] == []


def test_a_mode_the_host_cannot_serve_says_unavailable(tmp_path):
    """A host with no version list cannot answer "since", and the view says so rather than
    reporting an empty change."""
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            view = read_topic(ws, f"diff:{sid}:since")["view"]
            assert view["state"] == "unavailable" and view["files"] == []


def test_an_unknown_session_is_reported_not_crashed(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": ["diff:ghost:full"]})
            assert read_topic(ws, "diff:ghost:full")["view"]["state"] == "unknown-session"


def test_a_path_may_contain_a_colon(tmp_path):
    """Only the session and mode are colon-free, so the split must not claim the path's own."""
    from review_mate.view.difftopic import parse_address
    address = parse_address("abc123:full:pkg/odd:name.py")
    assert address.path == "pkg/odd:name.py" and address.mode == "full"


def test_a_malformed_name_is_named_rather_than_mis_split(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            # "bogus" is not a mode, so this is not a session with a file called "a.py"
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:bogus:a.py"]})
            view = read_topic(ws, f"diff:{sid}:bogus:a.py")["view"]
            assert view["state"] == "malformed-name"


def test_a_file_topic_is_the_list_topic_plus_a_path(tmp_path):
    """The property that makes one family worth having: a client concatenates, never reassembles."""
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        listing = f"diff:{sid}:full"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [listing]})
            files = read_topic(ws, listing)["view"]["files"]
            names = [f"{listing}:{row['path']}" for row in files]      # concatenation, nothing else
            ws.send_json({"action": "subscribe", "topics": names})
            for name in names:
                assert read_topic(ws, name)["view"]["state"] == "ready"


# --- blob:<sid>:<mode>:<path> — the content a reviewer unfolds into ----------

FILE_AT_HEAD = "def reserve(self, pu):\n    if pu.fleet == LEGACY:\n        q = self._legacy\n    return q\n"


class BlobHost(TwoFileHost):
    def __init__(self, fail=None):
        super().__init__()
        self.reads = []
        self._fail = fail

    async def get_file(self, project, path, sha):
        self.reads.append((project, path, sha))
        if self._fail is not None:
            raise self._fail
        return FILE_AT_HEAD


def build_with(tmp_path, provider):
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider)
    return create_app(manager=manager, provider=provider, with_mcp=False,
                      resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))


def test_a_blob_loads_then_arrives_numbered_and_tokenized(tmp_path):
    provider = BlobHost()
    with TestClient(build_with(tmp_path, provider)) as tc:
        sid = open_session(tc)
        topic = f"blob:{sid}:full:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [topic]})
            first = read_topic(ws, topic)["view"]
            assert first["state"] in ("loading", "ready")      # a fast host may beat the first send
            ready = first if first["state"] == "ready" else read_topic(ws, topic)["view"]
            assert ready["state"] == "ready" and ready["sha"] == "abc"
            assert [line["n"] for line in ready["lines"][:3]] == [1, 2, 3]
            assert ready["lines"][0]["text"] == "def reserve(self, pu):"
            assert any(kind == "keyword" for _, _, kind in ready["lines"][0]["tokens"])


def test_blob_line_numbers_are_the_diffs_new_side(tmp_path):
    """So a client splices revealed lines into a gap without translating coordinates."""
    provider = BlobHost()
    with TestClient(build_with(tmp_path, provider)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:full:a.py",
                                                            f"blob:{sid}:full:a.py"]})
            hunk = read_topic(ws, f"diff:{sid}:full:a.py")["view"]["hunks"][0]
            view = read_topic(ws, f"blob:{sid}:full:a.py")["view"]
            if view["state"] != "ready":
                view = read_topic(ws, f"blob:{sid}:full:a.py")["view"]
            added = [line for line in hunk["lines"] if line["side"] == "added"][0]
            blob_line = next(line for line in view["lines"] if line["n"] == added["new"])
            assert blob_line["text"] == added["text"]


def test_one_host_read_serves_repeated_builds(tmp_path):
    """Content at a sha is fixed, so a rebuild must not go back to the host — and a client holding
    the blob must not be told about it again either.

    The edited file is what proves the rebuild ran at all: without a frame to wait for, "the host
    was read once" would also hold if nothing had been rebuilt.
    """
    provider = BlobHost()
    with TestClient(build_with(tmp_path, provider)) as tc:
        sid = open_session(tc)
        blob, open_file = f"blob:{sid}:full:a.py", f"diff:{sid}:full:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [blob, open_file]})
            seen: dict[str, dict] = {}
            for _ in range(6):
                msg = json.loads(ws.receive_text())
                seen[msg["topic"]] = msg["view"]
                if seen.get(blob, {}).get("state") == "ready" and open_file in seen:
                    break
            assert seen[blob]["state"] == "ready"

            provider.diffs["a.py"] = DIFF_A_EDITED
            tc.post("/api/cmd", json={"cmd": "session.resync", "args": {"session": sid}})
            moved = json.loads(ws.receive_text())
            assert moved["topic"] == open_file          # the blob rebuilt identically, so it stayed
            assert provider.reads.count(("g/p", "a.py", "abc")) == 1


def test_a_failed_blob_read_is_reported_in_the_view(tmp_path):
    provider = BlobHost(fail=RuntimeError("gitlab 404"))
    with TestClient(build_with(tmp_path, provider)) as tc:
        sid = open_session(tc)
        topic = f"blob:{sid}:full:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [topic]})
            view = read_topic(ws, topic)["view"]
            if view["state"] == "loading":
                view = read_topic(ws, topic)["view"]
            assert view["state"] == "error" and "gitlab 404" in view["error"]


def test_a_commit_mode_reads_at_that_commit(tmp_path):
    provider = BlobHost()
    with TestClient(build_with(tmp_path, provider)) as tc:
        sid = open_session(tc)
        topic = f"blob:{sid}:commit@deadbee:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [topic]})
            view = read_topic(ws, topic)["view"]
            if view["state"] == "loading":
                view = read_topic(ws, topic)["view"]
            assert view["sha"] == "deadbee"
            assert provider.reads == [("g/p", "a.py", "deadbee")]


def test_a_blob_needs_a_path(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"blob:{sid}:full"]})
            assert read_topic(ws, f"blob:{sid}:full")["view"]["state"] == "malformed-name"


# --- the since and commit modes ---------------------------------------------

SINCE_DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,1 +1,2 @@
 def reserve(self, pu):
+    # added since you last looked
diff --git a/pkg/__init__.py b/pkg/__init__.py
new file mode 100644
index 0000000..e69de29
"""


class VersionHost(TwoFileHost):
    """A host that knows its MR versions and can diff a single commit, on an MR that says so."""

    def __init__(self, newest_head="abc", capabilities=None):
        super().__init__()
        self.newest_head = newest_head
        self.commit_calls = []
        self.capabilities = ({"diff_versions": True, "commits": True}
                             if capabilities is None else capabilities)

    async def load(self, ref: MRRef) -> MRPayload:
        payload = await super().load(ref)
        payload.mr.capabilities = dict(self.capabilities)
        return payload

    async def mr_versions(self, ref):
        return [{"head_sha": self.newest_head, "base_sha": "base2"},
                {"head_sha": "reviewed", "base_sha": "base1"}]

    async def commit_diff(self, ref, sha):
        self.commit_calls.append((ref.project, sha))
        return [FileEntry(path="pkg/b.py", change_type=ChangeType.MODIFIED, language="python",
                          hunks=[{"diff": DIFF_B}])]


class StubWorkspace:
    def __init__(self, diff=SINCE_DIFF, fail=None):
        self.calls = []
        self._diff = diff
        self._fail = fail

    async def since_diff(self, repo, old_base, old_head, new_base, new_head):
        self.calls.append((old_base, old_head, new_base, new_head))
        if self._fail is not None:
            raise self._fail
        return {"diff": self._diff, "clean": True}


def build_versioned(tmp_path, provider=None, workspace=None, watermark="reviewed"):
    from review_mate.kb.store import ReviewKB
    provider = provider or VersionHost()
    kb = ReviewKB(root=tmp_path / "kb")
    if watermark:
        kb.set_watermark("gitlab", "g/p", 1, watermark)
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider,
                             workspace=workspace or StubWorkspace())
    app = create_app(manager=manager, provider=provider, with_mcp=False, kb=kb,
                     resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))
    return app, provider


def settled(ws, topic, limit=10):
    """The view once it stops reporting `loading`."""
    for _ in range(limit):
        view = read_topic(ws, topic, limit)["view"]
        if view["state"] != "loading":
            return view
    raise AssertionError(f"{topic} never settled")


def test_since_resolves_to_per_file_diffs(tmp_path):
    app, _ = build_versioned(tmp_path)
    with TestClient(app) as tc:
        sid = open_session(tc)
        topic = f"diff:{sid}:since"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [topic]})
            view = settled(ws, topic)
            assert view["state"] == "ready"
            # the multi-file diff was split, empty new file included
            assert [f["path"] for f in view["files"]] == ["a.py", "pkg/__init__.py"]
            assert view["head_aligned"] is True


def test_since_passes_the_watermark_bases_to_the_workspace(tmp_path):
    workspace = StubWorkspace()
    app, _ = build_versioned(tmp_path, workspace=workspace)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            settled(ws, f"diff:{sid}:since")
            assert workspace.calls == [("base1", "reviewed", "base2", "abc")]


def test_since_marks_a_view_that_cannot_anchor(tmp_path):
    """The MR advanced past this session, so the diff's new side is not the head it holds."""
    app, _ = build_versioned(tmp_path, provider=VersionHost(newest_head="moved-on"))
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            assert settled(ws, f"diff:{sid}:since")["head_aligned"] is False


def test_nothing_reviewed_yet_means_nothing_new(tmp_path):
    app, _ = build_versioned(tmp_path, watermark=None)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
            assert view["state"] == "ready" and view["files"] == []


def test_a_commit_mode_reads_that_commits_files(tmp_path):
    app, provider = build_versioned(tmp_path)
    with TestClient(app) as tc:
        sid = open_session(tc)
        topic = f"diff:{sid}:commit@deadbee"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [topic]})
            view = settled(ws, topic)
            assert [f["path"] for f in view["files"]] == ["pkg/b.py"]
            assert provider.commit_calls == [("g/p", "deadbee")]


def test_one_resolution_serves_the_list_and_the_open_file(tmp_path):
    workspace = StubWorkspace()
    app, _ = build_versioned(tmp_path, workspace=workspace)
    with TestClient(app) as tc:
        sid = open_session(tc)
        listing, body = f"diff:{sid}:since", f"diff:{sid}:since:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [listing, body]})
            assert settled(ws, listing)["state"] == "ready"
            file_view = settled(ws, body)
            assert file_view["state"] == "ready"
            assert file_view["hunks"][0]["lines"][-1]["text"] == "    # added since you last looked"
            assert len(workspace.calls) == 1        # both topics came from one resolution


def test_a_failed_resolution_is_reported(tmp_path):
    app, _ = build_versioned(tmp_path, workspace=StubWorkspace(fail=RuntimeError("git exploded")))
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
            assert view["state"] == "error" and "git exploded" in view["error"]


def test_a_conflicted_replay_is_flagged_not_hidden(tmp_path):
    """since_diff falls back to a noisier diff when the replay conflicts, and says so. The view
    carries that, because a reviewer reading target-branch changes as the author's is the failure."""
    class Conflicted(StubWorkspace):
        async def since_diff(self, repo, old_base, old_head, new_base, new_head):
            await super().since_diff(repo, old_base, old_head, new_base, new_head)
            return {"diff": SINCE_DIFF, "clean": False}

    app, _ = build_versioned(tmp_path, workspace=Conflicted())
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
            assert view["state"] == "ready" and view["clean"] is False


def test_a_clean_replay_says_so(tmp_path):
    app, _ = build_versioned(tmp_path)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            assert settled(ws, f"diff:{sid}:since")["clean"] is True


def test_a_mode_the_mr_does_not_advertise_is_unavailable(tmp_path):
    """The host implements it, but this MR says the forge cannot step through its commits. Asking
    anyway produces an error where the honest answer is that the mode is not available here."""
    provider = VersionHost(capabilities={})
    app, _ = build_versioned(tmp_path, provider=provider)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:commit@abc1234"]})
            assert settled(ws, f"diff:{sid}:commit@abc1234")["state"] == "unavailable"


def test_a_forge_with_no_versions_still_compares_against_the_watermark(tmp_path):
    """`since` is not gated on the forge versioning anything, because it does not have to be.

    A watermark and the clone are enough for a head-to-head diff, and `since_diff` keeps the head
    as its new side — so the lines are head coordinates and a comment anchors exactly as it does on
    the full diff. What is lost is that nothing excludes target-branch movement and that the
    comparison exists nowhere but here, so the view says it came from the watermark and the client
    tells the reviewer.
    """
    provider = VersionHost(capabilities={})      # no diff_versions, as GitHub reports
    app, _ = build_versioned(tmp_path, provider=provider)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
    assert view["state"] == "ready" and view["files"], view
    assert view["from_watermark"] is True
    assert view["head_aligned"] is True, "a comment has to land on the head, which is the point"


def test_a_forge_that_versions_its_diffs_is_not_reported_as_local(tmp_path):
    """The other side of the same flag: this comparison is the forge's own, and another reviewer
    asking for it gets the same answer."""
    app, _ = build_versioned(tmp_path)            # VersionHost's default capabilities version diffs
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
    assert view["state"] == "ready" and view["from_watermark"] is False


def test_each_capability_gates_only_its_own_mode(tmp_path):
    provider = VersionHost(capabilities={"commits": True})
    app, _ = build_versioned(tmp_path, provider=provider)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since",
                                                            f"diff:{sid}:commit@deadbee"]})
            # read in subscription order: settled() discards frames for other topics as it goes
            since = settled(ws, f"diff:{sid}:since")
            # `since` needs no capability of the forge's at all — only a watermark and a clone
            assert (since["state"], since["from_watermark"]) == ("ready", True)
            assert settled(ws, f"diff:{sid}:commit@deadbee")["state"] == "ready"


def test_since_reports_what_happened_to_each_file(tmp_path):
    """A move must reach the client as a move: the view is what the file tree renders from."""
    renamed = ("diff --git a/test/a/f.py b/test/b/f.py\nsimilarity index 96%\n"
               "rename from test/a/f.py\nrename to test/b/f.py\n"
               "--- a/test/a/f.py\n+++ b/test/b/f.py\n@@ -1,1 +1,1 @@\n-a\n+c\n"
               "diff --git a/added.py b/added.py\nnew file mode 100644\n--- /dev/null\n"
               "+++ b/added.py\n@@ -0,0 +1 @@\n+fresh\n")
    app, _ = build_versioned(tmp_path, workspace=StubWorkspace(diff=renamed))
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
            by_path = {f["path"]: f for f in view["files"]}
            assert by_path["test/b/f.py"]["change_type"] == "renamed"
            assert by_path["test/b/f.py"]["old_path"] == "test/a/f.py"
            assert by_path["added.py"]["change_type"] == "added"
            assert by_path["added.py"]["old_path"] is None


# --- what a finished review stops costing --------------------------------------

def _diff_topics(app):
    """The one DiffTopics the app built — what holds every resolution for the process lifetime."""
    return app.state.diff_topics


def test_closing_a_review_drops_what_it_resolved(tmp_path):
    """A resolution is keyed on a head and nothing invalidates one, so a server that runs for weeks
    accumulates every head of every review it ever opened."""
    app, _ = build_versioned(tmp_path)
    with TestClient(app) as tc:
        sid = open_session(tc)
        topic = f"diff:{sid}:since"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [topic]})
            assert settled(ws, topic)["state"] == "ready"

        topics = _diff_topics(app)
        assert [k for k in topics._resolved if k[0] == sid], "nothing was cached to drop"

        tc.post("/api/cmd", json={"cmd": "session.close", "args": {"id": sid}})
        for store in (topics._resolved, topics._failed, topics._aligned, topics._clean):
            assert [k for k in store if k[0] == sid] == [], store


def test_closing_one_review_leaves_another_alone(tmp_path):
    app, _ = build_versioned(tmp_path)
    with TestClient(app) as tc:
        first, second = open_session(tc), open_session(tc)
        for sid in (first, second):
            with tc.websocket_connect("/api/stream") as ws:
                ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
                assert settled(ws, f"diff:{sid}:since")["state"] == "ready"

        topics = _diff_topics(app)
        tc.post("/api/cmd", json={"cmd": "session.close", "args": {"id": first}})
        assert [k for k in topics._resolved if k[0] == first] == []
        assert [k for k in topics._resolved if k[0] == second], "the open review still needs its own"


def test_a_failed_resolution_is_dropped_with_the_rest(tmp_path):
    """Otherwise a closed review's error outlives it, and `_failed` is read before `_resolved`."""
    app, _ = build_versioned(tmp_path, workspace=StubWorkspace(fail=RuntimeError("git exploded")))
    with TestClient(app) as tc:
        sid = open_session(tc)
        topic = f"diff:{sid}:since"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [topic]})
            assert settled(ws, topic)["state"] == "error"

        topics = _diff_topics(app)
        assert [k for k in topics._failed if k[0] == sid]
        tc.post("/api/cmd", json={"cmd": "session.close", "args": {"id": sid}})
        assert [k for k in topics._failed if k[0] == sid] == []


# --- what a long review stops costing -----------------------------------------

class _Files:
    """A host that serves file content and counts how often it is asked for the same one."""

    def __init__(self, size: int = 1000):
        self.reads: list[tuple[str, str]] = []
        self._size = size

    async def get_file(self, project: str, path: str, ref: str) -> str:
        self.reads.append((path, ref))
        return "x" * self._size


async def _blobs(tmp_path, budget, size=1000):
    from review_mate.view.difftopic import BlobTopics
    host = _Files(size=size)
    app, _ = build_versioned(tmp_path, watermark=None)
    manager = app.state.manager
    sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
    return manager, sid, host, BlobTopics(manager, provider=host, budget=budget)


async def _settled(topics, sid, sha, path):
    """One file at one version of the code, once its content has landed."""
    for _ in range(50):
        view = await topics.build(f"{sid}:commit@{sha}:{path}")
        if view["state"] == "ready":
            return view
        await asyncio.sleep(0.01)
    raise AssertionError(f"{sha}:{path} never settled")


async def test_unfolded_content_is_bounded_by_what_it_holds(tmp_path):
    """A blob entry is keyed on the version *and* the file, and reviewing your own branch moves the
    head on every fix — so a long session accumulates a version's worth per comment answered."""
    manager, sid, _host, topics = await _blobs(tmp_path, budget=2500)
    for sha in ("aaaaaa1", "bbbbbb2", "cccccc3"):
        await _settled(topics, sid, sha, "a.py")

    assert topics._held <= 2500
    assert list(topics._generations) == ["bbbbbb2", "cccccc3"], topics._generations.keys()
    await topics.aclose()
    await manager.shutdown()


async def test_a_whole_version_goes_at_once(tmp_path):
    """The unit that stops being useful together: when the head moves, every file read at the old
    one goes cold at the same moment."""
    manager, sid, _host, topics = await _blobs(tmp_path, budget=3500)
    for path in ("a.py", "b.py", "c.py"):
        await _settled(topics, sid, "aaaaaa1", path)
    assert len(topics._generations["aaaaaa1"]) == 3

    await _settled(topics, sid, "bbbbbb2", "a.py")      # the head moved; the first version is now cold
    assert list(topics._generations) == ["bbbbbb2"]
    assert topics._held == 1000, "the whole old version went, not one file of it"
    await topics.aclose()
    await manager.shutdown()


async def test_the_version_being_read_is_not_the_one_dropped(tmp_path):
    """Reading any file of a version keeps the version, so stepping back to an older commit and
    working there does not lose it to whatever was opened most recently."""
    manager, sid, _host, topics = await _blobs(tmp_path, budget=2500)
    await _settled(topics, sid, "aaaaaa1", "a.py")
    await _settled(topics, sid, "bbbbbb2", "a.py")
    await _settled(topics, sid, "aaaaaa1", "a.py")      # back to the first: it is the recent one now
    await _settled(topics, sid, "cccccc3", "a.py")      # pushes one out

    assert list(topics._generations) == ["aaaaaa1", "cccccc3"], topics._generations.keys()
    await topics.aclose()
    await manager.shutdown()


async def test_a_dropped_file_is_fetched_again_rather_than_lost(tmp_path):
    """Nothing depends on a hit: eviction costs a read, never a view that cannot recover."""
    manager, sid, host, topics = await _blobs(tmp_path, budget=1500)
    await _settled(topics, sid, "aaaaaa1", "a.py")
    await _settled(topics, sid, "bbbbbb2", "a.py")      # evicts the first
    again = await _settled(topics, sid, "aaaaaa1", "a.py")

    assert again["state"] == "ready" and again["lines"]
    assert [ref for _p, ref in host.reads].count("aaaaaa1") == 2
    await topics.aclose()
    await manager.shutdown()


async def test_the_last_version_is_kept_even_when_it_is_too_big(tmp_path):
    """Dropping it would only make the next build fetch all of it again — the reviewer is reading
    it, and a budget is not a reason to guarantee a miss."""
    manager, sid, _host, topics = await _blobs(tmp_path, budget=100, size=1000)
    await _settled(topics, sid, "aaaaaa1", "a.py")
    assert list(topics._generations) == ["aaaaaa1"] and topics._held == 1000
    await topics.aclose()
    await manager.shutdown()


async def test_the_budget_can_be_set_without_touching_the_code(tmp_path, monkeypatch):
    from review_mate.config import blob_budget_bytes
    from review_mate.view.difftopic import BlobTopics
    monkeypatch.setenv("REVIEW_MATE_BLOB_BUDGET_MB", "4")
    assert blob_budget_bytes() == 4 * 1024 * 1024
    assert BlobTopics(None)._budget == 4 * 1024 * 1024

    monkeypatch.setenv("REVIEW_MATE_BLOB_BUDGET_MB", "not a number")
    assert BlobTopics(None)._budget == 16 * 1024 * 1024, "a typo must not stop a server starting"


async def test_a_session_keeps_being_pushed_after_its_scenario_is_restaged(tmp_path):
    """The server captures a session's writer when the first client watches it, and listens to that
    object until the session ends. Anything handing out a second writer for the same id leaves it
    listening to the first for ever, and the page watches a session that pushes it nothing again.

    The real manager guarantees one writer per id. This is the guarantee, asserted — so a fake or a
    future manager that breaks it fails here rather than as a browser test that sometimes hangs.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webui"))
    from webui.fixtures.manager import FakeManager
    from webui.fixtures.scenarios import review_with_highlights

    manager = FakeManager()
    app = create_app(manager=manager, with_mcp=False)
    topic = "chat:s1:highlight:h1"
    with TestClient(app) as tc:
        manager.put(review_with_highlights("s1"))
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [topic]})
            assert next_frame(ws) is not None, "the initial view"

            manager.put(review_with_highlights("s1"))     # re-staged, as between two tests
            tc.post("/api/sessions/s1/commands", json={
                "type": "post_message", "body": "after restaging",
                "anchor": {"kind": "highlight", "id": "h1"}})
            pushed = next_frame(ws)
            assert pushed is not None, "the session stopped pushing when it was re-staged"
            assert pushed["view"]["messages"][-1]["body"] == "after restaging"


def test_a_blob_read_that_failed_once_is_tried_again(tmp_path):
    """A failure used to be permanent. Answered from this cache for the life of the process, so one
    blip — a 502, a dropped connection, a token refreshed mid-read — took unfolding away from that
    file until the server was restarted. The sha moves only on a push, so nothing a reviewer did
    could clear it and nothing told them why; the reported symptom was "unfolding is broken", and
    restarting the server fixed it.

    Remembering the failure is still right, or a build on every republish would re-fetch a file that
    genuinely cannot be read several times a second. It just has to stop being for ever.
    """
    provider = BlobHost(fail=RuntimeError("gitlab 502"))
    app = build_with(tmp_path, provider)
    with TestClient(app) as tc:
        sid = open_session(tc)
        topic = f"blob:{sid}:full:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [topic]})
            view = read_topic(ws, topic)["view"]
            if view["state"] == "loading":
                view = read_topic(ws, topic)["view"]
            assert view["state"] == "error" and "gitlab 502" in view["error"]
        attempts = len(provider.reads)

        blobs = app.state.blob_topics
        assert len(blobs._failed) == 1, "the failure is remembered, which is what throttles retries"
        key, (reason, _at) = next(iter(blobs._failed.items()))
        blobs._failed[key] = (reason, monotonic() - _FAILED_TTL - 1)   # as if it had been a while

        provider._fail = None                     # whatever it was, it has passed
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [topic]})
            view = read_topic(ws, topic)["view"]
            if view["state"] == "loading":
                view = read_topic(ws, topic)["view"]
        assert view["state"] == "ready", view
        assert len(provider.reads) > attempts, "it never asked again"


# --- files the reviewer has finished reading ---------------------------------------------------
#
# The one property worth protecting: a push that touched some other file must leave this file's
# mark standing. A record of progress that clears itself on every push records nothing.
#
# These read through a fresh subscription rather than the next frame on an open one. A subscribe
# always delivers the current view, while the next frame is whatever was published last — and this
# file's edited diffs are deliberately the same shape as their originals, so a listing that carries
# counts and not bodies is sometimes byte-identical afterwards and correctly publishes nothing.

def _mark(tc, sid, path, on=True):
    return tc.post(f"/api/sessions/{sid}/commands",
                   json={"type": "mark_file_reviewed" if on else "unmark_file_reviewed",
                         "path": path})


def _now(tc, topic):
    with tc.websocket_connect("/api/stream") as ws:
        ws.send_json({"action": "subscribe", "topics": [topic]})
        return read_topic(ws, topic)["view"]


def _rows(tc, sid, mode="full"):
    return {f["path"]: f for f in _now(tc, f"diff:{sid}:{mode}")["files"]}


def test_a_file_is_marked_read_and_the_listing_says_so(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        assert _rows(tc, sid)["a.py"]["reviewed"] is False
        assert _mark(tc, sid, "a.py").json()["ok"] is True
        rows = _rows(tc, sid)
        assert rows["a.py"]["reviewed"] is True and rows["a.py"]["reviewed_stale"] is False
        assert rows["pkg/b.py"]["reviewed"] is False       # one file, not the change


def test_a_push_that_touched_another_file_leaves_the_mark_standing(tmp_path):
    """The whole design. Keyed on the review's head instead, every push would clear every mark."""
    host = TwoFileHost()
    with TestClient(build(tmp_path, host)) as tc:
        sid = open_session(tc)
        _mark(tc, sid, "a.py")
        host.diffs["pkg/b.py"] = DIFF_B_EDITED             # the author pushes, touching only b
        host.sha = "def"                                   # ... which moves the head, as it must
        tc.post("/api/cmd", json={"cmd": "session.resync", "args": {"session": sid}})
        row = _rows(tc, sid)["a.py"]
        assert row["reviewed"] is True
        assert row["reviewed_stale"] is False              # it did not change, so it still holds


def test_a_push_that_touched_this_file_makes_the_mark_stale_without_removing_it(tmp_path):
    host = TwoFileHost()
    with TestClient(build(tmp_path, host)) as tc:
        sid = open_session(tc)
        _mark(tc, sid, "a.py")
        host.diffs["a.py"] = DIFF_A_EDITED
        host.sha = "def"
        tc.post("/api/cmd", json={"cmd": "session.resync", "args": {"session": sid}})
        row = _rows(tc, sid)["a.py"]
        assert row["reviewed"] is True and row["reviewed_stale"] is True    # stale, not gone

        _mark(tc, sid, "a.py")                             # re-reading records this version
        again = _rows(tc, sid)["a.py"]
        assert again["reviewed"] is True and again["reviewed_stale"] is False


def test_the_mark_is_the_file_s_and_the_file_topic_carries_it_too(tmp_path):
    """The diff view modes differ in which lines they show; the mark is about the file."""
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        _mark(tc, sid, "a.py")
        assert _rows(tc, sid)["a.py"]["reviewed"] is True
        one = _now(tc, f"diff:{sid}:full:a.py")
        assert one["reviewed"] is True and one["reviewed_sha"] == "abc"


def test_unmarking_puts_the_file_back(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        _mark(tc, sid, "a.py")
        assert _rows(tc, sid)["a.py"]["reviewed"] is True
        _mark(tc, sid, "a.py", on=False)
        assert _rows(tc, sid)["a.py"]["reviewed"] is False


def test_a_file_the_change_no_longer_touches_leaves_no_mark_behind(tmp_path):
    class OneFileLater(TwoFileHost):
        dropped = False

        async def load(self, ref):
            payload = await super().load(ref)
            if self.dropped:
                payload.files = [f for f in payload.files if f.path != "a.py"]
            return payload

    host = OneFileLater()
    with TestClient(build(tmp_path, host)) as tc:
        sid = open_session(tc)
        _mark(tc, sid, "a.py")
        host.dropped = True
        tc.post("/api/cmd", json={"cmd": "session.resync", "args": {"session": sid}})
        state = tc.get(f"/api/sessions/{sid}").json()
        assert [f["path"] for f in state["files"]] == ["pkg/b.py"]
        assert state["reviewed_files"] == []       # no orphan, as a draft's highlight going


def test_a_file_that_is_not_in_the_change_cannot_be_marked(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        resp = _mark(tc, sid, "nowhere.py")
        assert resp.status_code == 400 and "no such file" in resp.json()["reason"]


def test_the_mark_shows_in_every_diff_view_mode(tmp_path):
    """A reviewer marks a file read in one mode and finds it marked in the others.

    The modes differ in which of a file's lines they show, and in which files they list at all —
    full against the target, since against the watermark, commit@ within one commit. None of that
    changes which file it is, so the mark is read from the session's own files rather than from
    the mode's, and a file a mode lists but nobody marked stays unmarked.
    """
    app, _ = build_versioned(tmp_path)
    with TestClient(app) as tc:
        sid = open_session(tc)
        for path in ("a.py", "pkg/b.py"):
            assert _mark(tc, sid, path).json()["ok"] is True

        # ... and nothing has changed since, so no mode may call the mark stale. A mode's own
        # hunks are not the file's: comparing against those reports every marked file stale the
        # moment it is read in since or commit@.
        expected = {                              # topic -> {path: (reviewed, stale)}
            f"diff:{sid}:full": {"a.py": (True, False), "pkg/b.py": (True, False)},
            f"diff:{sid}:since": {"a.py": (True, False), "pkg/__init__.py": (False, False)},
            f"diff:{sid}:commit@abcdef0": {"pkg/b.py": (True, False)},
        }
        for topic, want in expected.items():
            with tc.websocket_connect("/api/stream") as ws:
                ws.send_json({"action": "subscribe", "topics": [topic]})
                view = settled(ws, topic)
                assert view["state"] == "ready", (topic, view)
                got = {f["path"]: (f["reviewed"], f["reviewed_stale"]) for f in view["files"]}
                assert got == want, topic


async def test_a_repository_out_of_reach_is_said_in_words_a_reviewer_can_act_on(tmp_path):
    """The forge half of a review keeps working when the server has no git credentials, so the
    change loads and only what comes from the repository is missing. Handing over git's own words
    about a promisor remote sends the reviewer to look at the merge request."""
    workspace = StubWorkspace(fail=RepoUnreadable(
        "git fetch failed: Please make sure you have the correct access rights"))
    app, _ = build_versioned(tmp_path, workspace=workspace)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
    assert view["state"] == "error"
    # the protocol and the host, which is the pair the reviewer has to go and fix, and the one
    # thing git's own words never say plainly
    assert "no ssh credentials for gitlab" in view["error"]
    assert "promisor" not in view["error"] and "fatal" not in view["error"]


async def test_a_failure_about_the_change_still_says_what_it_was(tmp_path):
    """Only the one a reviewer can act on is translated; the rest keep their own words, which are
    the only clue to what went wrong."""
    workspace = StubWorkspace(fail=RuntimeError("bad object deadbeef"))
    app, _ = build_versioned(tmp_path, workspace=workspace)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
    assert view["state"] == "error" and "bad object deadbeef" in view["error"]
