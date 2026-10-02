"""The consent path end to end through the real app: approving materializes the repository.

The gap this closes is not a behaviour but a wire. `CrossRepoBroker` honoured the consent invariant
from the day it was written and was never constructed outside its own tests, so an approval recorded
the reviewer's answer and nothing acted on it. A unit test of the broker cannot notice that; only
building the real app and approving through it can.
"""
import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from conftest import HostStub
from review_mate.contracts import MRRef
from review_mate.server.app import create_app
from review_mate.session.commands import DecideAccess, RequestAccess
from review_mate.session.manager import SessionManager
from review_mate.session.state import Origin
from review_mate.workspace.manager import WorkspaceManager

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "")}


@pytest.fixture
def sibling(tmp_path):
    src = tmp_path / "sibling"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=src, check=True, capture_output=True, env=_ENV)
    (src / "contract.md").write_text("readiness contract\n")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True, env=_ENV)
    subprocess.run(["git", "commit", "-m", "x"], cwd=src, check=True, capture_output=True, env=_ENV)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True,
                         env=_ENV).stdout.strip()
    return str(src), sha


class LocatingHost(HostStub):
    """A host that can say where a repository lives — what makes a grant possible at all."""

    def __init__(self, clone_url="", ref="", **kwargs):
        super().__init__(**kwargs)
        self._clone_url, self._ref = clone_url, ref

    async def locate_repo(self, name: str):
        if not self._clone_url:
            return None
        return {"host": "local", "project": name, "clone_url": self._clone_url, "ref": self._ref}


async def _approved_through_the_app(tmp_path, clone_url, ref, watch=True):
    """Build the real app, ask for access as the agent, approve as the reviewer, return the state."""
    provider = LocatingHost(clone_url=clone_url, ref=ref)
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider,
                             workspace=WorkspaceManager(root=tmp_path / "home"))
    app = create_app(manager=manager, with_mcp=False, provider=provider, kb=_kb(tmp_path))
    async with app.router.lifespan_context(app):
        sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
        writer = manager.get(sid)
        await writer.submit(RequestAccess(repo="g/sibling", reason="the contract"), Origin.AGENT)
        rid = writer.snapshot().access_requests[-1].id

        bus = app.state.bus
        async with bus.connect() as sub:
            if watch:
                await bus.subscribe(sub, [f"access:{sid}"])   # a reviewer opens the consent list
                await asyncio.sleep(0)
            await writer.submit(DecideAccess(request_id=rid, approve=True), Origin.BROWSER)
            for _ in range(200):
                grant = writer.snapshot().access_requests[-1].grant
                if grant is not None and grant.state in ("ready", "failed"):
                    break
                await asyncio.sleep(0.02)
            return writer.snapshot().access_requests[-1]


def _kb(tmp_path):
    from review_mate.kb.store import ReviewKB
    return ReviewKB(root=tmp_path / "home")


async def test_approving_in_the_real_app_materializes_the_repository(tmp_path, sibling):
    clone_url, sha = sibling
    req = await _approved_through_the_app(tmp_path, clone_url, sha)
    assert req.grant is not None, "an approval nothing acts on is the bug this closes"
    assert req.grant.state == "ready", req.grant
    assert (Path(req.grant.path) / "contract.md").exists(), "the repository is actually on disk"


async def test_a_repository_the_host_cannot_place_fails_visibly(tmp_path):
    req = await _approved_through_the_app(tmp_path, clone_url="", ref="")
    assert req.grant is not None and req.grant.state == "failed"
    assert "g/sibling" in req.grant.error
