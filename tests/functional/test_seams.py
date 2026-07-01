"""The host seam: a fake MRSource loaded through the manager populates session state.

Exercises the seam Protocol and the SYSTEM-origin apply path (ApplyMRMetadata/ApplyFiles).
"""
import pytest

from review_mate.seams import MRSource, MRPayload, MRRef
from review_mate.session.manager import SessionManager
from review_mate.session.state import MRMetadata, FileEntry, ChangeType


class FakeMRSource:
    """Implements the MRSource Protocol structurally."""
    async def load(self, ref: MRRef) -> MRPayload:
        return MRPayload(
            mr=MRMetadata(host="gitlab", project=ref.project, iid=ref.iid, title="T",
                          source_branch="x", target_branch="main", sha="abc",
                          author="dev", url="http://x"),
            files=[FileEntry(path="a.py", change_type=ChangeType.MODIFIED)],
            threads=[],
        )

    async def fetch_threads(self, ref: MRRef):
        return []


def test_fake_satisfies_protocol():
    assert isinstance(FakeMRSource(), MRSource)


@pytest.fixture
async def manager(tmp_path):
    m = SessionManager(root=tmp_path / "sessions", mr_source=FakeMRSource())
    yield m
    await m.shutdown()


async def test_load_populates_session_via_seam(manager):
    sid = await manager.create()
    ref = MRRef(host="gitlab", project="g/p", iid=42)
    await manager.load(sid, ref)
    snap = manager.get(sid).snapshot()
    assert snap.mr is not None and snap.mr.iid == 42
    assert [f.path for f in snap.files] == ["a.py"]
