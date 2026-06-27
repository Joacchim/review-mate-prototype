"""Functional tests for GitLabProvider against a fake GitLab API (httpx MockTransport)."""
import httpx
import pytest

from review_mate.host.gitlab import GitLabProvider
from review_mate.host.base import GITLAB_CAPABILITIES
from review_mate.seams import MRRef, MRSource


PROJECT = {"path_with_namespace": "group/proj", "http_url_to_repo": "https://gitlab/group/proj.git"}
MR = {"iid": 42, "title": "Add thing", "source_branch": "feat", "target_branch": "main",
      "sha": "deadbeef", "author": {"username": "dev"}, "web_url": "https://gitlab/group/proj/-/merge_requests/42"}
CHANGES = {"changes": [
    {"old_path": "a.py", "new_path": "a.py", "new_file": False, "deleted_file": False,
     "renamed_file": False, "diff": "@@ -1 +1 @@\n-old\n+new\n"},
    {"old_path": "b.py", "new_path": "b.py", "new_file": True, "deleted_file": False,
     "renamed_file": False, "diff": "@@ -0,0 +1 @@\n+hi\n"},
]}
DISCUSSIONS = [{"id": "disc1", "notes": [
    {"id": 1, "author": {"username": "rev"}, "body": "nit", "created_at": "t",
     "position": {"new_path": "a.py", "new_line": 1}, "resolved": False}]}]
QUEUE = [{"web_url": "https://gitlab/group/proj/-/merge_requests/42"},
         {"web_url": "https://gitlab/group/proj/-/merge_requests/43"}]


def _handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p.endswith("/changes"):
        return httpx.Response(200, json=CHANGES)
    if p.endswith("/discussions"):
        return httpx.Response(200, json=DISCUSSIONS)
    if "/merge_requests/42" in p:
        return httpx.Response(200, json=MR)
    if p.endswith("/related_merge_requests"):
        return httpx.Response(200, json=QUEUE)
    if p.endswith("/merge_requests"):
        return httpx.Response(200, json=QUEUE)
    if "/projects/" in p:
        return httpx.Response(200, json=PROJECT)
    return httpx.Response(404, json={})


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
    assert payload.clone_url == "https://gitlab/group/proj.git"
    assert [f.path for f in payload.files] == ["a.py", "b.py"]
    assert payload.files[1].change_type.value == "added"
    assert len(payload.threads) == 1 and payload.threads[0].comments[0].body == "nit"
    assert payload.mr.capabilities == GITLAB_CAPABILITIES


async def test_review_queue(provider):  # AC-5
    refs = await provider.review_queue()
    assert [r.iid for r in refs] == [42, 43]
    assert all(r.project == "group/proj" for r in refs)


async def test_issue_related_mrs(provider):  # AC-6
    refs = await provider.issue_related_mrs("group/proj", 5)
    assert [r.iid for r in refs] == [42, 43]


def test_capabilities_cover_review_model():  # AC-7
    for cap in ("threads", "suggestions", "approvals", "diff_versions"):
        assert GITLAB_CAPABILITIES.get(cap) is True
