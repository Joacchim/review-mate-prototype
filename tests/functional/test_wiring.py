"""Tests for the live wiring: GitLab config resolution, diff-refs in write-back, create-from-ref."""
import httpx
import pytest

from review_mate.host.config import resolve_gitlab_config, build_gitlab_provider
from review_mate.host.base import GITLAB_CAPABILITIES
from review_mate.host.gitlab import GitLabWriter
from review_mate.seams import MRRef, MRPayload
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.session.commands import AddHighlight, ApplyMRMetadata
from review_mate.session.state import MRMetadata, FileEntry, ChangeType, Side, LineRange, Origin
from review_mate.writeback.service import Writeback


# --- (A) GitLab config from env ---------------------------------------------

def test_config_from_env(monkeypatch):
    monkeypatch.setenv("REVIEW_MATE_GITLAB_URL", "https://gl.example.com/api/v4")
    monkeypatch.setenv("REVIEW_MATE_GITLAB_TOKEN", "glpat-x")
    monkeypatch.setenv("REVIEW_MATE_GITLAB_USER", "me")
    cfg = resolve_gitlab_config()
    assert cfg is not None
    assert cfg.host == "gl.example.com" and cfg.username == "me"
    provider = build_gitlab_provider(cfg)
    assert provider.host == "gl.example.com"


def test_no_credentials_no_provider(monkeypatch):
    for var in ("REVIEW_MATE_GITLAB_URL", "REVIEW_MATE_GITLAB_TOKEN", "REVIEW_MATE_GITLAB_USER",
                "GITLAB_TOKEN", "GITLAB_USER", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent-config-dir")
    assert resolve_gitlab_config() is None


# --- (B) write-back uses real diff-version shas ------------------------------

@pytest.fixture
def calls():
    return []


@pytest.fixture
async def session_with_diffrefs(tmp_path):
    m = SessionManager(root=tmp_path / "sessions")
    sid = await m.create()
    a = m.get(sid)
    await a.submit(ApplyMRMetadata(
        mr=MRMetadata(host="gitlab", project="g/p", iid=42, title="t", source_branch="x",
                      target_branch="main", sha="head1", author="d", url="u",
                      diff_refs={"base_sha": "B", "head_sha": "H", "start_sha": "S"})), Origin.SYSTEM)
    await a.submit(AddHighlight(file="a.py", side=Side.NEW, line_range=LineRange(start=7, end=7)),
                   Origin.BROWSER)
    yield m, sid, a.snapshot().highlights[0].id
    await m.shutdown()


async def test_writeback_uses_diff_refs(session_with_diffrefs, calls):
    m, sid, hid = session_with_diffrefs

    def handler(request):
        import json
        calls.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "d"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://gl/api/v4")
    writer = GitLabWriter("https://gl/api/v4", "t", dict(GITLAB_CAPABILITIES), client=client)
    await Writeback(m, writer).post_comment(sid, hid, "comment", MRRef(host="gitlab", project="g/p", iid=42))
    pos = calls[-1]["position"]
    assert pos["base_sha"] == "B" and pos["head_sha"] == "H" and pos["start_sha"] == "S"
    assert pos["new_line"] == 7


# --- (C) create a session from a reference string ---------------------------

class _FakeSource:
    async def load(self, ref: MRRef) -> MRPayload:
        return MRPayload(
            mr=MRMetadata(host=ref.host, project=ref.project, iid=ref.iid, title="loaded",
                          source_branch="x", target_branch="main", sha="s", author="d", url="u"),
            files=[FileEntry(path="x.py", change_type=ChangeType.MODIFIED)],
            clone_url="https://gl/g/p.git",
        )


async def test_create_session_from_ref_string(tmp_path):
    manager = SessionManager(root=tmp_path / "sessions", mr_source=_FakeSource())
    resolve = lambda s: MRRef(host="gl", project="g/p", iid=7) if "!" in s else None
    app = create_app(manager=manager, resolve_ref=resolve)
    from httpx import ASGITransport
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/sessions", json={"ref": "g/p!7"})
        assert r.json()["ref_resolved"] is True
        sid = r.json()["id"]
        snap = await c.get(f"/api/sessions/{sid}")
        assert snap.json()["mr"]["title"] == "loaded"
        assert [f["path"] for f in snap.json()["files"]] == ["x.py"]
    await manager.shutdown()
