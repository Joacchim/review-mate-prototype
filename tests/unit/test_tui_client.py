"""The TUI client's half of the protocol: absorbing messages, and nothing else."""
import json

from review_mate.tui.client import ViewClient


def client():
    return ViewClient("http://127.0.0.1:9999")


def test_ws_url_follows_the_base_scheme():
    assert client().ws_url == "ws://127.0.0.1:9999/api/stream"
    assert ViewClient("https://host.example").ws_url == "wss://host.example/api/stream"


def test_a_topic_message_replaces_the_view_wholesale():
    c = client()
    c._absorb(json.dumps({"type": "topic", "topic": "hub", "seq": 4,
                          "view": {"sessions": [1, 2]}}))
    c._absorb(json.dumps({"type": "topic", "topic": "hub", "seq": 5, "view": {"sessions": []}}))
    assert c.views["hub"] == {"sessions": []} and c.seqs["hub"] == 5


def test_a_topic_error_is_recorded_and_cleared_by_the_next_good_view():
    c = client()
    c._absorb(json.dumps({"type": "error", "topic": "hub", "reason": "host down"}))
    assert c.errors["hub"] == "host down"
    c._absorb(json.dumps({"type": "topic", "topic": "hub", "seq": 1, "view": {}}))
    assert "hub" not in c.errors


def test_an_unreadable_frame_is_dropped_not_fatal():
    c = client()
    c._absorb("{not json")
    c._absorb(json.dumps({"type": "topic", "topic": "hub", "seq": 1, "view": {"user": "u"}}))
    assert c.views["hub"]["user"] == "u"
