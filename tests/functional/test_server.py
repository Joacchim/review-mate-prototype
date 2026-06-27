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
