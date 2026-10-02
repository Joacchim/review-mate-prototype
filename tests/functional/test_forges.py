"""Two forges configured at once, and which one answers.

`host` is the network host a reference names — `gitlab.com`, `github.com`, a self-hosted
`gitlab.internal` — not the kind of forge it is. So this is also what keeps two instances of the
*same* forge apart, which routing by kind could not.
"""
import pytest

from review_mate.contracts import LocalRef, MRPayload, MRRef
from review_mate.forges import Forges
from review_mate.session.manager import SessionManager
from review_mate.session.state import MRMetadata


def _mr(host, project="g/p", iid=1):
    return MRMetadata(host=host, project=project, iid=iid, title="T", source_branch="x",
                      target_branch="main", sha="abc", author="a", url="u")


class Stub:
    def __init__(self, host):
        self.host = host
        self.asked = []
        self.read = []
        self.queue = [{"project": f"{host}/q", "iid": 1, "title": "t", "url": "u"}]
        self.hits = [{"project": f"{host}/s", "iid": 2, "title": "t", "url": "u"}]
        self.repo = {"clone_url": f"https://{host}/r.git"} if host == "second.example" else None

    async def load(self, ref):
        self.asked.append(ref)
        return MRPayload(mr=_mr(self.host, ref.project, ref.iid), files=[], threads=[])

    async def fetch_threads(self, ref):
        return []

    async def review_queue_items(self):
        return list(self.queue)

    async def search(self, query, limit=15):
        return list(self.hits)

    async def locate_repo(self, name):
        return self.repo

    async def get_file(self, project, path, ref):
        self.read.append((project, path, ref))
        return f"content from {self.host}\n"


def test_each_forge_answers_for_its_own_host():
    first, second = Stub("first.example"), Stub("second.example")
    forges = Forges({"first.example": first, "second.example": second})
    assert forges.pick("first.example") is first
    assert forges.pick("second.example") is second


def test_a_forge_does_not_answer_for_a_host_it_does_not_serve():
    """The guarantee a local branch depends on: `host` is `local`, and a GitLab server that happens
    to be configured must not supply its diff versions or its discussions."""
    forges = Forges({"first.example": Stub("first.example")})
    assert forges.pick("local") is None
    assert forges.pick("second.example") is None


def test_a_forge_that_names_no_host_answers_for_anything():
    """How a stub gets injected in a test without inventing a hostname for it."""
    anon = Stub("")
    assert Forges.of(anon).pick("whatever.example") is anon


def test_asking_everywhere_the_reviewer_works_merges_the_forges():
    """A queue, a search and a repository lookup are not about one change, so every forge is asked."""
    forges = Forges({"first.example": Stub("first.example"), "second.example": Stub("second.example")})
    import asyncio
    queue = asyncio.run(forges.review_queue_items())
    found = asyncio.run(forges.search("anything"))
    repo = asyncio.run(forges.locate_repo("r"))
    assert sorted(i["project"] for i in queue) == ["first.example/q", "second.example/q"]
    assert sorted(i["project"] for i in found) == ["first.example/s", "second.example/s"]
    assert repo == {"clone_url": "https://second.example/r.git"}   # the first forge that has it


async def test_a_session_loads_from_the_forge_its_reference_names(tmp_path):
    first, second = Stub("first.example"), Stub("second.example")
    manager = SessionManager(root=tmp_path / "s",
                             mr_source=Forges({"first.example": first, "second.example": second}))
    sid = await manager.create()
    await manager.load(sid, MRRef(host="second.example", project="g/p", iid=7))
    assert [r.iid for r in second.asked] == [7] and first.asked == []
    assert manager.get(sid).snapshot().mr.host == "second.example"
    await manager.shutdown()


async def test_a_branch_on_this_machine_still_bypasses_every_forge(tmp_path):
    manager = SessionManager(root=tmp_path / "s",
                             mr_source=Forges({"first.example": Stub("first.example")}))
    assert manager.source_for(LocalRef(path="/tmp", branch="x", base="main")) is None  # none wired
    assert manager.source_for(MRRef(host="first.example", project="g/p", iid=1)) is not None
    await manager.shutdown()


# --- two forges, each with its own kind of reference --------------------------------------------

def test_the_two_forges_shorthands_cannot_claim_each_other(monkeypatch, tmp_path):
    """A reference needs no forge named alongside it: `!` is how GitLab's users write one and `#`
    is how GitHub's do, and a URL carries the forge in its own path."""
    from review_mate.host.config import build_provider_from_env

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))      # no glab/gh config to fall back on
    monkeypatch.setenv("REVIEW_MATE_GITLAB_URL", "https://gitlab.example/api/v4")
    monkeypatch.setenv("REVIEW_MATE_GITLAB_TOKEN", "gl")
    monkeypatch.setenv("REVIEW_MATE_GITLAB_USER", "me")
    monkeypatch.setenv("REVIEW_MATE_GITHUB_TOKEN", "gh")
    monkeypatch.setenv("REVIEW_MATE_GITHUB_USER", "me")

    forges, resolve = build_provider_from_env()
    assert sorted(h for h in ["gitlab.example", "github.com"]
                  if forges.pick(h) is not None) == ["github.com", "gitlab.example"]

    assert resolve("g/p!3").host == "gitlab.example"
    assert resolve("o/r#7").host == "github.com"
    assert resolve("https://gitlab.example/g/p/-/merge_requests/3").host == "gitlab.example"
    assert resolve("https://github.com/o/r/pull/7").host == "github.com"
    assert resolve("neither of these") is None


def test_each_forge_is_asked_to_write_back_its_own_reviews(monkeypatch, tmp_path):
    from review_mate.host.config import build_writer_from_env

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_MATE_GITLAB_URL", "https://gitlab.example/api/v4")
    monkeypatch.setenv("REVIEW_MATE_GITLAB_TOKEN", "gl")
    monkeypatch.setenv("REVIEW_MATE_GITLAB_USER", "me")
    monkeypatch.setenv("REVIEW_MATE_GITHUB_TOKEN", "gh")
    monkeypatch.setenv("REVIEW_MATE_GITHUB_USER", "me")

    writers = build_writer_from_env()
    assert type(writers.pick("gitlab.example")).__name__ == "GitLabWriter"
    assert type(writers.pick("github.com")).__name__ == "GitHubWriter"
    assert writers.pick("local") is None       # a branch on this machine is written back to nothing


async def test_a_view_asks_the_forge_that_loaded_the_review(tmp_path):
    """The routing has to reach the view topics, not just the load.

    They are handed one `provider` at construction and serve every session, so a server with two
    forges configured would otherwise read a file from whichever one happened to be passed. The
    forge is resolved from the review instead, each time it is needed.
    """
    from review_mate.view.difftopic import BlobTopics

    first, second = Stub("first.example"), Stub("second.example")
    forges = Forges({"first.example": first, "second.example": second})
    manager = SessionManager(root=tmp_path / "s", mr_source=forges)
    sid = await manager.create()
    await manager.load(sid, MRRef(host="second.example", project="g/p", iid=7))

    blobs = BlobTopics(manager, provider=forges)
    view = await blobs.build(f"{sid}:full:a.py")
    assert view["state"] == "loading"                 # a host read, started off the build
    for task in list(blobs._tasks.values()):
        await task

    assert [p for p, _, _ in second.read] == ["g/p"], "the forge that loaded it was not asked"
    assert first.read == [], "a forge that never saw this review was asked for its files"
    view = await blobs.build(f"{sid}:full:a.py")
    assert view["state"] == "ready"
    assert "content from second.example" in "".join(l["text"] for l in view["lines"])
    await manager.shutdown()
