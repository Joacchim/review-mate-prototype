"""The diff-versions core correctness: a base-aware interdiff (git range-diff) hides rebase noise —
a pure rebase yields no patch content, a real author edit does. Drives WorkspaceManager against a
seeded on-disk git repo (real `git`)."""
import subprocess

from review_mate.seams import RepoRef
from review_mate.server.routes import _interdiff_empty
from review_mate.workspace.manager import WorkspaceManager


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(cwd),
                        "PATH": __import__("os").environ.get("PATH", "")})


def _rev(cwd, ref):
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd, check=True,
                          stdout=subprocess.PIPE).stdout.decode().strip()


def _seed_repo(root):
    """A feature branch, then the target base advances; the feature is rebased onto it (pure), then
    amended with a real edit. Every SHA we need is tagged so the bare clone holds it directly.
    A larger changed region keeps the pre/post patches similar enough for range-diff to match them."""
    src = root / "src"; src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "f.txt").write_text("a\nb\nc\nd\ne\n"); _git(src, "add", "."); _git(src, "commit", "-qm", "base1")
    _git(src, "tag", "t_base1")
    _git(src, "checkout", "-q", "-b", "feature")
    (src / "f.txt").write_text("a\nB\nc\nD\ne\n"); _git(src, "commit", "-qam", "feat: cap B and D")
    _git(src, "tag", "t_head1")
    # the target base advances (an unrelated file) — this is the rebase noise we must ignore
    _git(src, "checkout", "-q", "main")
    (src / "other.txt").write_text("x\n"); _git(src, "add", "."); _git(src, "commit", "-qm", "base2")
    _git(src, "tag", "t_base2")
    # pure rebase: same patch replayed on the new base
    _git(src, "checkout", "-q", "feature"); _git(src, "rebase", "-q", "main")
    _git(src, "tag", "t_pure")
    # a real edit: amend the rebased commit so range-diff matches it as the same commit, evolved
    (src / "f.txt").write_text("a\nBB\nc\nD\ne\n"); _git(src, "commit", "--amend", "-qam", "feat: cap B and D")
    _git(src, "tag", "t_edit")
    return dict(clone_url=str(src), base1=_rev(src, "t_base1"), head1=_rev(src, "t_head1"),
                base2=_rev(src, "t_base2"), head2_pure=_rev(src, "t_pure"), head2_edit=_rev(src, "t_edit"))


async def test_pure_rebase_yields_empty_interdiff(tmp_path):
    s = _seed_repo(tmp_path)
    ws = WorkspaceManager(root=tmp_path / "home")
    repo = RepoRef(host="gitlab", project="g/p", clone_url=s["clone_url"])
    text = await ws.range_diff(repo, s["base1"], s["head1"], s["base2"], s["head2_pure"])
    assert _interdiff_empty(text) is True   # the rebase brought no author change → nothing to review


async def test_real_edit_yields_nonempty_interdiff(tmp_path):
    s = _seed_repo(tmp_path)
    ws = WorkspaceManager(root=tmp_path / "home")
    repo = RepoRef(host="gitlab", project="g/p", clone_url=s["clone_url"])
    text = await ws.range_diff(repo, s["base1"], s["head1"], s["base2"], s["head2_edit"])
    assert _interdiff_empty(text) is False  # the author's edit shows, base movement does not
    assert "BB" in text                     # the actual evolution is present
