"""Functional tests for the consent-gated cross-repo broker (real git target repo)."""
import asyncio
import os
import subprocess

import pytest

from review_mate.crossrepo.broker import CrossRepoBroker
from review_mate.kb.store import ReviewKB
from review_mate.workspace.manager import WorkspaceManager
from review_mate.seams import RepoRef
from review_mate.session.manager import SessionManager
from review_mate.session.commands import ApplyMRMetadata, RequestAccess, DecideAccess
from review_mate.session.state import MRMetadata, Origin


_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "")}


@pytest.fixture
def sibling_repo(tmp_path):
    src = tmp_path / "sibling"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=src, check=True, capture_output=True, env=_ENV)
    (src / "contract.md").write_text("readiness contract\n")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True, env=_ENV)
    subprocess.run(["git", "commit", "-m", "x"], cwd=src, check=True, capture_output=True, env=_ENV)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True,
                         text=True, env=_ENV).stdout.strip()
    return src, sha


@pytest.fixture
async def setup(tmp_path, sibling_repo):
    src, sha = sibling_repo
    manager = SessionManager(root=tmp_path / "home" / "sessions")
    sid = await manager.create()
    actor = manager.get(sid)
    await actor.submit(ApplyMRMetadata(mr=MRMetadata(
        host="local", project="g/main", iid=1, title="t", source_branch="x",
        target_branch="main", sha="z", author="d", url="u")), Origin.SYSTEM)

    def resolve(repo: str):
        return RepoRef(host="local", project=repo, clone_url=str(src)), sha

    broker = CrossRepoBroker(
        manager,
        WorkspaceManager(root=tmp_path / "home"),
        ReviewKB(root=tmp_path / "home"),
        resolve,
    )
    yield manager, actor, sid, broker
    await manager.shutdown()


async def _request(actor, repo="g/sibling"):
    await actor.submit(RequestAccess(repo=repo, reason="contract"), Origin.AGENT)
    return actor.snapshot().access_requests[-1].id


async def test_approved_request_materializes_and_grants(setup):  # AC-1,3,4
    manager, actor, sid, broker = setup
    rid = await _request(actor)
    await actor.submit(DecideAccess(request_id=rid, approve=True), Origin.BROWSER)
    handle = await broker.grant_access(sid, rid)
    assert handle is not None
    from pathlib import Path
    assert (Path(handle.path) / "contract.md").exists()
    assert broker.is_granted(sid, "g/sibling")
    assert "g/sibling" in broker.kb.related("g/main")  # relationship recorded


async def test_denied_request_not_granted(setup):  # AC-2
    manager, actor, sid, broker = setup
    rid = await _request(actor)
    await actor.submit(DecideAccess(request_id=rid, approve=False), Origin.BROWSER)
    assert await broker.grant_access(sid, rid) is None
    assert not broker.is_granted(sid, "g/sibling")


async def test_pending_request_not_granted(setup):  # AC-2
    manager, actor, sid, broker = setup
    rid = await _request(actor)
    assert await broker.grant_access(sid, rid) is None


async def test_watch_processes_approval(setup):  # AC-5
    manager, actor, sid, broker = setup
    task = asyncio.create_task(broker.watch(sid))
    await asyncio.sleep(0)
    rid = await _request(actor)
    await actor.submit(DecideAccess(request_id=rid, approve=True), Origin.BROWSER)
    for _ in range(50):
        if broker.is_granted(sid, "g/sibling"):
            break
        await asyncio.sleep(0.02)
    assert broker.is_granted(sid, "g/sibling")
    task.cancel()
