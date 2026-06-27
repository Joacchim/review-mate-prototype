"""Functional tests for the write side (GitLab), against a fake API capturing requests."""
import json

import httpx
import pytest

from review_mate.host.base import HostWriter, CapabilityError, GITLAB_CAPABILITIES
from review_mate.host.gitlab import GitLabWriter
from review_mate.seams import MRRef
from review_mate.writeback.service import Writeback
from review_mate.session.manager import SessionManager
from review_mate.session.commands import ApplyMRMetadata, AddHighlight
from review_mate.session.state import MRMetadata, Side, LineRange, Origin


REF = MRRef(host="gitlab", project="g/p", iid=42)


def _client_recording(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, dict(request.url.params), body))
        return httpx.Response(201, json={"id": "disc1"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://gitlab/api/v4")


@pytest.fixture
def calls():
    return []


@pytest.fixture
def writer(calls):
    return GitLabWriter(base_url="https://gitlab/api/v4", token="t",
                        capabilities=dict(GITLAB_CAPABILITIES), client=_client_recording(calls))


def test_writer_satisfies_protocol(writer):  # AC-7
    assert isinstance(writer, HostWriter)


async def test_post_comment_inline(writer, calls):  # AC-1
    await writer.post_comment(REF, {"new_path": "a.py", "new_line": 12, "sha": "abc"}, "needs a guard")
    method, path, _params, body = calls[-1]
    assert method == "POST" and path.endswith("/merge_requests/42/discussions")
    assert body["body"] == "needs a guard"
    assert body["position"]["new_path"] == "a.py" and body["position"]["new_line"] == 12


async def test_capability_gating_blocks_and_sends_nothing(calls):  # AC-3
    caps = dict(GITLAB_CAPABILITIES); caps["approvals"] = False
    w = GitLabWriter(base_url="https://gitlab/api/v4", token="t", capabilities=caps,
                     client=_client_recording(calls))
    with pytest.raises(CapabilityError):
        await w.approve(REF)
    assert calls == []  # nothing posted


async def test_approve(writer, calls):  # AC-4
    await writer.approve(REF)
    assert calls[-1][1].endswith("/merge_requests/42/approve")


async def test_resolve(writer, calls):  # AC-5
    await writer.resolve(REF, "disc1")
    method, path, params, _ = calls[-1]
    assert method == "PUT" and path.endswith("/discussions/disc1")
    assert params.get("resolved") == "true"


async def test_suggestion_has_block(writer, calls):  # AC-6
    await writer.suggest(REF, {"new_path": "a.py", "new_line": 12}, "fixed = True")
    body = calls[-1][3]["body"]
    assert "```suggestion" in body and "fixed = True" in body


# --- Writeback ties a highlight to a reviewer-authored, anchored comment (D14) ---

@pytest.fixture
async def session(tmp_path):
    m = SessionManager(root=tmp_path / "sessions")
    sid = await m.create()
    a = m.get(sid)
    await a.submit(ApplyMRMetadata(mr=MRMetadata(host="gitlab", project="g/p", iid=42, title="t",
                   source_branch="x", target_branch="main", sha="abc", author="d", url="u")),
                   Origin.SYSTEM)
    await a.submit(AddHighlight(file="a.py", side=Side.NEW, line_range=LineRange(start=12, end=12)),
                   Origin.BROWSER)
    yield m, sid, a.snapshot().highlights[0].id
    await m.shutdown()


async def test_writeback_posts_reviewer_text_anchored_to_highlight(session, writer, calls):  # AC-1,2
    m, sid, hid = session
    wb = Writeback(m, writer)
    await wb.post_comment(sid, hid, "the reviewer's own words", REF)
    body = calls[-1][3]
    assert body["body"] == "the reviewer's own words"          # reviewer text, not a card (D14)
    assert body["position"]["new_path"] == "a.py" and body["position"]["new_line"] == 12
