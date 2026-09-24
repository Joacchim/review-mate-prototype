"""Reviewing a branch that is still on this machine, before anyone else is asked to look at it.

Against a real repository, because the whole provider is git: a stub would only prove that the
methods I wrote call the methods I wrote. What matters is that a branch produces the same diff a
merge request would — from where it left its base, not from wherever the base has got to since.
"""
import os
import subprocess

import pytest

from review_mate.host.local import GitError, LocalBranchProvider
from review_mate.seams import LocalRef

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
    assert caps["commits"] is False, "nothing routes a per-commit read to git yet"
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
    """A scope holds one provider for every session. Sending a local directory name to a remote API
    gets the reviewer an error where the honest answer is "this host knows nothing about that"."""
    from review_mate.session.manager import SessionManager
    from review_mate.view.rail import RailScope

    class Forge:
        host = "gitlab"
        asked = 0

        async def blame(self, *args, **kwargs):
            Forge.asked += 1
            raise AssertionError("the forge was asked about a local branch")

    manager = SessionManager(root=tmp_path / "sessions", local_source=LocalBranchProvider())
    sid = await manager.create(ref=LocalRef(path=str(repo), branch="feat/retry", base="main"))
    actor = manager.get(sid)
    from review_mate.session.commands import AddHighlight
    from review_mate.session.state import LineRange, Origin, Side
    await actor.submit(AddHighlight(file="queue.py", side=Side.NEW,
                                    line_range=LineRange(start=1, end=1)), Origin.BROWSER)

    view = await RailScope(manager, provider=Forge()).build(sid)
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
    assert opened["url"] == f"http://127.0.0.1:9999/?session={opened['session_id']}"
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
