"""WorkspaceManager — isolated git checkouts under ~/.review-mate/, never touching user clones.

A bare, blobless mirror per repo (cloned once, reused), with a detached `git worktree` per
checkout at the requested commit. An optional local clone may be reused read-only as a `--reference`
seed. Shells out to the system `git`, inheriting ambient credentials (D8). Implements `Workspace`.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from review_mate.config import review_mate_home
from review_mate.seams import CheckoutHandle, RepoRef


class WorkspaceManager:
    def __init__(self, root: Path | str | None = None, seeds: dict[str, str] | None = None):
        self.root = Path(root) if root is not None else review_mate_home()
        self.seeds = seeds or {}
        (self.root / "mirrors").mkdir(parents=True, exist_ok=True)
        (self.root / "checkouts").mkdir(parents=True, exist_ok=True)
        self._mirror_of: dict[str, Path] = {}  # checkout path -> mirror path

    # --- Workspace seam -----------------------------------------------------

    async def materialize(self, repo: RepoRef, commit: str) -> CheckoutHandle:
        mirror = await self._ensure_mirror(repo)
        await self._ensure_commit(mirror, commit)
        path = self.root / "checkouts" / _key(repo) / commit[:12]
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            await self._git("-C", str(mirror), "worktree", "add", "--detach",
                            str(path), commit)
        self._mirror_of[str(path)] = mirror
        return CheckoutHandle(repo=repo.project, commit=commit, path=str(path))

    async def release(self, handle: CheckoutHandle) -> None:
        path = Path(handle.path)
        mirror = self._mirror_of.pop(handle.path, None)
        if mirror is not None:
            await self._git("-C", str(mirror), "worktree", "remove", "--force", str(path))
        elif path.exists():  # fallback: drop the dir and prune dangling worktrees
            shutil.rmtree(path, ignore_errors=True)

    async def range_diff(self, repo: RepoRef, old_base: str, old_head: str,
                         new_base: str, new_head: str) -> str:
        """A base-aware interdiff (diff-versions): how the branch's changeset evolved between two
        versions, transparently ignoring target-base movement. A pure rebase yields an empty result.
        Runs `git range-diff old_base..old_head new_base..new_head` in the bare mirror."""
        mirror = await self._ensure_mirror(repo)
        for sha in (old_base, old_head, new_base, new_head):
            await self._ensure_commit(mirror, sha)
        return await self._git("-C", str(mirror), "range-diff",
                               f"{old_base}..{old_head}", f"{new_base}..{new_head}")

    # --- internals ----------------------------------------------------------

    def mirror_path(self, repo: RepoRef) -> Path:
        return self.root / "mirrors" / f"{_key(repo)}.git"

    async def _ensure_mirror(self, repo: RepoRef) -> Path:
        mirror = self.mirror_path(repo)
        if mirror.exists():
            return mirror
        # Clone into a .tmp sibling and rename on success, so a failed/interrupted clone never
        # leaves a poisoned empty mirror that mirror.exists() would then treat as complete forever.
        tmp = mirror.with_name(mirror.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        args = ["clone", "--bare", "--filter=blob:none"]
        seed = self.seeds.get(_key(repo))
        if seed:
            args += ["--reference", seed]
        args += [repo.clone_url, str(tmp)]
        try:
            await self._git(*args)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        tmp.replace(mirror)
        return mirror

    async def _ensure_commit(self, mirror: Path, commit: str) -> None:
        if await self._has_commit(mirror, commit):
            return
        try:  # try to fetch the specific commit/ref into the mirror
            await self._git("-C", str(mirror), "fetch", "origin", commit)
        except RuntimeError:
            pass
        if not await self._has_commit(mirror, commit):
            raise RuntimeError(f"commit not available in mirror: {commit}")

    async def _has_commit(self, mirror: Path, commit: str) -> bool:
        try:
            await self._git("-C", str(mirror), "cat-file", "-e", f"{commit}^{{commit}}")
            return True
        except RuntimeError:
            return False

    async def _git(self, *args: str, timeout: float = 120.0) -> str:
        # Never let git block on an interactive credential prompt (no ambient creds → the request
        # would hang forever). Fail fast and bounded: prompts off, stdin closed, hard timeout.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"git {' '.join(args)} timed out after {timeout:.0f}s")
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {err.decode().strip()}")
        return out.decode()


def _key(repo: RepoRef) -> str:
    raw = f"{repo.host}__{repo.project}"
    return "".join(c if c.isalnum() or c in "_-." else "_" for c in raw)
