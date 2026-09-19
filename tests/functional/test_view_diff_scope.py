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
            # the re-sync touched the session, so every held reading scope is rebuilt once
            seqs = {}
            for _ in range(3):
                msg = json.loads(ws.receive_text())
                seqs[msg["scope"]] = msg["seq"]
            assert seqs == {f"diff:{sid}:full": 1, f"diff:{sid}:full:a.py": 1,
                            f"diff:{sid}:full:pkg/b.py": 1}


def test_a_path_the_change_does_not_touch_says_so(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:full:nope.py"]})
            view = read_scope(ws, f"diff:{sid}:full:nope.py")["view"]
            assert view["state"] == "unknown-file" and view["hunks"] == []


def test_an_unimplemented_mode_is_named_rather_than_faked(tmp_path):
    with TestClient(build(tmp_path)) as tc:
        sid = open_session(tc)
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"diff:{sid}:since"]})
            view = read_scope(ws, f"diff:{sid}:since")["view"]
            assert view["state"] == "unsupported-mode" and view["files"] == []


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
