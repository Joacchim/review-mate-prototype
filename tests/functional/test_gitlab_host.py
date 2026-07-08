"""Functional tests for GitLabProvider against a fake GitLab API (httpx MockTransport)."""
from urllib.parse import unquote

import httpx
import pytest

from review_mate.host.gitlab import GitLabProvider
from review_mate.host.base import GITLAB_CAPABILITIES
from review_mate.seams import MRRef, MRSource


PROJECT = {"path_with_namespace": "group/proj", "http_url_to_repo": "https://gitlab/group/proj.git",
           "ssh_url_to_repo": "git@gitlab:group/proj.git"}
MR = {"iid": 42, "title": "Add thing", "source_branch": "feat", "target_branch": "main",
      "sha": "deadbeef", "author": {"username": "dev"}, "web_url": "https://gitlab/group/proj/-/merge_requests/42"}
CHANGES = {"changes": [
    {"old_path": "a.py", "new_path": "a.py", "new_file": False, "deleted_file": False,
     "renamed_file": False, "diff": "@@ -1 +1 @@\n-old\n+new\n"},
    {"old_path": "b.py", "new_path": "b.py", "new_file": True, "deleted_file": False,
     "renamed_file": False, "diff": "@@ -0,0 +1 @@\n+hi\n"},
]}
DISCUSSIONS = [
    {"id": "disc1", "notes": [
        {"id": 1, "author": {"username": "rev"}, "body": "nit", "created_at": "t",
         "position": {"new_path": "a.py", "new_line": 1}, "resolved": False}]},
    # a system-note-only discussion (an approval) — MR-history noise, must be filtered out
    {"id": "sys1", "notes": [
        {"id": 2, "author": {"username": "rev"}, "body": "approved this merge request",
         "system": True, "created_at": "t"}]}]
QUEUE = [{"web_url": "https://gitlab/group/proj/-/merge_requests/42"},
         {"web_url": "https://gitlab/group/proj/-/merge_requests/43"}]


SEARCH_HITS = [{"title": "fix cache", "web_url": "https://gitlab/group/proj/-/merge_requests/77"}]
PROJECTS = [{"path_with_namespace": "group/proj"}]

# The fake knows exactly one project: group/proj, named "proj", with two opened MRs (42, 43).


BLAME = [{"commit": {"id": "abc123def456", "author_name": "dev", "committed_date": "2026-01-02",
                     "title": "add the guard"}, "lines": ["x = 1"]}]
CLOSES_ISSUES = [{"iid": 7, "title": "Fix the leak", "web_url": "https://gitlab/group/proj/-/issues/7"}]
VERSIONS = [{"base_commit_sha": "base2", "head_commit_sha": "head2", "start_commit_sha": "s2",
             "created_at": "2026-02-02"},
            {"base_commit_sha": "base1", "head_commit_sha": "head1", "start_commit_sha": "s1",
             "created_at": "2026-01-01"}]


COMMITS = [  # GitLab returns newest-first
    {"id": "sha2", "short_id": "sha2sh", "title": "second", "message": "second\n\nbody",
     "author_name": "dev", "created_at": "t2"},
    {"id": "sha1", "short_id": "sha1sh", "title": "first", "message": "first", "author_name": "dev",
     "created_at": "t1"}]
COMMIT_DIFF = [{"old_path": "a.py", "new_path": "a.py", "new_file": False, "deleted_file": False,
                "renamed_file": False, "diff": "@@ -1 +1 @@\n-x\n+y\n"}]


APPROVALS = {"approved": True, "approved_by": [{"user": {"username": "me"}},
                                               {"user": {"username": "other"}}]}


def _handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    params = dict(request.url.params)
    # sub-resources of a specific MR
    if p.endswith("/approvals"):
        return httpx.Response(200, json=APPROVALS)
    if p.endswith("/commits"):
        return httpx.Response(200, json=COMMITS)
    if "/repository/commits/" in p and p.endswith("/diff"):
        return httpx.Response(200, json=COMMIT_DIFF)
    if p.endswith("/versions"):
        return httpx.Response(200, json=VERSIONS)
    if p.endswith("/blame"):
        return httpx.Response(200, json=BLAME)
    if p.endswith("/closes_issues"):
        return httpx.Response(200, json=CLOSES_ISSUES)
    if p.endswith("/changes"):
        return httpx.Response(200, json=CHANGES)
    if p.endswith("/discussions"):
        return httpx.Response(200, json=DISCUSSIONS)
    if p.endswith("/related_merge_requests"):
        return httpx.Response(200, json=QUEUE)
    if "/merge_requests/42" in p:
        return httpx.Response(200, json=MR)
    # any merge_requests listing — the global review-queue or a project's opened MRs
    if p.endswith("/merge_requests"):
        return httpx.Response(200, json=QUEUE)
    # global cross-project MR text search — only "cache" matches an MR title here
    if p.endswith("/search"):
        return httpx.Response(200, json=SEARCH_HITS if params.get("search") == "cache" else [])
    # project name search — only "proj" (or the full path) matches, and only when scoped to the
    # reviewer's memberships (an unscoped name search must not resolve the project here)
    if p.endswith("/projects"):
        matches = params.get("search") in ("proj", "group/proj") and params.get("membership") == "true"
        return httpx.Response(200, json=PROJECTS if matches else [])
    # exact project by url-encoded path — only group/proj exists
    if "/projects/" in p:
        ident = unquote(p.rsplit("/projects/", 1)[-1])
        return httpx.Response(200, json=PROJECT) if ident == "group/proj" else httpx.Response(404, json={})
    return httpx.Response(404, json={})


def test_glab_git_protocol_per_host_overrides_global(tmp_path, monkeypatch):
    from review_mate.host.config import _glab_git_protocol
    cfgdir = tmp_path / "glab-cli"; cfgdir.mkdir()
    (cfgdir / "config.yml").write_text(
        "git_protocol: https\nhosts:\n    gitlab.com:\n        git_protocol: ssh\n        user: me\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert _glab_git_protocol("gitlab.com") == "ssh"     # the host's own block wins
    assert _glab_git_protocol("other.host") == "https"   # no block → the global default
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "absent"))
    assert _glab_git_protocol("gitlab.com") is None      # no glab config → caller defaults to https


@pytest.fixture
def provider():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler),
                               base_url="https://gitlab/api/v4")
    return GitLabProvider(base_url="https://gitlab/api/v4", token="t",
                          username="me", host="gitlab", client=client)


def test_satisfies_mrsource(provider):  # AC-8
    assert isinstance(provider, MRSource)


async def test_load_maps_payload(provider):  # AC-1, 7
    payload = await provider.load(MRRef(host="gitlab", project="group/proj", iid=42))
    assert payload.mr.iid == 42 and payload.mr.title == "Add thing"
    assert payload.mr.author == "dev"
    assert payload.clone_url == "https://gitlab/group/proj.git"   # default: HTTPS clone URL
    assert [f.path for f in payload.files] == ["a.py", "b.py"]


async def test_load_uses_ssh_clone_url_when_protocol_is_ssh():
    # honoring the git access method the user chose in glab (git_protocol: ssh)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="https://gitlab/api/v4")
    p = GitLabProvider(base_url="https://gitlab/api/v4", token="t", username="me",
                       host="gitlab", client=client, git_protocol="ssh")
    payload = await p.load(MRRef(host="gitlab", project="group/proj", iid=42))
    assert payload.clone_url == "git@gitlab:group/proj.git"
    assert payload.mr.clone_url == "git@gitlab:group/proj.git"
    assert payload.files[1].change_type.value == "added"
    assert len(payload.threads) == 1 and payload.threads[0].comments[0].body == "nit"
    assert payload.mr.capabilities == GITLAB_CAPABILITIES


async def test_approvals_maps_state_and_detects_self(provider):   # provider username is "me"
    a = await provider.approvals(MRRef(host="gitlab", project="group/proj", iid=42))
    assert a["approved_by"] == ["me", "other"] and a["you_approved"] is True


async def test_commits_mapped_oldest_first(provider):
    cs = await provider.commits(MRRef(host="gitlab", project="group/proj", iid=42))
    assert [c["sha"] for c in cs] == ["sha1", "sha2"]     # reversed into authoring order
    assert cs[0]["title"] == "first" and cs[1]["short_id"] == "sha2sh"


async def test_commit_diff_maps_files(provider):
    files = await provider.commit_diff("group/proj", "sha2")
    assert [f.path for f in files] == ["a.py"]
    assert "+y" in files[0].hunks[0]["diff"]


async def test_review_queue(provider):  # AC-5
    refs = await provider.review_queue()
    assert [r.iid for r in refs] == [42, 43]
    assert all(r.project == "group/proj" for r in refs)


async def test_issue_related_mrs(provider):  # AC-6
    refs = await provider.issue_related_mrs("group/proj", 5)
    assert [r.iid for r in refs] == [42, 43]


async def test_mr_versions_maps_bases_and_heads(provider):
    vs = await provider.mr_versions(MRRef(host="gitlab", project="group/proj", iid=42))
    assert vs[0] == {"base_sha": "base2", "head_sha": "head2", "start_sha": "s2",
                     "created_at": "2026-02-02"}
    assert [v["head_sha"] for v in vs] == ["head2", "head1"]


async def test_blame_maps_last_touch(provider):
    rows = await provider.blame("group/proj", "a.py", "sha1", 5, 5)
    assert rows and rows[0]["commit"] == "abc123def456" and rows[0]["author"] == "dev"
    assert rows[0]["summary"] == "add the guard" and rows[0]["lines"] == [5, 5]


async def test_linked_issues_maps_closes_issues(provider):
    issues = await provider.linked_issues("group/proj", 42)
    assert issues == [{"iid": 7, "title": "Fix the leak",
                       "url": "https://gitlab/group/proj/-/issues/7"}]


async def test_cheap_context_degrades_on_host_error():
    # a host that errors on every read → the cheap tier yields [] rather than raising
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, json={})),
                               base_url="https://gitlab/api/v4")
    p = GitLabProvider(base_url="https://gitlab/api/v4", token="t", username="me", client=client)
    assert await p.blame("group/proj", "a.py", "s", 1, 1) == []
    assert await p.linked_issues("group/proj", 42) == []


async def test_fetch_threads_maps_discussions_and_drops_system_notes(provider):
    threads = await provider.fetch_threads(MRRef(host="gitlab", project="group/proj", iid=42))
    assert [t.id for t in threads] == ["disc1"]   # the approval (system-note) discussion is filtered
    t = threads[0]
    assert t.resolved is False
    assert t.comments[0].body == "nit" and t.comments[0].author == "rev"
    assert t.anchor == {"file": "a.py", "line": 1}


async def test_search_fuzzy_text_falls_back_to_global(provider):
    # a query that names no project (only matches an MR title) still returns the global text hit
    items = await provider.search("cache")
    assert items[0] == {"host": "gitlab", "project": "group/proj", "iid": 77,
                        "title": "fix cache", "url": "https://gitlab/group/proj/-/merge_requests/77"}


async def test_search_empty_query_short_circuits(provider):
    assert await provider.search("   ") == []


async def test_search_lists_project_mrs_by_full_path(provider):
    # the primary intent: a project path lists THAT project's opened MRs, not a title keyword match
    items = await provider.search("group/proj")
    iids = sorted(it["iid"] for it in items)
    assert iids == [42, 43] and all(it["project"] == "group/proj" for it in items)


async def test_search_lists_project_mrs_by_bare_name(provider):
    # the bug fix: a bare project name (no slash) also resolves the project and lists its MRs
    items = await provider.search("proj")
    iids = sorted(it["iid"] for it in items)
    assert iids == [42, 43]


async def test_search_dedupes_project_resolved_twice(provider):
    # group/proj is resolved by both the exact-path hit and the name search; its MRs must not double
    items = await provider.search("group/proj")
    keys = [(it["project"], it["iid"]) for it in items]
    assert len(keys) == len(set(keys))


def _auth_handler(valid_token):
    """A fake that 401s unless the request carries `Bearer <valid_token>`."""
    def h(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {valid_token}":
            return httpx.Response(401, json={"message": "401 Unauthorized"})
        p = request.url.path
        if p.endswith("/projects"):
            return httpx.Response(200, json=[{"path_with_namespace": "group/proj"}])
        if p.endswith("/merge_requests"):
            return httpx.Response(200, json=[{"web_url": "https://gitlab/group/proj/-/merge_requests/1"}])
        return httpx.Response(200, json=[])
    return h


def _provider_with(handler, token="stale", reload_token=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://gitlab/api/v4")
    return GitLabProvider(base_url="https://gitlab/api/v4", token=token, username="me",
                          host="gitlab", client=client, reload_token=reload_token)


async def test_search_surfaces_auth_error_instead_of_empty():
    # a dead token → search raises (the route turns it into a visible 502), never a silent []
    p = _provider_with(_auth_handler("never-matches"))
    with pytest.raises(httpx.HTTPStatusError):
        await p.search("proj")


async def test_token_reloads_live_on_401_and_retries():
    # simulate a side `glab auth` refresh: the reloader hands back a now-valid token, and the
    # request succeeds on retry without a restart
    p = _provider_with(_auth_handler("fresh"), token="stale", reload_token=lambda: "fresh")
    items = await p.search("proj")
    assert p.token == "fresh"                       # credentials reloaded in place
    assert items and items[0]["project"] == "group/proj"


async def test_capabilities_cover_review_model():  # AC-7
    for cap in ("threads", "suggestions", "approvals", "diff_versions"):
        assert GITLAB_CAPABILITIES.get(cap) is True
