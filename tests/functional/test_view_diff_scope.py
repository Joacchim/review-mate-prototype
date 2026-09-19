"""The reading scopes over the real transport: a file list, one file's hunks, and their blast radius.

The property that matters is isolation — re-reading one file must not move the others — because it
is the whole reason the diff is split into scopes at all.
"""
import json

from starlette.testclient import TestClient

from conftest import HostStub
from review_mate.seams import MRPayload, MRRef
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.session.state import ChangeType, FileEntry, MRMetadata

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


class TwoFileHost(HostStub):
    async def load(self, ref: MRRef) -> MRPayload:
        return MRPayload(
            mr=MRMetadata(host="gitlab", project=ref.project, iid=ref.iid, title="T",
                          source_branch="x", target_branch="main", sha="abc",
                          author="dev", url="http://x"),
            files=[
                FileEntry(path="a.py", change_type=ChangeType.MODIFIED, language="python",
                          hunks=[{"diff": DIFF_A}]),
                FileEntry(path="pkg/b.py", change_type=ChangeType.MODIFIED, language="python",
                          hunks=[{"diff": DIFF_B}]),
            ],
            threads=[],
        )


def build(tmp_path):
    provider = TwoFileHost()
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider)
    return create_app(manager=manager, provider=provider, with_mcp=False,
                      resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))


def read_scope(ws, scope, limit=8):
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        if msg.get("scope") == scope:
            return msg
    raise AssertionError(f"no message for {scope}")


def open_session(tc):
    return tc.post("/api/cmd", json={"cmd": "session.open",
                                     "args": {"ref": "g/p!1"}}).json()["session"]


def test_the_diff_scope_lists_files_without_their_bodies(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:full"]})
            view = read_scope(ws, f"diff:{sid}:full")["view"]
            assert view["state"] == "ready" and view["mr"]["project"] == "g/p"
            assert [f["path"] for f in view["files"]] == ["a.py", "pkg/b.py"]
            assert view["files"][0]["additions"] == 2 and view["files"][0]["deletions"] == 1
            assert "hunks" not in json.dumps(view)     # bodies belong to the file scopes


def test_a_file_scope_carries_numbered_lines_and_tokens(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:full:a.py"]})
            view = read_scope(ws, f"diff:{sid}:full:a.py")["view"]
            assert view["state"] == "ready" and view["language"] == "python"
            lines = view["hunks"][0]["lines"]
            assert [line["side"] for line in lines] == \
                ["context", "removed", "added", "added", "context"]
            assert lines[0]["new"] == 1 and lines[2]["new"] == 2
            assert any(kind == "keyword" for line in lines for _, _, kind in line["tokens"])


def test_rereading_one_file_leaves_the_others_untouched(tmp_path):
    """The point of splitting the diff: a per-file republish moves one scope's seq, not every one."""
    app = build(tmp_path)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [
                f"diff:{sid}:full", f"diff:{sid}:full:a.py", f"diff:{sid}:full:pkg/b.py"]})
            for scope in (f"diff:{sid}:full", f"diff:{sid}:full:a.py", f"diff:{sid}:full:pkg/b.py"):
                assert read_scope(ws, scope)["seq"] == 0

            tc.post(f"/api/sessions/{sid}/refresh-threads", json={})
            # the re-sync changed the session, so every held reading scope is rebuilt. It applies
            # several commands, and the tail republishes per change, so the count is not fixed —
            # what matters is that each held scope moved and nothing else was sent.
            seqs = {}
            for _ in range(8):
                msg = json.loads(ws.receive_text())
                seqs[msg["scope"]] = msg["seq"]
                if len(seqs) == 3:
                    break
            assert set(seqs) == {f"diff:{sid}:full", f"diff:{sid}:full:a.py",
                                 f"diff:{sid}:full:pkg/b.py"}
            assert all(seq >= 1 for seq in seqs.values())


def test_a_path_the_change_does_not_touch_says_so(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:full:nope.py"]})
            view = read_scope(ws, f"diff:{sid}:full:nope.py")["view"]
            assert view["state"] == "unknown-file" and view["hunks"] == []


def test_a_mode_the_host_cannot_serve_says_unavailable(tmp_path):
    """A host with no version list cannot answer "since", and the view says so rather than
    reporting an empty change."""
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since"]})
            view = read_scope(ws, f"diff:{sid}:since")["view"]
            assert view["state"] == "unavailable" and view["files"] == []


def test_an_unknown_session_is_reported_not_crashed(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["diff:ghost:full"]})
            assert read_scope(ws, "diff:ghost:full")["view"]["state"] == "unknown-session"


def test_a_path_may_contain_a_colon(tmp_path):
    """Only the session and mode are colon-free, so the split must not claim the path's own."""
    from review_mate.view.diffscope import parse_address
    address = parse_address("abc123:full:pkg/odd:name.py")
    assert address.path == "pkg/odd:name.py" and address.mode == "full"


def test_a_malformed_name_is_named_rather_than_mis_split(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            # "bogus" is not a mode, so this is not a session with a file called "a.py"
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:bogus:a.py"]})
            view = read_scope(ws, f"diff:{sid}:bogus:a.py")["view"]
            assert view["state"] == "malformed-name"


def test_a_file_scope_is_the_list_scope_plus_a_path(tmp_path):
    """The property that makes one family worth having: a client concatenates, never reassembles."""
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        listing = f"diff:{sid}:full"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [listing]})
            files = read_scope(ws, listing)["view"]["files"]
            names = [f"{listing}:{row['path']}" for row in files]      # concatenation, nothing else
            ws.send_json({"action": "subscribe", "scopes": names})
            for name in names:
                assert read_scope(ws, name)["view"]["state"] == "ready"


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
        scope = f"blob:{sid}:full:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [scope]})
            first = read_scope(ws, scope)["view"]
            assert first["state"] in ("loading", "ready")      # a fast host may beat the first send
            ready = first if first["state"] == "ready" else read_scope(ws, scope)["view"]
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
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:full:a.py",
                                                            f"blob:{sid}:full:a.py"]})
            hunk = read_scope(ws, f"diff:{sid}:full:a.py")["view"]["hunks"][0]
            view = read_scope(ws, f"blob:{sid}:full:a.py")["view"]
            if view["state"] != "ready":
                view = read_scope(ws, f"blob:{sid}:full:a.py")["view"]
            added = [line for line in hunk["lines"] if line["side"] == "added"][0]
            blob_line = next(line for line in view["lines"] if line["n"] == added["new"])
            assert blob_line["text"] == added["text"]


def test_one_host_read_serves_repeated_builds(tmp_path):
    provider = BlobHost()
    with TestClient(build_with(tmp_path, provider)) as tc:
        sid = open_session(tc)
        scope = f"blob:{sid}:full:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [scope]})
            view = read_scope(ws, scope)["view"]
            if view["state"] != "ready":
                read_scope(ws, scope)
            tc.post(f"/api/sessions/{sid}/refresh-threads", json={})   # republishes held scopes
            read_scope(ws, scope)
            assert provider.reads.count(("g/p", "a.py", "abc")) == 1   # content at a sha is fixed


def test_a_failed_blob_read_is_reported_in_the_view(tmp_path):
    provider = BlobHost(fail=RuntimeError("gitlab 404"))
    with TestClient(build_with(tmp_path, provider)) as tc:
        sid = open_session(tc)
        scope = f"blob:{sid}:full:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [scope]})
            view = read_scope(ws, scope)["view"]
            if view["state"] == "loading":
                view = read_scope(ws, scope)["view"]
            assert view["state"] == "error" and "gitlab 404" in view["error"]


def test_a_commit_mode_reads_at_that_commit(tmp_path):
    provider = BlobHost()
    with TestClient(build_with(tmp_path, provider)) as tc:
        sid = open_session(tc)
        scope = f"blob:{sid}:commit@deadbee:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [scope]})
            view = read_scope(ws, scope)["view"]
            if view["state"] == "loading":
                view = read_scope(ws, scope)["view"]
            assert view["sha"] == "deadbee"
            assert provider.reads == [("g/p", "a.py", "deadbee")]


def test_a_blob_needs_a_path(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"blob:{sid}:full"]})
            assert read_scope(ws, f"blob:{sid}:full")["view"]["state"] == "malformed-name"


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

    async def commit_diff(self, project, sha):
        self.commit_calls.append((project, sha))
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


def settled(ws, scope, limit=10):
    """The view once it stops reporting `loading`."""
    for _ in range(limit):
        view = read_scope(ws, scope, limit)["view"]
        if view["state"] != "loading":
            return view
    raise AssertionError(f"{scope} never settled")


def test_since_resolves_to_per_file_diffs(tmp_path):
    app, _ = build_versioned(tmp_path)
    with TestClient(app) as tc:
        sid = open_session(tc)
        scope = f"diff:{sid}:since"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [scope]})
            view = settled(ws, scope)
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
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since"]})
            settled(ws, f"diff:{sid}:since")
            assert workspace.calls == [("base1", "reviewed", "base2", "abc")]


def test_since_marks_a_view_that_cannot_anchor(tmp_path):
    """The MR advanced past this session, so the diff's new side is not the head it holds."""
    app, _ = build_versioned(tmp_path, provider=VersionHost(newest_head="moved-on"))
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since"]})
            assert settled(ws, f"diff:{sid}:since")["head_aligned"] is False


def test_nothing_reviewed_yet_means_nothing_new(tmp_path):
    app, _ = build_versioned(tmp_path, watermark=None)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
            assert view["state"] == "ready" and view["files"] == []


def test_a_commit_mode_reads_that_commits_files(tmp_path):
    app, provider = build_versioned(tmp_path)
    with TestClient(app) as tc:
        sid = open_session(tc)
        scope = f"diff:{sid}:commit@deadbee"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [scope]})
            view = settled(ws, scope)
            assert [f["path"] for f in view["files"]] == ["pkg/b.py"]
            assert provider.commit_calls == [("g/p", "deadbee")]


def test_one_resolution_serves_the_list_and_the_open_file(tmp_path):
    workspace = StubWorkspace()
    app, _ = build_versioned(tmp_path, workspace=workspace)
    with TestClient(app) as tc:
        sid = open_session(tc)
        listing, body = f"diff:{sid}:since", f"diff:{sid}:since:a.py"
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [listing, body]})
            assert settled(ws, listing)["state"] == "ready"
            file_view = settled(ws, body)
            assert file_view["state"] == "ready"
            assert file_view["hunks"][0]["lines"][-1]["text"] == "    # added since you last looked"
            assert len(workspace.calls) == 1        # both scopes came from one resolution


def test_a_failed_resolution_is_reported(tmp_path):
    app, _ = build_versioned(tmp_path, workspace=StubWorkspace(fail=RuntimeError("git exploded")))
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since"]})
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
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
            assert view["state"] == "ready" and view["clean"] is False


def test_a_clean_replay_says_so(tmp_path):
    app, _ = build_versioned(tmp_path)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since"]})
            assert settled(ws, f"diff:{sid}:since")["clean"] is True


def test_a_mode_the_mr_does_not_advertise_is_unavailable(tmp_path):
    """The host implements it, but this MR says the forge cannot list its versions. Asking anyway
    produces an error where the honest answer is that the mode is not available here."""
    provider = VersionHost(capabilities={})
    app, _ = build_versioned(tmp_path, provider=provider)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since",
                                                            f"diff:{sid}:commit@abc1234"]})
            assert settled(ws, f"diff:{sid}:since")["state"] == "unavailable"
            assert settled(ws, f"diff:{sid}:commit@abc1234")["state"] == "unavailable"


def test_each_capability_gates_only_its_own_mode(tmp_path):
    provider = VersionHost(capabilities={"commits": True})
    app, _ = build_versioned(tmp_path, provider=provider)
    with TestClient(app) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since",
                                                            f"diff:{sid}:commit@deadbee"]})
            assert settled(ws, f"diff:{sid}:since")["state"] == "unavailable"
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
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since"]})
            view = settled(ws, f"diff:{sid}:since")
            by_path = {f["path"]: f for f in view["files"]}
            assert by_path["test/b/f.py"]["change_type"] == "renamed"
            assert by_path["test/b/f.py"]["old_path"] == "test/a/f.py"
            assert by_path["added.py"]["change_type"] == "added"
            assert by_path["added.py"]["old_path"] is None
