"""Reviewing a branch that is still on this machine, before anyone else is asked to look at it.

Against a real repository, because the whole provider is git: a stub would only prove that the
methods I wrote call the methods I wrote. What matters is that a branch produces the same diff a
merge request would — from where it left its base, not from wherever the base has got to since.
"""
import asyncio
import os
import subprocess

import pytest

from review_mate.host.local import GitError, LocalBranchProvider
from review_mate.contracts import LocalRef

_ENV = {"GIT_AUTHOR_NAME": "the agent", "GIT_AUTHOR_EMAIL": "a@a",
        "GIT_COMMITTER_NAME": "the agent", "GIT_COMMITTER_EMAIL": "a@a",
        "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=_ENV).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repository whose base moved on after the branch left it — the ordinary case."""
    path = tmp_path / "work"
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "queue.py").write_text("def drain():\n    pass\n")
    (path / "README.md").write_text("# work\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "base")

    git(path, "checkout", "-b", "feat/retry")
    (path / "queue.py").write_text("def drain():\n    retry()\n")
    (path / "retry.py").write_text("def retry():\n    return 1\n")
    git(path, "rm", "-q", "README.md")
    git(path, "add", ".")
    git(path, "commit", "-m", "add a retry to the drain")

    git(path, "checkout", "main")            # the base moves on, as it does
    (path / "unrelated.py").write_text("x = 1\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "someone else's work")
    git(path, "checkout", "feat/retry")
    return path


@pytest.fixture
def provider():
    return LocalBranchProvider()


async def test_it_reviews_the_branch_against_where_it_left_its_base(provider, repo):
    """Not against the base's tip: what other people pushed since is not under review."""
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry", base="main"))
    assert sorted(f.path for f in payload.files) == ["README.md", "queue.py", "retry.py"]
    assert "unrelated.py" not in [f.path for f in payload.files]


async def test_each_kind_of_change_is_named(provider, repo):
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry", base="main"))
    kinds = {f.path: f.change_type.value for f in payload.files}
    assert kinds == {"retry.py": "added", "queue.py": "modified", "README.md": "deleted"}


async def test_the_diff_is_the_hunks_without_gits_header(provider, repo):
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry", base="main"))
    body = next(f for f in payload.files if f.path == "queue.py").hunks[0]["diff"]
    assert body.startswith("@@"), body
    assert "+    retry()" in body


async def test_it_carries_the_shas_to_diff_any_pair(provider, repo):
    """The same endpoints a forge gives, so the agent can compute ranges itself from the worktree."""
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry", base="main"))
    refs = payload.mr.diff_refs
    assert refs["head_sha"] == git(repo, "rev-parse", "feat/retry")
    assert refs["base_sha"] == git(repo, "merge-base", "main", "feat/retry")
    assert refs["base_sha"] != git(repo, "rev-parse", "main"), "the base moved; the fork point did not"


async def test_it_points_at_the_working_repository_not_a_copy(provider, repo):
    """The agent has to edit the code it is being asked about, so there is nothing to materialize."""
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry", base="main"))
    assert payload.clone_url == str(repo) and payload.mr.clone_url == str(repo)


async def test_it_advertises_what_a_branch_can_and_cannot_do(provider, repo):
    """The review channel, the approval bar and the discussion list turn themselves off on these."""
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry", base="main"))
    caps = payload.mr.capabilities
    assert caps["threads"] is False and caps["approvals"] is False
    assert caps["commits"] is True, "a branch has its own commits, and they step the same way"
    assert caps["diff_versions"] is False, "git keeps no record of what you last read"
    assert payload.threads == []


async def test_the_branch_and_its_base_are_reported_as_they_are(provider, repo):
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry", base="main"))
    assert payload.mr.source_branch == "feat/retry" and payload.mr.target_branch == "main"
    assert payload.mr.title == "add a retry to the drain"
    assert payload.mr.author == "the agent"


async def test_the_base_defaults_to_the_one_the_repository_names(provider, repo):
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry"))
    assert payload.mr.target_branch == "main"


async def test_a_repository_that_cannot_name_a_base_says_so(provider, tmp_path):
    """Reviewing against the wrong base produces a diff full of other people's work, so it refuses
    rather than guessing."""
    path = tmp_path / "odd"
    path.mkdir()
    git(path, "init", "-b", "trunk")
    (path / "a").write_text("x")
    git(path, "add", ".")
    git(path, "commit", "-m", "one")
    with pytest.raises(GitError, match="name a base"):
        await provider.load(LocalRef(path=str(path), branch="trunk"))


async def test_a_path_that_is_not_a_repository_says_so(provider, tmp_path):
    with pytest.raises(GitError, match="not a git repository"):
        await provider.load(LocalRef(path=str(tmp_path), branch="main"))


async def test_a_branch_that_is_not_there_says_what_git_said(provider, repo):
    with pytest.raises(GitError):
        await provider.load(LocalRef(path=str(repo), branch="feat/nope", base="main"))


async def test_a_rename_keeps_both_paths(provider, repo):
    git(repo, "mv", "retry.py", "backoff.py")
    git(repo, "commit", "-m", "rename it")
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry", base="main"))
    renamed = [f for f in payload.files if f.change_type.value == "added" and f.path == "backoff.py"]
    assert renamed or any(f.old_path == "retry.py" for f in payload.files)


# --- a session over a borrowed repository --------------------------------------

async def test_a_local_session_borrows_the_repository_and_never_copies_it(tmp_path, repo):
    """The review must point at the code the agent is editing, and must not take it away on close."""
    from review_mate.session.manager import SessionManager
    from review_mate.workspace.manager import WorkspaceManager

    workspace = WorkspaceManager(root=tmp_path / "home")
    manager = SessionManager(root=tmp_path / "sessions", local_source=LocalBranchProvider(),
                             workspace=workspace)
    sid = await manager.create(ref=LocalRef(path=str(repo), branch="feat/retry", base="main"))
    assert manager.get(sid).snapshot().checkout_path == str(repo)
    mirrors = tmp_path / "home" / "mirrors"
    assert not mirrors.exists() or list(mirrors.iterdir()) == [], "nothing was cloned"

    await manager.end(sid)
    assert (repo / "queue.py").exists(), "closing the review must not touch the reviewer's repo"
    await manager.shutdown()


async def test_the_forge_is_not_asked_about_a_branch_it_has_never_seen(tmp_path, repo):
    """A topic holds one provider for every session. Sending a local directory name to a remote API
    gets the reviewer an error where the honest answer is "this host knows nothing about that"."""
    from review_mate.session.manager import SessionManager
    from review_mate.view.annotations import AnnotationsTopic

    class Forge:
        host = "gitlab"
        asked = 0

        async def blame(self, *args, **kwargs):
            Forge.asked += 1
            raise AssertionError("the forge was asked about a local branch")

    manager = SessionManager(root=tmp_path / "sessions", local_source=LocalBranchProvider())
    sid = await manager.create(ref=LocalRef(path=str(repo), branch="feat/retry", base="main"))
    writer = manager.get(sid)
    from review_mate.session.commands import AddHighlight
    from review_mate.session.state import LineRange, Origin, Side
    await writer.submit(AddHighlight(file="queue.py", side=Side.NEW,
                                    line_range=LineRange(start=1, end=1)), Origin.BROWSER)

    view = await AnnotationsTopic(manager, provider=Forge()).build(sid)
    assert view["highlights"][0]["context"]["state"] == "unavailable"
    assert Forge.asked == 0
    await manager.shutdown()


# --- the agent opening the review ---------------------------------------------

@pytest.fixture
async def agent_on(tmp_path, repo):
    from review_mate.mcp.bridge import AgentBridge
    from review_mate.session.manager import SessionManager
    manager = SessionManager(root=tmp_path / "sessions", local_source=LocalBranchProvider())
    yield manager, AgentBridge(manager, base_url="http://127.0.0.1:9999"), repo
    await manager.shutdown()


async def test_the_agent_opens_the_review_and_gets_somewhere_to_send_them(agent_on):
    """A session id is homework; a link is somewhere to look."""
    manager, bridge, repo = agent_on
    opened = await bridge.open_local_review(str(repo), "feat/retry", "main")
    assert opened["url"] == f"http://127.0.0.1:9999/?s={opened['session_id']}"
    assert opened["branch"] == "feat/retry" and opened["base"] == "main"
    assert opened["files"] == 3
    assert manager.get(opened["session_id"]) is not None


async def test_the_review_it_opened_is_of_the_branch(agent_on):
    manager, bridge, repo = agent_on
    opened = await bridge.open_local_review(str(repo), "feat/retry", "main")
    built = await bridge.view(opened["session_id"]) if bridge._view else None
    snapshot = manager.get(opened["session_id"]).snapshot()
    assert snapshot.mr.source_branch == "feat/retry"
    assert sorted(f.path for f in snapshot.files) == ["README.md", "queue.py", "retry.py"]
    assert snapshot.checkout_path == str(repo), "it edits the code it is asked about"
    assert built is None or built["state"] == "ready"


async def test_a_branch_that_will_not_load_fails_loudly(agent_on):
    manager, bridge, repo = agent_on
    with pytest.raises(Exception):
        await bridge.open_local_review(str(repo), "feat/nope", "main")
    assert manager.list() == [], "a session that could not load must not be left behind"


# --- stepping through the branch one commit at a time --------------------------

@pytest.fixture
def stacked(tmp_path):
    """A branch of three commits, one of which adds a file — and a base that moved after."""
    path = tmp_path / "stack"
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "a.py").write_text("one\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "base")

    git(path, "checkout", "-b", "feat/three")
    for n, (name, body, message) in enumerate((
            ("a.py", "one\ntwo\n", "add two"),
            ("b.py", "new file\n", "add b"),
            ("a.py", "one\ntwo\nthree\n", "add three"))):
        (path / name).write_text(body)
        git(path, "add", ".")
        git(path, "commit", "-m", message)

    git(path, "checkout", "main")
    (path / "elsewhere.py").write_text("x\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "someone else")
    git(path, "checkout", "feat/three")
    return path


async def test_it_lists_the_commits_the_branch_added(provider, stacked):
    """`base..branch`, so what the base did afterwards is not listed as the author's work."""
    rows = await provider.commits(LocalRef(path=str(stacked), branch="feat/three", base="main"))
    assert [r["title"] for r in rows] == ["add three", "add b", "add two"]
    assert "someone else" not in [r["title"] for r in rows]


async def test_a_commit_row_carries_what_a_forge_would_give(provider, stacked):
    rows = await provider.commits(LocalRef(path=str(stacked), branch="feat/three", base="main"))
    row = rows[-1]
    assert row["sha"].startswith(row["short_id"])
    assert row["title"] == "add two" and row["author"] == "the agent"
    assert row["created_at"], "a forge gives a timestamp, and so does this"


async def test_a_message_with_newlines_stays_one_commit(provider, stacked):
    """Fields are separated by git's own unit marks, so prose in a subject cannot split a row."""
    (stacked / "a.py").write_text("one\ntwo\nthree\nfour\n")
    git(stacked, "add", ".")
    git(stacked, "commit", "-m", "add four\n\nwhy: because the retry needed a bound\nand a note")
    rows = await provider.commits(LocalRef(path=str(stacked), branch="feat/three", base="main"))
    assert len(rows) == 4
    assert rows[0]["title"] == "add four"
    assert "because the retry needed a bound" in rows[0]["message"]


async def test_one_commit_reads_as_its_own_change(provider, stacked):
    rows = await provider.commits(LocalRef(path=str(stacked), branch="feat/three", base="main"))
    adding_b = next(r for r in rows if r["title"] == "add b")
    files = await provider.commit_diff(
        LocalRef(path=str(stacked), branch="feat/three", base="main"), adding_b["sha"])
    assert [f.path for f in files] == ["b.py"]
    assert files[0].change_type.value == "added"
    assert "+new file" in files[0].hunks[0]["diff"]


async def test_the_first_commit_of_a_history_is_readable(provider, tmp_path):
    """A root commit has no parent. It is also exactly the one a reviewer opens first."""
    path = tmp_path / "fresh"
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "only.py").write_text("hello\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "the first thing")
    sha = git(path, "rev-parse", "HEAD")
    files = await provider.commit_diff(LocalRef(path=str(path), branch="main", base="main"), sha)
    assert [f.path for f in files] == ["only.py"]
    assert "+hello" in files[0].hunks[0]["diff"]


async def test_the_commit_list_reaches_the_topic_that_publishes_it(tmp_path, stacked):
    """Through `ref_of`, which is what lets a topic address a session it did not open."""
    from review_mate.session.manager import SessionManager
    from review_mate.view.browse import BrowseTopics

    local = LocalBranchProvider()
    manager = SessionManager(root=tmp_path / "sessions", local_source=local)
    sid = await manager.create(ref=LocalRef(path=str(stacked), branch="feat/three", base="main"))
    topics = BrowseTopics(manager, provider=local)
    assert (await topics.build_commits(sid))["state"] == "idle"
    await topics.fetch_commits(sid)
    view = await topics.build_commits(sid)
    assert view["state"] == "ready"
    assert [c["title"] for c in view["commits"]] == ["add three", "add b", "add two"]
    await manager.shutdown()


async def test_a_commit_mode_resolves_for_a_local_branch(tmp_path, stacked):
    """The whole path: the topic picks the mode, `ref_of` addresses the session, git answers."""
    from review_mate.session.manager import SessionManager
    from review_mate.view.difftopic import DiffTopics

    local = LocalBranchProvider()
    manager = SessionManager(root=tmp_path / "sessions", local_source=local)
    sid = await manager.create(ref=LocalRef(path=str(stacked), branch="feat/three", base="main"))
    rows = await local.commits(LocalRef(path=str(stacked), branch="feat/three", base="main"))
    adding_b = next(r for r in rows if r["title"] == "add b")

    topics = DiffTopics(manager, provider=local)
    topic = f"{sid}:commit@{adding_b['sha']}"
    assert (await topics.build(topic))["state"] == "loading"
    for _ in range(100):
        view = await topics.build(topic)
        if view["state"] != "loading":
            break
        await asyncio.sleep(0.02)
    assert view["state"] == "ready", view
    assert [f["path"] for f in view["files"]] == ["b.py"]
    await topics.aclose()
    await manager.shutdown()


# --- how a branch is named, and what it is not ---------------------------------

async def test_a_branch_is_named_by_where_it_is_going(provider, repo):
    """`LocalRef` keeps a fake merge-request number out of the model; the label keeps it off screen.
    A client that built `project!iid` itself would put it straight back."""
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry", base="main"))
    assert payload.mr.label == "feat/retry → main"
    assert "!0" not in payload.mr.label


async def test_a_merge_request_is_still_named_by_its_number(provider, repo):
    from review_mate.session.state import MRMetadata
    mr = MRMetadata(host="gitlab", project="g/p", iid=137, title="t", source_branch="x",
                    target_branch="main", sha="a", author="d", url="u")
    assert mr.label == "g/p !137"


async def test_the_label_survives_the_log(provider, repo):
    """It is on every published copy of the metadata, so it has to replay like the rest of it."""
    from review_mate.session.state import MRMetadata
    payload = await provider.load(LocalRef(path=str(repo), branch="feat/retry", base="main"))
    replayed = MRMetadata.model_validate(payload.mr.model_dump(mode="json"))
    assert replayed.label == "feat/retry → main"


async def test_a_branch_is_never_behind_a_watermark_it_cannot_have(tmp_path, repo):
    """`since` needs a forge's versions, so there is no record of what was read last — and keying
    one on a branch would collide across repositories that happen to share a name."""
    from review_mate.kb.store import ReviewKB
    from review_mate.session.manager import SessionManager
    from review_mate.view.hub import HubTopic

    kb = ReviewKB(root=tmp_path / "home")
    kb.set_watermark("local", repo.name, 0, "some-other-sha")   # a collision waiting to happen
    manager = SessionManager(root=tmp_path / "sessions", local_source=LocalBranchProvider())
    sid = await manager.create(ref=LocalRef(path=str(repo), branch="feat/retry", base="main"))

    row = next(s for s in (await HubTopic(manager, kb=kb).build())["sessions"] if s["id"] == sid)
    assert row["behind"] is False
    assert row["mr"]["label"] == "feat/retry → main"
    await manager.shutdown()


# --- the loop: the agent changes the code, and the review follows ---------------

async def test_resyncing_a_branch_picks_up_what_the_agent_just_committed(tmp_path, repo):
    """The hot path of reviewing your own work. Without it the reviewer reads a diff frozen at the
    moment the session opened, and every fix the agent makes is invisible to them."""
    from review_mate.session.manager import SessionManager
    from review_mate.writeback.threads import ThreadVerbs

    local = LocalBranchProvider()
    manager = SessionManager(root=tmp_path / "sessions", local_source=local)
    sid = await manager.create(ref=LocalRef(path=str(repo), branch="feat/retry", base="main"))
    opened = manager.get(sid).snapshot()
    assert "bound.py" not in [f.path for f in opened.files]

    (repo / "bound.py").write_text("LIMIT = 5\n")      # the agent answers a comment
    git(repo, "add", ".")
    git(repo, "commit", "-m", "bound the retry")
    landed = git(repo, "rev-parse", "HEAD")

    verbs = ThreadVerbs(manager, writeback=None)
    answer = await verbs.resync(sid)
    assert answer["ok"] and answer["head"] == landed

    after = manager.get(sid).snapshot()
    assert after.mr.sha == landed
    assert "bound.py" in [f.path for f in after.files], "the reviewer sees what was just written"
    await manager.shutdown()


async def test_a_branch_has_no_merge_request_to_write_a_reply_to(tmp_path, repo):
    """Reaching for the writer anyway would send a repository path to a forge and report back
    whatever it made of it."""
    from review_mate.session.manager import SessionManager
    from review_mate.writeback.threads import ThreadVerbs

    manager = SessionManager(root=tmp_path / "sessions", local_source=LocalBranchProvider())
    sid = await manager.create(ref=LocalRef(path=str(repo), branch="feat/retry", base="main"))

    class Writer:
        called = False

        async def reply(self, *args, **kwargs):
            Writer.called = True

    answer = await ThreadVerbs(manager, writeback=Writer()).reply(sid, "t1", "hello")
    assert answer == {"error": "this review has no merge request to write to"}
    assert Writer.called is False
    await manager.shutdown()
