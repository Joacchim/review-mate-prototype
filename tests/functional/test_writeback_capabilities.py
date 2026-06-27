"""Every write op is gated on its capability and sends nothing when the capability is off (AC-3)."""
import httpx
import pytest

from review_mate.host.base import GITLAB_CAPABILITIES, CapabilityError
from review_mate.host.gitlab import GitLabWriter
from review_mate.seams import MRRef

REF = MRRef(host="gitlab", project="g/p", iid=1)
POS = {"new_path": "a.py", "new_line": 1}

# (op invocation, the capability it requires)
OPS = [
    ("post_comment", "inline_comments", lambda w: w.post_comment(REF, POS, "b")),
    ("reply", "threads", lambda w: w.reply(REF, "d1", "b")),
    ("resolve", "threads", lambda w: w.resolve(REF, "d1")),
    ("approve", "approvals", lambda w: w.approve(REF)),
    ("suggest", "suggestions", lambda w: w.suggest(REF, POS, "x = 1")),
]


def _writer(calls, **cap_overrides):
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(201, json={"id": "d"})
    caps = dict(GITLAB_CAPABILITIES)
    caps.update(cap_overrides)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://gl/api/v4")
    return GitLabWriter("https://gl/api/v4", "t", caps, client=client)


@pytest.mark.parametrize("name, cap, invoke", OPS)
async def test_missing_capability_blocks_and_sends_nothing(name, cap, invoke):
    calls = []
    writer = _writer(calls, **{cap: False})
    with pytest.raises(CapabilityError):
        await invoke(writer)
    assert calls == []


@pytest.mark.parametrize("name, cap, invoke", OPS)
async def test_present_capability_allows_the_call(name, cap, invoke):
    calls = []
    writer = _writer(calls)  # all caps on
    await invoke(writer)
    assert len(calls) == 1
