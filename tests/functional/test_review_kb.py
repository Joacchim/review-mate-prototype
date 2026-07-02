"""Functional tests for the user-wide review knowledge base."""
from review_mate.kb.store import ReviewKB, RepoInfo


def test_record_repo_persists(tmp_path):  # AC-1, 4
    kb = ReviewKB(root=tmp_path / "home")
    kb.record_repo("g/p", group="g", purpose="control plane", mirror_path="/m/p.git")
    again = ReviewKB(root=tmp_path / "home")  # fresh instance reads from disk
    info = again.get_repo("g/p")
    assert info is not None and info.purpose == "control plane"
    assert (tmp_path / "home" / "knowledge" / "knowledge.json").exists()


def test_relationship_and_related(tmp_path):  # AC-2
    kb = ReviewKB(root=tmp_path / "home")
    kb.record_relationship("g/orchestrator", "g/control-plane", note="uses the MR API")
    assert "g/control-plane" in kb.related("g/orchestrator")


def test_prefs_persist(tmp_path):  # AC-3
    kb = ReviewKB(root=tmp_path / "home")
    kb.set_pref("seed_from_local", "ask")
    assert ReviewKB(root=tmp_path / "home").get_pref("seed_from_local") == "ask"


def test_idempotent_recording(tmp_path):  # AC-5
    kb = ReviewKB(root=tmp_path / "home")
    kb.record_repo("g/p", purpose="one")
    kb.record_repo("g/p", purpose="two")
    kb.record_relationship("a", "b")
    kb.record_relationship("a", "b")
    assert len([r for r in kb.list_repos() if r.project == "g/p"]) == 1
    assert kb.related("a") == ["b"]


def test_seed_preserves_enrichment(tmp_path):  # AC-6
    kb = ReviewKB(root=tmp_path / "home")
    kb.record_repo("g/p", purpose="hand-written purpose")
    kb.seed([RepoInfo(project="g/p", group="g", purpose="", mirror_path=""),
             RepoInfo(project="g/q", group="g", purpose="seeded", mirror_path="")])
    assert kb.get_repo("g/p").purpose == "hand-written purpose"  # not clobbered
    assert kb.get_repo("g/q").purpose == "seeded"               # new one added


def test_watermark_roundtrip_and_persists(tmp_path):  # diff-versions
    kb = ReviewKB(root=tmp_path / "home")
    assert kb.get_watermark("gitlab", "g/p", 42) is None
    kb.set_watermark("gitlab", "g/p", 42, "sha-abc")
    assert kb.get_watermark("gitlab", "g/p", 42) == "sha-abc"
    # a fresh store over the same root reads the persisted watermark
    assert ReviewKB(root=tmp_path / "home").get_watermark("gitlab", "g/p", 42) == "sha-abc"
    # keyed per MR — a different iid is independent
    assert kb.get_watermark("gitlab", "g/p", 43) is None
