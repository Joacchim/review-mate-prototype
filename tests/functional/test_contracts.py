"""The host contract: a fake MRSource loaded through the manager populates session state.

Exercises the contract Protocol and the SYSTEM-origin apply path (ApplyMRMetadata/ApplyFiles).
"""
import pytest

from review_mate.contracts import MRSource, MRPayload, MRRef
from review_mate.session.manager import SessionManager
from review_mate.session.state import MRMetadata, FileEntry, ChangeType


class FakeMRSource:
    """Implements the MRSource Protocol structurally."""
    async def load(self, ref: MRRef) -> MRPayload:
        return MRPayload(
            mr=MRMetadata(host="gitlab", project=ref.project, iid=ref.iid, title="T",
                          source_branch="x", target_branch="main", sha="abc",
                          author="dev", url="http://x"),
            files=[FileEntry(path="a.py", change_type=ChangeType.MODIFIED)],
            threads=[],
        )

    async def fetch_threads(self, ref: MRRef):
        return []


def test_fake_satisfies_protocol():
    assert isinstance(FakeMRSource(), MRSource)


@pytest.fixture
async def manager(tmp_path):
    m = SessionManager(root=tmp_path / "sessions", mr_source=FakeMRSource())
    yield m
    await m.shutdown()


async def test_load_populates_session_via_seam(manager):
    sid = await manager.create()
    ref = MRRef(host="gitlab", project="g/p", iid=42)
    await manager.load(sid, ref)
    snap = manager.get(sid).snapshot()
    assert snap.mr is not None and snap.mr.iid == 42
    assert [f.path for f in snap.files] == ["a.py"]


# --- what every host must agree on -------------------------------------------------------------

async def test_every_host_lists_commits_oldest_first(tmp_path):
    """One order, because the client counts positions in it.

    The picker marks a commit reviewed when it sits at or before the reviewed watermark, which is
    a statement about position — so a host that hands the list back the other way round does not
    merely read oddly, it ticks the newest commits as the ones already read.

    Each adapter was tested against its own fixture and all three passed while two disagreed:
    GitLab serves newest-first and is reversed, GitHub serves oldest-first and was reversed too,
    and a local branch came straight from `git log`, which is newest-first. Nothing compared them.
    """
    import subprocess
    from review_mate.contracts import LocalRef
    from review_mate.host.github import GitHubProvider
    from review_mate.host.gitlab import GitLabProvider
    from review_mate.host.local import LocalBranchProvider
    import httpx

    def reply(payload):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
                                 base_url="https://host")

    # each forge in its own order, which is the point: the adapters must level them
    gitlab = GitLabProvider(base_url="https://host", token="t", username="me",
                            client=reply([{"id": "b", "short_id": "b", "title": "second",
                                           "created_at": "2026-01-02T00:00:00Z"},
                                          {"id": "a", "short_id": "a", "title": "first",
                                           "created_at": "2026-01-01T00:00:00Z"}]))
    github = GitHubProvider(base_url="https://host", token="t", username="me",
                            client=reply([{"sha": "a", "commit": {"message": "first",
                                                                  "author": {"date": "2026-01-01T00:00:00Z"}}},
                                          {"sha": "b", "commit": {"message": "second",
                                                                  "author": {"date": "2026-01-02T00:00:00Z"}}}]))
    src = tmp_path / "branch"
    src.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ.get("PATH", ""),
           "HOME": str(tmp_path)}
    run = lambda *a: subprocess.run(["git", *a], cwd=src, check=True, capture_output=True, env=env)
    run("init", "-b", "main")
    (src / "f").write_text("0\n"); run("add", "."); run("commit", "-m", "base")
    run("checkout", "-b", "work")
    for n in ("first", "second"):
        (src / "f").write_text(n + "\n"); run("commit", "-am", n)

    ref = MRRef(host="h", project="g/p", iid=1)
    listings = {
        "gitlab": [c["title"] for c in await gitlab.commits(ref)],
        "github": [c["title"] for c in await github.commits(ref)],
        "local": [c["title"] for c in await LocalBranchProvider().commits(
            LocalRef(path=str(src), branch="work", base="main"))],
    }
    for host, titles in listings.items():
        assert titles == ["first", "second"], f"{host} hands them back {titles}"
