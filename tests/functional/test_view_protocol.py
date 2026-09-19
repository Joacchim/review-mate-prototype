"""The view protocol end to end: subscribe to a scope, act through /api/cmd, see the scope again.

The behavioural guarantee these pin down is the one a second client would otherwise break:
building the hub is local, and the per-review host fan-out happens only on an explicit
`hub.refresh` (D19).
"""
import asyncio
import json

import pytest
from starlette.testclient import TestClient

from review_mate.seams import MRPayload, MRRef
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.session.state import ChangeType, FileEntry, MRMetadata, ReviewThread
from review_mate.view.hub import HubScope


class Provider:
    """A host stub that counts its own calls, so a test can assert what was never called."""

    username = "reviewer"

    def __init__(self, queue=None, queue_error=None, unresolved=1, state="opened"):
        self.calls = {"queue": 0, "summary": 0, "threads": 0}
        self._queue = queue if queue is not None else [{"iid": 7, "title": "queued"}]
        self._queue_error = queue_error
        self._unresolved = unresolved
        self._state = state

    async def load(self, ref: MRRef) -> MRPayload:
        return MRPayload(
            mr=MRMetadata(host="gitlab", project=ref.project, iid=ref.iid, title="T",
                          source_branch="x", target_branch="main", sha="abc",
                          author="dev", url="http://x"),
            files=[FileEntry(path="a.py", change_type=ChangeType.MODIFIED)],
            threads=[],
        )

    async def review_queue_items(self):
        self.calls["queue"] += 1
        if self._queue_error is not None:
            raise self._queue_error
        return self._queue

    async def mr_summary(self, ref: MRRef):
        self.calls["summary"] += 1
        return {"head": "abc", "state": self._state}

    async def fetch_threads(self, ref: MRRef):
        self.calls["threads"] += 1
        return [ReviewThread(id=str(n), resolved=False) for n in range(self._unresolved)]


def build(tmp_path, provider):
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider)
    app = create_app(manager=manager, provider=provider,
                     resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))
    return app


def read_until(ws, predicate, limit=6):
    """Read messages until one satisfies `predicate`, and return it.

    The queue read races the first render by design — a fast host answers before the local half
    is even sent — so a test names the view it is waiting for rather than counting messages.
    """
    seen = []
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        seen.append(msg)
        if predicate(msg):
            return msg
    raise AssertionError(f"no message matched after {limit}: {seen}")


def hub_view(ws, predicate=lambda view: True, limit=6):
    msg = read_until(ws, lambda m: m["type"] == "scope" and m["scope"] == "hub"
                     and predicate(m["view"]), limit)
    return msg["view"]


def test_subscribing_delivers_the_hub_scope(tmp_path):
    with TestClient(build(tmp_path, Provider())) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            view = hub_view(ws)
            assert view["sessions"] == []
            assert view["user"] == "reviewer"
            assert view["queue_state"] in ("loading", "ready")


def test_the_queue_arrives_in_the_view(tmp_path):
    with TestClient(build(tmp_path, Provider())) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            view = hub_view(ws, lambda v: v["queue_state"] == "ready")
            assert view["queue"] == [{"iid": 7, "title": "queued"}]


def test_a_failing_queue_read_is_a_field_not_a_broken_view(tmp_path):
    provider = Provider(queue_error=RuntimeError("gitlab 503"))
    with TestClient(build(tmp_path, provider)) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            view = hub_view(ws, lambda v: v["queue_state"] == "error")
            assert "gitlab 503" in view["queue_error"]
            assert view["sessions"] == []          # the local half is unaffected


async def test_the_queue_reads_as_loading_until_the_host_answers(tmp_path):
    """The ordering guarantee, away from the transport where it would be a race."""
    gate = asyncio.Event()
    provider = Provider()

    async def gated_queue():
        await gate.wait()
        return [{"iid": 7}]

    provider.review_queue_items = gated_queue
    manager = SessionManager(root=tmp_path / "sessions")
    hub = HubScope(manager, provider=provider)
    published = []

    async def publish():
        published.append(await hub.build())

    assert (await hub.build())["queue_state"] == "idle"
    task = hub.ensure_queue(publish)
    assert (await hub.build())["queue_state"] == "loading"
    gate.set()
    await task
    assert (await hub.build())["queue_state"] == "ready"
    assert published[-1]["queue"] == [{"iid": 7}]
    await manager.shutdown()


def test_opening_and_closing_a_review_republishes_the_hub(tmp_path):
    with TestClient(build(tmp_path, Provider())) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            hub_view(ws)

            opened = tc.post("/api/cmd", json={"cmd": "session.open", "args": {"ref": "g/p!1"}})
            assert opened.status_code == 200 and opened.json()["ok"]
            sid = opened.json()["session"]
            view = hub_view(ws, lambda v: v["sessions"])
            assert [s["id"] for s in view["sessions"]] == [sid]
            assert view["sessions"][0]["mr"]["project"] == "g/p"

            closed = tc.post("/api/cmd", json={"cmd": "session.close", "args": {"id": sid}})
            assert closed.status_code == 200 and closed.json()["ok"]
            assert hub_view(ws, lambda v: not v["sessions"])["sessions"] == []


def test_building_the_hub_never_fans_out_to_the_host(tmp_path):   # D19
    provider = Provider()
    with TestClient(build(tmp_path, provider)) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            hub_view(ws)
            tc.post("/api/cmd", json={"cmd": "session.open", "args": {"ref": "g/p!1"}})
            view = hub_view(ws, lambda v: v["sessions"])

            assert provider.calls["summary"] == 0 and provider.calls["threads"] == 0
            assert view["sessions"][0]["host_checked"] is False
            assert view["sessions"][0]["state"] == "new"
            assert view["host_checked_at"] == ""


def test_refresh_is_what_prices_in_the_host(tmp_path):
    provider = Provider(unresolved=3)
    with TestClient(build(tmp_path, provider)) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            hub_view(ws)
            tc.post("/api/cmd", json={"cmd": "session.open", "args": {"ref": "g/p!1"}})
            hub_view(ws, lambda v: v["sessions"])

            assert tc.post("/api/cmd", json={"cmd": "hub.refresh"}).json()["ok"]
            view = hub_view(ws, lambda v: v["sessions"] and v["sessions"][0]["host_checked"])
            assert provider.calls["summary"] == 1 and provider.calls["threads"] == 1
            assert view["sessions"][0]["unresolved"] == 3
            assert view["sessions"][0]["state"] == "discussions"
            assert view["host_checked_at"]


def test_an_unknown_scope_is_reported_without_dropping_the_stream(tmp_path):
    with TestClient(build(tmp_path, Provider())) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["nope"]})
            err = read_until(ws, lambda m: m["type"] == "error")
            assert err["scope"] == "nope"
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            assert hub_view(ws)["user"] == "reviewer"


def test_a_malformed_frame_is_answered_and_the_stream_survives(tmp_path):
    with TestClient(build(tmp_path, Provider())) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "nonsense"})
            err = read_until(ws, lambda m: m["type"] == "error")
            assert "nonsense" in err["reason"]
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            assert hub_view(ws)["sessions"] == []


@pytest.mark.parametrize("body,expected", [
    ({"cmd": "nope"}, 400),
    ({"cmd": "session.close", "args": {}}, 400),
    ({"cmd": "session.close", "args": {"id": "ghost"}}, 404),
    ({"cmd": "session.open", "args": {"ref": 5}}, 400),
    ({"cmd": "session.open", "args": {"ref": {"bogus": 1}}}, 400),
])
def test_command_errors_are_reported_as_status_codes(tmp_path, body, expected):
    with TestClient(build(tmp_path, Provider())) as tc:
        r = tc.post("/api/cmd", json=body)
        assert r.status_code == expected
        assert r.json()["ok"] is False
