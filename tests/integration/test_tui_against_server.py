"""The second client against a real server over a real socket.

This is the contract's proof: the TUI renders reviews it never modelled, and acts on them through
the same command path the browser uses. If the view protocol were browser-shaped, it would show
up here first.
"""
import asyncio
from contextlib import asynccontextmanager

import pytest
import uvicorn

from conftest import HostStub
from review_mate.contracts import MRRef
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.tui.app import HubScreen
from review_mate.tui.client import ViewClient

pytest.importorskip("websockets")


@asynccontextmanager
async def serving(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 10
    while not server.started:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("server did not start")
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


class Watcher:
    """Waits for a view satisfying a predicate, driven by the client's own callback."""

    def __init__(self, client: ViewClient):
        self.client = client
        self.also = None            # the shell, once a test has one — wired as __main__ does
        self._predicate = None
        self._event = asyncio.Event()

    def notify(self) -> None:
        if self.also is not None:
            self.also()
        view = self.client.views.get("hub")
        if self._predicate is not None and view is not None and self._predicate(view):
            self._event.set()

    async def until(self, predicate, timeout=10.0) -> dict:
        self._predicate = predicate
        self._event.clear()
        self.notify()
        await asyncio.wait_for(self._event.wait(), timeout)
        return self.client.views["hub"]


@asynccontextmanager
async def connected(base_url):
    client = ViewClient(base_url)
    watcher = Watcher(client)
    stream = asyncio.create_task(client.run(["hub"], watcher.notify))
    try:
        yield client, watcher
    finally:
        stream.cancel()
        try:
            await stream
        except asyncio.CancelledError:
            pass



async def wait_for(predicate, timeout=10.0):
    """Poll a cheap local predicate until it holds — the client's own callback drives the views."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.01)


def _tmp_kb(tmp_path):
    from review_mate.kb.store import ReviewKB
    return ReviewKB(root=tmp_path / "kb")


def build(tmp_path, provider, host_writer=None):
    from review_mate.kb.store import ReviewKB
    from review_mate.writeback.service import Writeback
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider)
    # tmp-rooted on purpose: create_app defaults the KB to ~/.review-mate, and a test that submits
    # a review would otherwise write a watermark into the reviewer's own store
    return create_app(manager=manager, provider=provider, with_mcp=False,
                      writeback=Writeback(manager, host_writer) if host_writer is not None else None,
                      kb=ReviewKB(root=tmp_path / "kb"),
                      resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))


class SpyHostWriter:
    """A host that records what a review sent it, so a test can name it rather than count calls."""

    def __init__(self):
        self.posted = []
        self.approved = False
        self.replied = []
        self.resolved = []

    def capabilities(self):
        from review_mate.host.base import GITLAB_CAPABILITIES
        return dict(GITLAB_CAPABILITIES)

    async def post_comment(self, ref, position, body):
        self.posted.append(body)
        return {"id": "disc-1", "notes": [{"id": 11}]}

    async def post_mr_comment(self, ref, body):
        self.posted.append(body)
        return {"id": 12}

    async def approve(self, ref):
        self.approved = True
        return {}

    async def reply(self, ref, thread_id, body):
        self.replied.append((thread_id, body))
        return {"id": "note-1"}

    async def resolve(self, ref, thread_id, resolved=True):
        self.resolved.append((thread_id, resolved))
        return {}


async def test_the_tui_client_renders_a_hub_it_never_modelled(tmp_path):
    provider = HostStub()
    async with serving(build(tmp_path, provider)) as base:
        async with connected(base) as (client, watcher):
            view = await watcher.until(lambda v: v.get("queue_state") == "ready")
            assert client.status == "live"
            assert view["user"] == "reviewer"
            assert view["queue"][0]["iid"] == 7
            assert view["sessions"] == []


async def test_the_tui_acts_through_the_same_command_path(tmp_path):
    provider = HostStub()
    async with serving(build(tmp_path, provider)) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")

            ok = await client.command("session.open",
                                      ref={"host": "gitlab", "project": "g/p", "iid": 1})
            assert ok, client.last_command_error
            view = await watcher.until(lambda v: v["sessions"])
            sid = view["sessions"][0]["id"]
            assert view["sessions"][0]["host_checked"] is False

            assert await client.command("hub.refresh"), client.last_command_error
            view = await watcher.until(lambda v: v["sessions"] and v["sessions"][0]["host_checked"])
            assert view["sessions"][0]["unresolved"] == 1
            assert view["sessions"][0]["state"] == "discussions"

            assert await client.command("session.close", id=sid), client.last_command_error
            view = await watcher.until(lambda v: not v["sessions"])
            assert view["sessions"] == []


async def test_a_rejected_command_reports_why(tmp_path):
    async with serving(build(tmp_path, HostStub())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            assert await client.command("session.close", id="ghost") is False
            assert "unknown session" in client.last_command_error


async def test_the_rendered_screen_shows_what_the_server_sent(tmp_path):
    """Protocol and rendering together: the text a reviewer would actually read."""
    async with serving(build(tmp_path, HostStub())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            screen = HubScreen(client)
            rendered = "".join(text for _, text in screen.fragments())
            assert "review-mate · reviewer" in rendered
            assert "g/p!7  queued" in rendered
            assert "none" in rendered                    # no open reviews yet
            assert "j/k move" in rendered


# --- the review screen against a real server --------------------------------

DIFF_A = """@@ -1,2 +1,3 @@ def reserve(self, pu):
 def reserve(self, pu):
-    if pu.legacy:
+    if pu.fleet == LEGACY:
+        q = self._legacy
"""
DIFF_B = "@@ -10,1 +10,1 @@\n-old = 1\n+new = 2\n"


class DiffHost(HostStub):
    async def load(self, ref):
        from review_mate.session.state import ChangeType, FileEntry, MRMetadata
        from review_mate.contracts import MRPayload
        return MRPayload(
            mr=MRMetadata(host="gitlab", project=ref.project, iid=ref.iid, title="reserve capacity",
                          source_branch="x", target_branch="main", sha="abc", author="dev",
                          url="http://x"),
            files=[FileEntry(path="a.py", change_type=ChangeType.MODIFIED, language="python",
                             hunks=[{"diff": DIFF_A}]),
                   FileEntry(path="pkg/b.py", change_type=ChangeType.MODIFIED, language="python",
                             hunks=[{"diff": DIFF_B}])],
            threads=[])


async def test_the_review_screen_renders_a_real_diff(tmp_path):
    from review_mate.tui.app import Shell

    async with serving(build(tmp_path, DiffHost())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            assert await client.command("session.open",
                                        ref={"host": "gitlab", "project": "g/p", "iid": 1})
            view = await watcher.until(lambda v: v["sessions"])
            session = view["sessions"][0]["id"]

            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            listing, body = f"diff:{session}:full", f"diff:{session}:full:a.py"
            await wait_for(lambda: client.views.get(listing) and client.views.get(body))

            rendered = "".join(text for _, text in shell.fragments())
            assert "g/p !1" in rendered and "reserve capacity" in rendered
            assert "a.py" in rendered and "pkg/b.py" in rendered          # the file pane
            assert "if pu.fleet == LEGACY:" in rendered                   # the body

            # the panes share a 24-row terminal, so the rest of the file is a scroll away
            shell.diff.focus = "body"
            for _ in range(6):
                shell.diff.move(1)
            assert "q = self._legacy" in "".join(text for _, text in shell.fragments())


async def test_moving_between_files_moves_the_subscription(tmp_path):
    from review_mate.tui.app import Shell

    async with serving(build(tmp_path, DiffHost())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]

            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            first, second = f"diff:{session}:full:a.py", f"diff:{session}:full:pkg/b.py"
            await wait_for(lambda: client.views.get(first))

            previous = shell.diff.wanted()
            shell.diff.next_file(1)
            await shell.resync(previous)
            await wait_for(lambda: client.views.get(second))

            assert second in client.views
            assert first not in client.views        # dropped, so the server stops building it
            rendered = "".join(text for _, text in shell.fragments())
            assert "new = 2" in rendered


async def test_leaving_a_review_drops_its_scopes(tmp_path):
    from review_mate.tui.app import Shell

    async with serving(build(tmp_path, DiffHost())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]
            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            await wait_for(lambda: client.views.get(f"diff:{session}:full"))
            await shell.leave_review()
            assert not [scope for scope in client.views if scope.startswith("diff:")]
            assert shell.screen is shell.hub


async def test_highlighting_in_the_terminal_reaches_the_annotations(tmp_path):
    """The write path and the read path meet: a selection becomes a command, the session changes,
    the tail republishes, and the annotations the screen renders comes back with it."""
    from review_mate.tui.app import Shell

    async with serving(build(tmp_path, DiffHost())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]

            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            await wait_for(lambda: client.views.get(f"diff:{session}:full:a.py"))
            await wait_for(lambda: client.views.get(f"annotations:{session}") is not None)

            screen = shell.diff
            assert screen.highlights == []
            screen.focus = "body"
            screen.body_cursor = next(i for i, row in enumerate(screen.body_rows())
                                      if row["line"] == 2)
            assert screen.start_or_commit_selection() is None        # anchor
            command = screen.start_or_commit_selection()
            assert await client.session_command(session, command), client.last_command_error

            await wait_for(lambda: screen.highlights)
            highlight = screen.highlights[0]
            assert (highlight["n"], highlight["file"]) == (1, "a.py")
            assert (highlight["start"], highlight["end"]) == (2, 2)
            rendered = "".join(text for _, text in shell.fragments())
            assert "#1" in rendered and "a.py:2-2" in rendered
            assert "▌" in rendered                                    # marked in the body too


async def test_escalating_a_highlight_is_accepted_by_the_session(tmp_path):
    from review_mate.tui.app import Shell

    async with serving(build(tmp_path, DiffHost())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]
            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            await wait_for(lambda: client.views.get(f"diff:{session}:full:a.py"))

            screen = shell.diff
            screen.focus = "body"
            screen.body_cursor = next(i for i, row in enumerate(screen.body_rows())
                                      if row["line"] == 2)
            screen.start_or_commit_selection()
            await client.session_command(session, screen.start_or_commit_selection())
            await wait_for(lambda: screen.highlights)

            screen.focus = "annotations"
            ask = screen.ask_command()
            assert ask["type"] == "request_context"
            assert await client.session_command(session, ask), client.last_command_error


async def test_writing_in_the_terminal_reaches_the_conversation(tmp_path):
    """The terminal holds a chat over the same protocol: the message it writes becomes a
    command, the session changes, and the chat scope comes back carrying it."""
    from review_mate.tui.app import Shell

    async with serving(build(tmp_path, DiffHost())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]

            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            await wait_for(lambda: client.views.get(f"chat:{session}:review") is not None)

            shell.start_compose()
            shell.compose.text = "what is this guard for?"
            _via, command = shell.compose_submission()
            shell.cancel_compose()
            assert await client.session_command(session, command), client.last_command_error

            await wait_for(lambda: shell.diff.chat.get("messages"))
            said = shell.diff.chat["messages"][0]
            assert (said["role"], said["body"]) == ("user", "what is this guard for?")
            assert shell.diff.chat["owed"] is True          # the agent owes an answer
            rendered = "".join(text for _, text in shell.fragments())
            assert "what is this guard for?" in rendered


async def test_the_terminal_is_told_whether_anyone_is_listening(tmp_path):
    """The label is the server's: presence joined with what is outstanding, not a rule here."""
    from review_mate.tui.app import Shell

    app = build(tmp_path, DiffHost())
    async with serving(app) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]
            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            await wait_for(lambda: client.views.get(f"chat:{session}") is not None)

            def agent():
                return (client.views.get(f"chat:{session}") or {}).get("agent", {})

            await wait_for(lambda: agent().get("state") == "off")     # nothing asked, none listening
            await client.session_command(session, {"type": "post_message", "body": "look?"})
            await wait_for(lambda: agent().get("state") == "stalled")  # asked, and still nobody
            assert "nothing is listening" in "".join(t for _, t in shell.fragments())


async def test_drafting_in_the_terminal_reaches_the_review(tmp_path):
    """The terminal prepares a review comment over the same protocol: what it writes becomes a
    command, the session changes, and the review scope comes back carrying it."""
    from review_mate.tui.app import Shell

    async with serving(build(tmp_path, DiffHost())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]

            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            await wait_for(lambda: client.views.get(f"review:{session}") is not None)

            shell.start_compose("draft")
            shell.compose.text = "this needs a test"
            _via, command = shell.compose_submission()
            shell.cancel_compose()
            assert await client.session_command(session, command), client.last_command_error

            await wait_for(lambda: shell.diff.review.get("drafts"))
            review = shell.diff.review
            assert review["pending"] == 1 and review["posted"] == 0
            prepared = review["drafts"][0]
            assert prepared["body"] == "this needs a test"
            assert prepared["highlight_id"] is None      # nothing selected, so it is the summary
            # and reopening the composer starts from it rather than from blank
            assert shell.diff.draft_body() == "this needs a test"


async def test_sending_a_review_from_the_terminal_reaches_the_host(tmp_path):
    """The whole point of the commands: a review prepared in the terminal leaves from the terminal,
    through the same sequence the browser's submit runs."""
    from review_mate.tui.app import Shell

    host_writer = SpyHostWriter()
    async with serving(build(tmp_path, DiffHost(), host_writer=host_writer)) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]

            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            await wait_for(lambda: client.views.get(f"review:{session}") is not None)

            shell.start_compose("draft")
            shell.compose.text = "reads well overall"
            _via, command = shell.compose_submission()
            shell.cancel_compose()
            assert await client.session_command(session, command), client.last_command_error
            await wait_for(lambda: shell.diff.review.get("pending") == 1)

            assert await client.command("review.submit", session=session, approve=False), \
                client.last_command_error
            await wait_for(lambda: shell.diff.review.get("posted") == 1)

    assert host_writer.posted == ["reads well overall"]
    assert host_writer.approved is False
    assert shell.diff.review["pending"] == 0


async def test_the_terminal_reads_the_merge_requests_discussions(tmp_path):
    """The discussions arrive on their own scope, with the reviewer's own comments marked as such —
    the terminal never asks who it is and compares names."""
    from review_mate.tui.app import Shell
    from review_mate.session.commands import ReplaceThreads
    from review_mate.session.state import Origin, ReviewThread, ThreadComment

    host = DiffHost()
    manager = SessionManager(root=tmp_path / "sessions", mr_source=host)
    app = create_app(manager=manager, provider=host, with_mcp=False,
                     kb=_tmp_kb(tmp_path),
                     resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))

    async with serving(app) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]
            await manager.get(session).submit(ReplaceThreads(threads=[
                ReviewThread(id="d1", anchor={"file": "a.py", "side": "new", "line": 2},
                             comments=[ThreadComment(id="1", author="eric", body="prefer a guard"),
                                       ThreadComment(id="2", author="reviewer", body="agreed")]),
                ReviewThread(id="d2", resolved=True),
            ]), Origin.SYSTEM)

            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            await wait_for(lambda: shell.diff.threads.get("threads"))

            view = shell.diff.threads
            assert view["total"] == 2 and view["unresolved"] == 1
            mine = {c["author"]: c["mine"] for c in view["threads"][0]["comments"]}
            assert mine == {"eric": False, "reviewer": True}      # DiffHost's username is reviewer
            assert [t["id"] for t in shell.diff.thread_rows()] == ["d1"]   # open ones lead
            rendered = "".join(text for _, text in shell.fragments())
            assert "prefer a guard" in rendered and "1 open of 2" in rendered


async def test_replying_from_the_terminal_reaches_the_merge_request(tmp_path):
    """The reply leaves the terminal, lands on the host, and the discussion comes back carrying it
    — the re-sync is what makes the pane show what the merge request says rather than a guess."""
    from review_mate.tui.app import Shell
    from review_mate.session.commands import ReplaceThreads
    from review_mate.session.state import Origin, ReviewThread, ThreadComment

    answered = [ReviewThread(id="d1", anchor={"file": "a.py", "side": "new", "line": 2},
                             comments=[ThreadComment(id="1", author="eric", body="prefer a guard"),
                                       ThreadComment(id="2", author="reviewer", body="fixed")])]

    class Answering(DiffHost):
        async def fetch_threads(self, ref):
            return list(answered)

    host_writer = SpyHostWriter()
    host = Answering()
    app = build(tmp_path, host, host_writer=host_writer)

    async with serving(app) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]
            manager = app.state.manager
            await manager.get(session).submit(ReplaceThreads(threads=[
                ReviewThread(id="d1", anchor={"file": "a.py", "side": "new", "line": 2},
                             comments=[ThreadComment(id="1", author="eric",
                                                     body="prefer a guard")])]), Origin.SYSTEM)

            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            await wait_for(lambda: shell.diff.threads.get("threads"))

            shell.diff.focus = "threads"
            shell.start_compose("reply")
            shell.compose.text = "fixed"
            via, payload = shell.compose_submission()
            shell.cancel_compose()
            assert via == "view"
            assert await client.command(payload.pop("cmd"), **payload), client.last_command_error

            await wait_for(lambda: len(shell.diff.threads["threads"][0]["comments"]) == 2)

    assert host_writer.replied == [("d1", "fixed")]
    said = shell.diff.threads["threads"][0]["comments"]
    assert [(c["body"], c["mine"]) for c in said] == [("prefer a guard", False), ("fixed", True)]


async def test_browsing_the_repository_from_the_terminal(tmp_path):
    """The repository listing is read while the browser is open and not otherwise, and a file that
    the change never touched is read from its blob rather than from a diff that does not exist."""
    from review_mate.tui.app import Shell

    class Browsable(DiffHost):
        async def get_repo_tree(self, project, ref, max_pages=30):
            return ["a.py", "docs/README.md"]

        async def get_file(self, project, path, ref):
            return "# the readme\nsecond line\n"

    async with serving(build(tmp_path, Browsable())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            await client.command("session.open", ref={"host": "gitlab", "project": "g/p", "iid": 1})
            session = (await watcher.until(lambda v: v["sessions"]))["sessions"][0]["id"]

            shell = Shell(client)
            watcher.also = shell.on_change
            await shell.open_review(session)
            assert f"tree:{session}" not in client.views      # nobody has asked to browse

            previous = shell.diff.wanted()
            shell.diff.toggle_browse()
            await shell.resync(previous)
            await wait_for(lambda: shell.diff.repo_paths())
            assert "docs/README.md" in shell.diff.repo_paths()

            shell.diff.file_index = [r["path"] for r in shell.diff.browse_rows()].index(
                "docs/README.md")
            previous = shell.diff.wanted()
            assert shell.diff.open_current() is True
            await shell.resync(previous)
            await wait_for(lambda: shell.diff.blob.get("state") == "ready")

            rendered = "".join(text for _, text in shell.fragments())
            assert "# the readme" in rendered and "second line" in rendered
