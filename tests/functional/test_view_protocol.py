"""The view protocol end to end: subscribe to a scope, act through /api/cmd, see the scope again.

The behavioural guarantee these pin down is the one a second client would otherwise break:
building the hub is local, and the per-review host fan-out happens only on an explicit
`hub.refresh` (D19).
"""
import asyncio
import json

import pytest
from starlette.testclient import TestClient

from review_mate.contracts import MRRef
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.view.hub import HubScope

from conftest import HostStub, next_frame


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
    with TestClient(build(tmp_path, HostStub())) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            view = hub_view(ws)
            assert view["sessions"] == []
            assert view["user"] == "reviewer"
            assert view["queue_state"] in ("loading", "ready")


def test_the_queue_arrives_in_the_view(tmp_path):
    with TestClient(build(tmp_path, HostStub())) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            view = hub_view(ws, lambda v: v["queue_state"] == "ready")
            assert view["queue"] == [{"host": "gitlab", "project": "g/p", "iid": 7,
                                      "title": "queued", "url": "http://q"}]


def test_a_failing_queue_read_is_a_field_not_a_broken_view(tmp_path):
    provider = HostStub(queue_error=RuntimeError("gitlab 503"))
    with TestClient(build(tmp_path, provider)) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            view = hub_view(ws, lambda v: v["queue_state"] == "error")
            assert "gitlab 503" in view["queue_error"]
            assert view["sessions"] == []          # the local half is unaffected


async def test_the_queue_reads_as_loading_until_the_host_answers(tmp_path):
    """The ordering guarantee, away from the transport where it would be a race."""
    gate = asyncio.Event()
    provider = HostStub()

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
    with TestClient(build(tmp_path, HostStub())) as tc:
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
    provider = HostStub()
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
    provider = HostStub(unresolved=3)
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
    with TestClient(build(tmp_path, HostStub())) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["nope"]})
            err = read_until(ws, lambda m: m["type"] == "error")
            assert err["scope"] == "nope"
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            assert hub_view(ws)["user"] == "reviewer"


def test_a_malformed_frame_is_answered_and_the_stream_survives(tmp_path):
    with TestClient(build(tmp_path, HostStub())) as tc:
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
    with TestClient(build(tmp_path, HostStub())) as tc:
        r = tc.post("/api/cmd", json=body)
        assert r.status_code == expected
        assert r.json()["ok"] is False


def test_reviews_arrive_newest_first_and_carry_their_counts(tmp_path):
    """Row order and the per-review counts are the server's call, so every client agrees."""
    with TestClient(build(tmp_path, HostStub())) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            hub_view(ws)

            first = tc.post("/api/cmd", json={"cmd": "session.open",
                                              "args": {"ref": "g/p!1"}}).json()["session"]
            second = tc.post("/api/cmd", json={"cmd": "session.open",
                                               "args": {"ref": "g/p!2"}}).json()["session"]
            tc.post(f"/api/sessions/{first}/commands",
                    json={"type": "add_highlight", "file": "a.py", "side": "new",
                          "line_range": {"start": 1, "end": 2}})
            tc.post("/api/cmd", json={"cmd": "hub.refresh"})

            view = hub_view(ws, lambda v: len(v["sessions"]) == 2
                            and any(s["highlights"] for s in v["sessions"]))
            ids = [s["id"] for s in view["sessions"]]
            assert ids == [second, first]        # newest first, decided server-side
            by_id = {s["id"]: s for s in view["sessions"]}
            assert by_id[first]["highlights"] == 1 and by_id[second]["highlights"] == 0
            assert all(s["created_at"] for s in view["sessions"])


def test_no_host_means_an_empty_queue_not_an_error(tmp_path):
    manager = SessionManager(root=tmp_path / "sessions")
    with TestClient(create_app(manager=manager, with_mcp=False)) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            view = hub_view(ws, lambda v: v["queue_state"] == "ready")
            assert view["queue"] == [] and view["queue_error"] == ""


def test_a_host_without_mr_summary_falls_back_to_versions(tmp_path):
    """The watermark comparison drives `git_update`, whichever host read supplies the head."""
    from review_mate.kb.store import ReviewKB

    class VersionsOnly:
        username = "reviewer"

        async def load(self, ref):
            return await HostStub().load(ref)

        async def review_queue_items(self):
            return []

        async def mr_versions(self, ref):
            return [{"base_sha": "b", "head_sha": "HEAD2", "start_sha": "", "created_at": ""}]

    kb = ReviewKB(root=tmp_path / "kb")
    provider = VersionsOnly()
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider)
    app = create_app(manager=manager, provider=provider, with_mcp=False, kb=kb,
                     resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))
    with TestClient(app) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            hub_view(ws)
            tc.post("/api/cmd", json={"cmd": "session.open", "args": {"ref": "g/p!1"}})
            view = hub_view(ws, lambda v: v["sessions"])
            kb.set_watermark("gitlab", "g/p", 1, "abc")      # reviewed the head the session loaded

            tc.post("/api/cmd", json={"cmd": "hub.refresh"})
            view = hub_view(ws, lambda v: v["sessions"] and v["sessions"][0]["behind"])
            assert view["sessions"][0]["state"] == "git_update"   # host head moved past the watermark


def test_a_merged_mr_reads_as_merged(tmp_path):
    provider = HostStub(state="merged")
    with TestClient(build(tmp_path, provider)) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            hub_view(ws)
            tc.post("/api/cmd", json={"cmd": "session.open", "args": {"ref": "g/p!1"}})
            hub_view(ws, lambda v: v["sessions"])
            tc.post("/api/cmd", json={"cmd": "hub.refresh"})
            view = hub_view(ws, lambda v: v["sessions"] and v["sessions"][0]["host_checked"])
            assert view["sessions"][0]["state"] == "merged"
            assert view["sessions"][0]["mr_state"] == "merged"


def test_the_hub_says_which_reviews_are_waiting_on_the_agent(tmp_path):
    """The review list is where a reviewer decides what to open next, so it carries the count —
    the same predicate the chat scope publishes per session."""
    app = build(tmp_path, HostStub())
    with TestClient(app) as tc:
        sid = tc.post("/api/cmd", json={"cmd": "session.open",
                                        "args": {"ref": "g/p!1"}}).json()["session"]
        with tc.websocket_connect("/api/stream") as ws:
            # a reviewer with the review open: the tail on its events is what refreshes the hub
            ws.send_json({"action": "subscribe", "scopes": ["hub", f"chat:{sid}"]})
            assert hub_view(ws, lambda v: bool(v["sessions"]))["sessions"][0]["asks"] == 0
            tc.post(f"/api/sessions/{sid}/commands",
                    json={"type": "post_message", "body": "what is this for?"})
            counts = []
            while (frame := next_frame(ws)) is not None:
                if frame.get("scope") == "hub":
                    counts.append(frame["view"]["sessions"][0]["asks"])
            assert counts and counts[-1] == 1


def test_the_hub_carries_whether_an_agent_is_listening_at_all(tmp_path):
    """Presence is a property of the activity stream, so the fleet-wide surface states it once."""
    from datetime import datetime, timezone
    app = build(tmp_path, HostStub())
    with TestClient(app) as tc:
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            assert hub_view(ws)["agent"]["attached"] is False
            # the hub does not tick on its own here: the subscribe below is what re-reads presence
            app.state.activity_broker._last_wait_at = datetime.now(timezone.utc)
            ws.send_json({"action": "subscribe", "scopes": ["hub"]})
            assert hub_view(ws, lambda v: v["agent"]["attached"] is True)["agent"]["parked"] is False
