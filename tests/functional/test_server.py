"""Functional tests for the HTTP + WS transport over the manager.

Covers AC-6 (serve UI) and re-covers AC-1/2/3/5/10 through the real HTTP/WS surface.
"""
import json

import httpx
import pytest
from httpx import ASGITransport
from starlette.testclient import TestClient

from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager


@pytest.fixture
async def client(tmp_path):
    manager = SessionManager(root=tmp_path / "sessions")
    app = create_app(manager=manager)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    await manager.shutdown()


async def test_serves_ui_root(client):  # AC-6
    r = await client.get("/")
    assert r.status_code == 200
    assert "review-mate" in r.text.lower()


async def test_create_then_snapshot(client):  # AC-1
    r = await client.post("/api/sessions")
    assert r.status_code == 200
    sid = r.json()["id"]
    snap = await client.get(f"/api/sessions/{sid}")
    assert snap.status_code == 200
    assert snap.json()["status"] == "active"


async def test_unknown_session_404(client):  # AC-10
    assert (await client.get("/api/sessions/nope")).status_code == 404
    bad = await client.post("/api/sessions/nope/commands",
                            json={"type": "add_highlight", "file": "a.py", "side": "new",
                                  "line_range": {"start": 1, "end": 1}})
    assert bad.status_code == 404


async def test_command_applies_via_http(client):  # AC-2 (HTTP path)
    sid = (await client.post("/api/sessions")).json()["id"]
    r = await client.post(f"/api/sessions/{sid}/commands",
                          json={"type": "add_highlight", "file": "a.py", "side": "new",
                                "line_range": {"start": 1, "end": 2}})
    assert r.status_code == 200 and r.json()["ok"]
    snap = await client.get(f"/api/sessions/{sid}")
    assert len(snap.json()["highlights"]) == 1


async def test_unauthorized_command_rejected_400(client):  # AC-9 via HTTP
    sid = (await client.post("/api/sessions")).json()["id"]
    r = await client.post(f"/api/sessions/{sid}/commands",
                          json={"type": "emit_card", "highlight_id": "x", "body": "b"})
    assert r.status_code == 400  # browser may not emit cards


async def test_malformed_command_400(client):
    sid = (await client.post("/api/sessions")).json()["id"]
    r = await client.post(f"/api/sessions/{sid}/commands", json={"type": "nonsense"})
    assert r.status_code == 400


async def test_end_session_via_http(client):  # AC-5
    sid = (await client.post("/api/sessions")).json()["id"]
    assert (await client.delete(f"/api/sessions/{sid}")).status_code == 200
    assert (await client.get(f"/api/sessions/{sid}")).json()["status"] == "ended"


async def test_search_no_provider_returns_empty(client):
    r = await client.get("/api/search?q=anything")
    assert r.status_code == 200 and r.json() == []


async def test_search_routes_to_provider(tmp_path):
    class StubProvider:
        async def search(self, q):
            return [{"host": "gitlab", "project": "g/p", "iid": 9, "title": f"hit:{q}", "url": "u"}]

    manager = SessionManager(root=tmp_path / "sessions")
    app = create_app(manager=manager, provider=StubProvider())
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        empty = await c.get("/api/search?q=")            # blank query → no host call
        assert empty.json() == []
        r = await c.get("/api/search?q=cache")
        assert r.json()[0]["title"] == "hit:cache"
    await manager.shutdown()


async def test_session_list_carries_mr_and_progress_counts(tmp_path):
    from review_mate.session.commands import AddHighlight, ApplyMRMetadata, EmitCard, SaveDraft
    from review_mate.session.state import LineRange, MRMetadata, Origin, Side

    manager = SessionManager(root=tmp_path / "sessions")
    app = create_app(manager=manager)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        sid = (await c.post("/api/sessions")).json()["id"]
        actor = manager.get(sid)
        await actor.submit(ApplyMRMetadata(mr=MRMetadata(
            host="gitlab", project="g/p", iid=7, title="T", source_branch="x", target_branch="m",
            sha="s", author="a", url="u")), Origin.SYSTEM)
        await actor.submit(AddHighlight(file="a.py", side=Side.NEW,
                                        line_range=LineRange(start=1, end=1)), Origin.BROWSER)
        hid = actor.snapshot().highlights[0].id
        await actor.submit(EmitCard(highlight_id=hid, body="ctx"), Origin.AGENT)
        await actor.submit(SaveDraft(highlight_id=hid, body="nit"), Origin.BROWSER)

        listed = (await c.get("/api/sessions")).json()
        row = next(s for s in listed if s["id"] == sid)
        assert row["project"] == "g/p" and row["iid"] == 7 and row["title"] == "T"
        assert row["highlights"] == 1 and row["cards"] == 1
        assert row["drafts_pending"] == 1 and row["drafts_posted"] == 0

        # closing the session ends it → drops from the active set the hub shows
        await c.delete(f"/api/sessions/{sid}")
        after = (await c.get("/api/sessions")).json()
        active = [s for s in after if s["status"] == "active"]
        assert sid not in [s["id"] for s in active]
    await manager.shutdown()


async def test_submit_review_unavailable_without_writer(client):
    sid = (await client.post("/api/sessions")).json()["id"]
    r = await client.post(f"/api/sessions/{sid}/submit-review")
    assert r.status_code == 400  # no host writer configured (self-contained baseline)


async def test_submit_review_posts_drafts_and_reports_partial_failure(tmp_path):
    from review_mate.session.commands import AddHighlight, ApplyMRMetadata, SaveDraft
    from review_mate.session.state import LineRange, MRMetadata, Origin, Side

    class FakeWriteback:
        async def post_comment(self, sid, highlight_id, body, ref):
            if "boom" in body:
                raise RuntimeError("bad anchor")
            return {"notes": [{"id": 101}]}

    manager = SessionManager(root=tmp_path / "sessions")
    app = create_app(manager=manager, writeback=FakeWriteback())
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        sid = (await c.post("/api/sessions")).json()["id"]
        actor = manager.get(sid)
        await actor.submit(ApplyMRMetadata(mr=MRMetadata(
            host="gitlab", project="g/p", iid=7, title="t", source_branch="x", target_branch="m",
            sha="s", author="a", url="http://h/g/p/-/merge_requests/7")), Origin.SYSTEM)
        for f in ("a.py", "b.py"):
            await actor.submit(AddHighlight(file=f, side=Side.NEW,
                                            line_range=LineRange(start=1, end=1)), Origin.BROWSER)
        hids = [h.id for h in actor.snapshot().highlights]
        await actor.submit(SaveDraft(highlight_id=hids[0], body="good comment"), Origin.BROWSER)
        await actor.submit(SaveDraft(highlight_id=hids[1], body="boom comment"), Origin.BROWSER)

        data = (await c.post(f"/api/sessions/{sid}/submit-review")).json()
        assert data["posted"] == 1 and data["total"] == 2
        snap = actor.snapshot()
        posted = [d for d in snap.drafts if d.status.value == "posted"]
        still_draft = [d for d in snap.drafts if d.status.value == "draft"]
        assert len(posted) == 1 and posted[0].url.endswith("#note_101")
        assert len(still_draft) == 1  # the failed one is left for a retry
    await manager.shutdown()


# WS uses Starlette's sync TestClient (httpx has no WS client)

def test_ws_stream_delivers_live_events(tmp_path):  # AC-3 over WS
    manager = SessionManager(root=tmp_path / "sessions")
    app = create_app(manager=manager)
    with TestClient(app) as tc:
        sid = tc.post("/api/sessions").json()["id"]
        with tc.websocket_connect(f"/api/sessions/{sid}/stream?since=0") as ws:
            first = json.loads(ws.receive_text())  # session_created replayed
            assert first["type"] == "session_created"
            tc.post(f"/api/sessions/{sid}/commands",
                    json={"type": "add_highlight", "file": "live.py", "side": "new",
                          "line_range": {"start": 1, "end": 1}})
            evt = json.loads(ws.receive_text())
            assert evt["type"] == "highlight_added"
            assert evt["highlight"]["file"] == "live.py"
