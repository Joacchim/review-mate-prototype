"""The GitHub adapter against a mocked API — REST for most of it, GraphQL for the rest.

What this is really checking is that a second forge produces the *same* host-neutral model the
first one does, because everything above the adapter is written against that model and nothing
else. Where GitHub differs it differs in capabilities, not in shape.
"""
import json

import httpx
import pytest

from review_mate.contracts import MRRef
from review_mate.host.base import CapabilityError, HostWriter
from review_mate.host.github import (
    GITHUB_CAPABILITIES, GitHubProvider, GitHubWriter, parse_github_reference,
)
from review_mate.session.state import ChangeType

PR = {"title": "Rework the retry backoff", "html_url": "https://github.com/o/r/pull/7",
      "user": {"login": "dev"}, "draft": False, "state": "open", "merged_at": None,
      "head": {"sha": "head1", "ref": "fix/backoff"},
      "base": {"sha": "base1", "ref": "main",
               "repo": {"clone_url": "https://github.com/o/r.git",
                        "ssh_url": "git@github.com:o/r.git", "full_name": "o/r",
                        "default_branch": "main"}}}
FILES = [{"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new"},
         {"filename": "new.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+hello"},
         {"filename": "now.py", "previous_filename": "was.py", "status": "renamed", "patch": ""},
         {"filename": "gone.py", "status": "removed", "patch": "@@ -1 +0,0 @@\n-bye"}]
COMMITS = [{"sha": "c1", "commit": {"message": "first\n\nbody",
                                    "author": {"name": "dev", "date": "2026-01-01T00:00:00Z"}}},
           {"sha": "c2", "commit": {"message": "second",
                                    "author": {"name": "dev", "date": "2026-01-02T00:00:00Z"}}}]
REVIEWS = [{"user": {"login": "me"}, "state": "APPROVED"},
           {"user": {"login": "other"}, "state": "CHANGES_REQUESTED"},
           {"user": {"login": "me"}, "state": "COMMENTED"}]
THREADS = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
    {"id": "THREAD_1", "isResolved": False, "path": "a.py", "line": 12,
     "comments": {"nodes": [{"databaseId": 901, "author": {"login": "paul"},
                             "body": "why here?", "createdAt": "2026-01-03T00:00:00Z"}]}},
    {"id": "THREAD_2", "isResolved": True, "path": "a.py", "line": 30,
     "comments": {"nodes": [{"databaseId": 902, "author": {"login": "sam"},
                             "body": "settled", "createdAt": "2026-01-04T00:00:00Z"}]}},
    {"id": "THREAD_EMPTY", "isResolved": False, "path": "a.py", "line": 1,
     "comments": {"nodes": []}},
]}}}}}

sent: list = []


def _handler(request: httpx.Request) -> httpx.Response:
    p, body = request.url.path, {}
    if request.content:
        body = json.loads(request.content)
    sent.append((request.method, p, dict(request.url.params), body))
    if p.endswith("/graphql"):
        query = body.get("query", "")
        if "reviewThreads" in query:
            return httpx.Response(200, json=THREADS)
        if "resolveReviewThread" in query or "addPullRequestReviewThreadReply" in query:
            return httpx.Response(200, json={"data": {"ok": True}})
        return httpx.Response(200, json={"data": {}})
    if p.endswith("/pulls/7/files"):
        return httpx.Response(200, json=FILES if request.url.params.get("page", "1") == "1" else [])
    if p.endswith("/pulls/7/commits"):
        return httpx.Response(200, json=COMMITS if request.url.params.get("page", "1") == "1" else [])
    if p.endswith("/pulls/7/reviews"):
        return httpx.Response(200, json=REVIEWS if request.url.params.get("page", "1") == "1" else [])
    if p.endswith("/pulls/7"):
        return httpx.Response(200, json=PR)
    if "/commits/c1" in p:
        return httpx.Response(200, json={"files": FILES[:1]})
    if "/contents/" in p:
        return httpx.Response(200, text="the whole file\n")
    if p.endswith("/search/issues"):
        return httpx.Response(200, json={"items": [
            {"html_url": "https://github.com/o/r/pull/7", "title": "Rework the retry backoff"}]})
    if request.method in ("POST", "PATCH", "DELETE") and "/repos/" in p:
        return httpx.Response(200, json={"id": 1})
    if p == "/repos/o/other":
        return httpx.Response(200, json={"full_name": "o/other", "default_branch": "trunk",
                                         "clone_url": "https://github.com/o/other.git",
                                         "ssh_url": "git@github.com:o/other.git"})
    return httpx.Response(404, json={"message": "no"})


@pytest.fixture
def provider():
    sent.clear()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler),
                               base_url="https://api.github.com")
    return GitHubProvider(base_url="https://api.github.com", token="t", username="me",
                          host="github.com", client=client)


@pytest.fixture
def writer():
    sent.clear()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler),
                               base_url="https://api.github.com")
    return GitHubWriter(base_url="https://api.github.com", token="t", host="github.com",
                        client=client)


REF = MRRef(host="github.com", project="o/r", iid=7)


# --- a reference, however it is written -------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("https://github.com/o/r/pull/7", ("github.com", "o/r", 7)),
    ("https://github.com/my-org/my.repo/pull/12", ("github.com", "my-org/my.repo", 12)),
    ("github.com/o/r/pull/7", None),       # no scheme: the GitLab parser declines these too
    ("o/r#7", ("github.com", "o/r", 7)),
    ("https://gitlab.com/g/p/-/merge_requests/3", None),   # the other forge's URL
    ("g/p!3", None),                                       # and the other forge's shorthand
    ("nonsense", None),
])
def test_a_pull_request_reference_is_recognised_without_being_told_the_forge(text, expected):
    ref = parse_github_reference(text, "github.com")
    assert (None if ref is None else (ref.host, ref.project, ref.iid)) == expected


# --- the change -------------------------------------------------------------------------------

async def test_load_maps_a_pull_request_onto_the_host_neutral_model(provider):
    payload = await provider.load(REF)
    mr = payload.mr
    assert (mr.host, mr.project, mr.iid) == ("github.com", "o/r", 7)
    assert mr.title == "Rework the retry backoff" and mr.author == "dev"
    assert (mr.source_branch, mr.target_branch, mr.sha) == ("fix/backoff", "main", "head1")
    assert mr.clone_url == "https://github.com/o/r.git"
    assert mr.diff_refs == {"base_sha": "base1", "head_sha": "head1", "start_sha": "base1"}
    assert [(f.path, f.change_type) for f in payload.files] == [
        ("a.py", ChangeType.MODIFIED), ("new.py", ChangeType.ADDED),
        ("now.py", ChangeType.RENAMED), ("gone.py", ChangeType.DELETED)]
    assert payload.files[2].old_path == "was.py"
    # GitHub's `patch` is already the hunks without a header — the shape the review model holds
    assert payload.files[0].hunks == [{"diff": "@@ -1 +1 @@\n-old\n+new"}]


async def test_it_says_it_cannot_version_a_diff(provider):
    assert provider.capabilities()["diff_versions"] is False
    assert not hasattr(provider, "mr_versions"), (
        "the absent method is the other half of the capability — a guard upstream asks for both")


async def test_commits_come_back_newest_first(provider):
    rows = await provider.commits(REF)
    assert [c["sha"] for c in rows] == ["c2", "c1"]
    assert rows[1]["title"] == "first" and rows[1]["message"].startswith("first\n\nbody")


async def test_one_commit_diffs_like_the_whole_change(provider):
    files = await provider.commit_diff(REF, "c1")
    assert [f.path for f in files] == ["a.py"]


async def test_a_file_is_read_raw(provider):
    assert await provider.get_file("o/r", "a.py", "head1") == "the whole file\n"
    accept = [m for m in sent if "/contents/" in m[1]]
    assert accept, sent


async def test_approvals_take_each_reviewer_s_last_word(provider):
    """An approval followed by a request for changes is not an approval; a later plain comment
    leaves it standing."""
    got = await provider.approvals(REF)
    assert got["approved"] is True and got["user_has_approved"] is True
    assert [a["username"] for a in got["approved_by"]] == ["me"]


# --- discussions ------------------------------------------------------------------------------

async def test_threads_carry_their_resolution_and_their_own_id(provider):
    threads = await provider.fetch_threads(REF)
    assert [t.id for t in threads] == ["THREAD_1", "THREAD_2"]   # the empty one is not a discussion
    assert [t.resolved for t in threads] == [False, True]
    assert threads[0].anchor == {"file": "a.py", "line": 12}
    assert [(c.id, c.author, c.body) for c in threads[0].comments] == [("901", "paul", "why here?")]


async def test_discussions_degrade_rather_than_failing_the_load(provider):
    """A forge that will not answer costs the reviewer its discussions, not the whole review."""
    async def refuse(request):
        return httpx.Response(500, json={"message": "boom"})
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(refuse),
                                         base_url="https://api.github.com")
    assert await provider.fetch_threads(REF) == []


# --- everywhere the reviewer works -------------------------------------------------------------

async def test_the_queue_and_search_speak_in_references(provider):
    queue = await provider.review_queue_items()
    assert [(i["host"], i["project"], i["iid"]) for i in queue] == [("github.com", "o/r", 7)]
    found = await provider.search("backoff")
    assert [(i["project"], i["iid"]) for i in found] == [("o/r", 7)]


async def test_a_repository_the_agent_asked_for_is_located(provider):
    assert await provider.locate_repo("o/other") == {
        "host": "github.com", "project": "o/other",
        "clone_url": "https://github.com/o/other.git", "default_branch": "trunk"}
    assert await provider.locate_repo("o/absent") is None


# --- writing back -------------------------------------------------------------------------------

def test_the_writer_satisfies_the_contract(writer):
    assert isinstance(writer, HostWriter)


async def test_an_inline_comment_lands_on_the_head_the_lines_belong_to(writer):
    await writer.post_comment(REF, {"new_path": "a.py", "new_line": 12, "head_sha": "head1",
                                    "sha": "fallback"}, "this needs a guard")
    method, path, _, body = sent[-1]
    assert (method, path) == ("POST", "/repos/o/r/pulls/7/comments")
    assert body == {"body": "this needs a guard", "path": "a.py", "line": 12,
                    "side": "RIGHT", "commit_id": "head1"}


async def test_a_comment_falls_back_to_the_session_head_when_the_refs_are_unknown(writer):
    await writer.post_comment(REF, {"new_path": "a.py", "new_line": 1, "sha": "only"}, "x")
    assert sent[-1][3]["commit_id"] == "only"


async def test_a_review_wide_comment_goes_to_the_pull_request_itself(writer):
    await writer.post_mr_comment(REF, "reads well overall")
    assert sent[-1][:2] == ("POST", "/repos/o/r/issues/7/comments")


async def test_a_suggestion_is_a_comment_in_the_form_github_renders(writer):
    await writer.suggest(REF, {"new_path": "a.py", "new_line": 3, "head_sha": "head1"},
                         "if x is not None:")
    assert sent[-1][3]["body"] == "```suggestion\nif x is not None:\n```"


async def test_approving_is_a_review_with_a_verdict(writer):
    await writer.approve(REF)
    assert sent[-1][:2] == ("POST", "/repos/o/r/pulls/7/reviews")
    assert sent[-1][3] == {"event": "APPROVE"}


async def test_replying_and_resolving_address_the_thread_itself(writer):
    """Both are GraphQL: REST will not report a thread's resolution, let alone set it. Addressing
    the thread by its own id is what saves every caller from reconstructing which comment began it.
    """
    await writer.reply(REF, "THREAD_1", "fixed in the next push")
    assert sent[-1][1].endswith("/graphql")
    assert sent[-1][3]["variables"] == {"thread": "THREAD_1", "body": "fixed in the next push"}

    await writer.resolve(REF, "THREAD_1", True)
    assert "resolveReviewThread" in sent[-1][3]["query"]
    await writer.resolve(REF, "THREAD_1", False)
    assert "unresolveReviewThread" in sent[-1][3]["query"]


async def test_a_note_is_edited_and_deleted_by_its_own_id(writer):
    await writer.edit_note(REF, "THREAD_1", "901", "reworded")
    assert sent[-1][:2] == ("PATCH", "/repos/o/r/pulls/comments/901")
    await writer.delete_note(REF, "THREAD_1", "901")
    assert sent[-1][:2] == ("DELETE", "/repos/o/r/pulls/comments/901")


async def test_a_capability_the_forge_lacks_is_refused_rather_than_attempted(writer):
    writer._caps["approvals"] = False
    with pytest.raises(CapabilityError):
        await writer.approve(REF)
