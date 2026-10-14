"""Functional tests for the write side (GitLab), against a fake API capturing requests."""
import json

import httpx
import pytest

from review_mate.host.base import HostWriter, CapabilityError, GITLAB_CAPABILITIES
from review_mate.host.gitlab import GitLabWriter
from review_mate.contracts import MRRef
from review_mate.writeback.service import Writeback
from review_mate.session.manager import SessionManager
from review_mate.session.commands import ApplyFiles, ApplyMRMetadata, AddHighlight
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


@pytest.fixture
async def writeback_at(tmp_path, calls, writer):
    """Post a comment on one line of a staged file, and hand back the position that went out.

    Through the real Writeback over a real session, because the old side is worked out from the
    diff the session holds — a writer test alone would only prove the adapter passes on whatever
    it is given.
    """
    from review_mate.session.state import SessionStatus

    async def post(entry, line: int):
        manager = SessionManager(root=tmp_path / "sessions")
        sid = await manager.create()
        session = manager.get(sid)
        await session.submit(ApplyMRMetadata(
            mr=MRMetadata(host="gitlab", project="g/p", iid=42, title="t", source_branch="x",
                          target_branch="main", sha="headsha", author="a", url="u",
                          diff_refs={"base_sha": "b", "head_sha": "headsha", "start_sha": "s"})),
            Origin.SYSTEM)
        await session.submit(ApplyFiles(files=[entry]), Origin.SYSTEM)
        result = await session.submit(
            AddHighlight(file=entry.path, side=Side.NEW,
                         line_range=LineRange(start=line, end=line)), Origin.BROWSER)
        hl = next(h for h in session.snapshot().highlights)
        await Writeback(manager, writer).post_comment(sid, hl.id, "needs a guard", REF)
        await manager.shutdown()
        return calls[-1][3]["position"]
    yield post


def test_writer_satisfies_protocol(writer):  # AC-7
    assert isinstance(writer, HostWriter)


async def test_post_comment_inline(writer, calls):  # AC-1
    await writer.post_comment(REF, {"new_path": "a.py", "new_line": 12, "sha": "abc"}, "needs a guard")
    method, path, _params, body = calls[-1]
    assert method == "POST" and path.endswith("/merge_requests/42/discussions")
    assert body["body"] == "needs a guard"
    assert body["position"]["new_path"] == "a.py" and body["position"]["new_line"] == 12


async def test_post_mr_comment_is_an_unanchored_note(writer, calls):
    await writer.post_mr_comment(REF, "overall this looks solid; one concern below")
    method, path, _params, body = calls[-1]
    assert method == "POST" and path.endswith("/merge_requests/42/notes")
    assert body == {"body": "overall this looks solid; one concern below"}  # no position


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


async def test_unresolve_sends_resolved_false(writer, calls):
    await writer.resolve(REF, "disc1", resolved=False)
    assert calls[-1][2].get("resolved") == "false"


async def test_reply_posts_to_the_discussion(writer, calls):
    await writer.reply(REF, "disc1", "addressed in the latest push")
    method, path, _params, body = calls[-1]
    assert method == "POST" and path.endswith("/discussions/disc1/notes")
    assert body == {"body": "addressed in the latest push"}


async def test_edit_note_puts_the_note(writer, calls):
    await writer.edit_note(REF, "disc1", "7", "fixed wording")
    method, path, params, _ = calls[-1]
    assert method == "PUT" and path.endswith("/discussions/disc1/notes/7")
    assert params.get("body") == "fixed wording"


async def test_delete_note_deletes(writer, calls):
    await writer.delete_note(REF, "disc1", "7")
    method, path, _params, _ = calls[-1]
    assert method == "DELETE" and path.endswith("/discussions/disc1/notes/7")


async def test_writeback_thread_verbs_passthrough(session, writer, calls):
    m = session[0]
    wb = Writeback(m, writer)
    await wb.reply(REF, "disc1", "reply text")
    assert calls[-1][1].endswith("/discussions/disc1/notes")
    await wb.resolve(REF, "disc1", resolved=False)
    assert calls[-1][2].get("resolved") == "false"
    await wb.approve(REF)
    assert calls[-1][1].endswith("/merge_requests/42/approve")


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


async def test_writeback_posts_mr_level_comment_when_unanchored(session, writer, calls):
    m, sid, _hid = session
    wb = Writeback(m, writer)
    await wb.post_comment(sid, None, "a review summary", REF)   # None anchor = MR-level
    method, path, _params, body = calls[-1]
    assert method == "POST" and path.endswith("/merge_requests/42/notes")
    assert body == {"body": "a review summary"} and "position" not in body


class _PositionSpy:
    """A writer that keeps the host-neutral position it was handed — which is the thing under test
    here. What a forge then makes of it is that adapter's business and its own test."""

    def __init__(self):
        self.positions = []

    def capabilities(self):
        return dict(GITLAB_CAPABILITIES)

    async def post_comment(self, ref, position, body):
        self.positions.append(position)
        return {"id": "disc1", "notes": [{"id": 1}]}


async def test_the_commit_a_mark_was_aimed_at_travels_with_its_comment(session):
    """A mark made while reading an intermediate commit is about that commit, and its line numbers
    are that commit's. Posting it against the head would land on whatever is at that number now —
    the wrong line, and sometimes someone else's code."""
    m, sid, _hid = session
    marked = m.get(sid)
    await marked.submit(AddHighlight(file="a.py", side=Side.NEW,
                                     line_range=LineRange(start=3, end=3), commit_sha="older7"),
                        Origin.BROWSER)
    aimed = marked.snapshot().highlights[-1].id

    spy = _PositionSpy()
    await Writeback(m, spy).post_comment(sid, aimed, "fix it where it is written", REF)
    assert spy.positions[-1]["commit_sha"] == "older7"


async def test_an_ordinary_comment_names_no_commit(session):
    m, sid, hid = session
    spy = _PositionSpy()
    await Writeback(m, spy).post_comment(sid, hid, "about the change", REF)
    assert spy.positions[-1]["commit_sha"] is None


# --- where a line was before the change ---------------------------------------
# A forge identifies a line by both its sides. A comment anchored on a line the change did not
# touch — which is most of what a reviewer marks when quoting the code around a problem — was
# refused with a bare 400 while one on an added line went through.

DIFF_BOTH = ("@@ -103,6 +110,8 @@ def authorise(token):\n"
             "     if token is None:\n"
             "         return None\n"
             "+    if token.expired:\n"
             "+        return None\n"
             "     return lookup(token)\n")


def _staged_file():
    from review_mate.session.state import ChangeType, FileEntry
    return FileEntry(path="vmproxy/api/auth.py", change_type=ChangeType.MODIFIED,
                     hunks=[{"diff": DIFF_BOTH}])


async def test_a_comment_on_an_unchanged_line_carries_where_it_was(writeback_at):
    """Line 110 is context: unchanged, present on both sides. Without its old number the forge
    cannot build a line identifier, and the comment is refused."""
    position = await writeback_at(_staged_file(), line=110)
    assert position["old_line"] == 103, position
    assert position["old_path"] == "vmproxy/api/auth.py"


async def test_a_comment_on_an_added_line_says_it_has_no_old_side(writeback_at):
    """Line 112 is added — there is no old line, and claiming one would place the comment on
    whatever happened to be there before."""
    position = await writeback_at(_staged_file(), line=112)
    assert "old_line" not in position, position
    assert position["old_path"] == "vmproxy/api/auth.py"


async def test_a_renamed_file_is_named_by_both_of_its_names(writeback_at):
    """The old side of a position is read against the old path, which is a different name."""
    from review_mate.session.state import ChangeType, FileEntry
    moved = FileEntry(path="vmproxy/api/auth.py", old_path="vmproxy/auth.py",
                      change_type=ChangeType.RENAMED, hunks=[{"diff": DIFF_BOTH}])
    position = await writeback_at(moved, line=110)
    assert position["old_path"] == "vmproxy/auth.py" and position["new_path"] == "vmproxy/api/auth.py"
