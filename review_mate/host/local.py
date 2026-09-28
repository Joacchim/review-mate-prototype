"""Reviewing a branch that is still on this machine.

The other thing worth reviewing: work an agent has just produced, before anyone else is asked to
look at it. Same review — the diff, the rail, highlights, the chat — against a branch in the
working repository instead of a merge request on a forge.

It is a provider, not a mode. `MRSource` is two methods, and everything downstream already turns
itself off on capabilities, so a local branch simply advertises less: no threads to mirror, no
approval to record, no comment to post. The review channel, the approval bar and the discussion list
disappear on their own, because each was already asking whether the host could do that.

Two things differ from a forge in ways that matter rather than degrade:

- **The diff is `base...head`**, taken from where the branch left its base, which is what a merge
  request shows. Not `base..head`: a base that has moved on is not the author's work.
- **Nothing is materialized.** Every other checkout in review-mate is a detached worktree at a fixed
  sha, because nothing was going to write to it. Here the agent has to edit the code it is being
  asked about, so the session points at the working repository itself and the head moves under it —
  sometimes with no event, because a commit is not something the session was asked to do.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from review_mate.contracts import LocalRef, MRPayload
from review_mate.session.state import ChangeType, FileEntry, MRMetadata

# What a branch on disk can offer. Read as a subset of GITLAB_CAPABILITIES: what is missing is
# missing because there is no forge to ask, not because it is unimplemented.
LOCAL_CAPABILITIES: dict[str, bool] = {
    "commits": True,          # a branch has its own commits, and they step the same way
    "diff_versions": False,   # "since you last looked" needs a forge's versions; git has no record
    "threads": False,         # nobody else is here to discuss it with
    "approvals": False,       # there is nothing to approve yet
    "inline_comments": False,
    "mr_comments": False,
    "suggestions": False,
    "draft_reviews": False,
}


# git's own field separators, so a commit message containing newlines or tabs stays one field
_UNIT = "\x1f"
_RECORD = "\x1e"
# what git diffs a root commit against; `git hash-object -t tree /dev/null` on any repository
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class GitError(RuntimeError):
    """A git command that failed, carrying what git said rather than a return code."""


class LocalBranchProvider:
    """Loads a branch from a repository on this machine. Reads git and nothing else."""

    host = "local"

    def capabilities(self) -> dict[str, bool]:
        return dict(LOCAL_CAPABILITIES)

    async def load(self, ref: LocalRef) -> MRPayload:
        repo = Path(ref.path)
        if not (repo / ".git").exists() and not (repo / "HEAD").exists():
            raise GitError(f"not a git repository: {ref.path}")
        base = ref.base or await self._default_branch(repo)
        head = await self._git(repo, "rev-parse", ref.branch)
        fork = await self._fork_point(repo, base, ref.branch)
        subject = await self._git(repo, "log", "-1", "--format=%s", ref.branch)
        author = await self._git(repo, "log", "-1", "--format=%an", ref.branch)

        return MRPayload(
            mr=MRMetadata(
                host=self.host, project=repo.name, iid=0,
                title=subject or ref.branch,
                source_branch=ref.branch, target_branch=base,
                sha=head, author=author or "", url=str(repo),
                clone_url=str(repo),
                capabilities=self.capabilities(),
                # the endpoints an agent needs to diff any pair itself, exactly as a forge gives them
                diff_refs={"base_sha": fork, "head_sha": head, "start_sha": fork},
            ),
            files=await self._files(repo, fork, head),
            threads=[],
            clone_url=str(repo),
            checkout_path=str(repo),   # it is already here, and the agent is about to edit it
        )

    async def fetch_threads(self, ref: LocalRef) -> list:
        return []      # nobody else is here yet; that is the point of reviewing it now

    # --- stepping through the branch one commit at a time ---------------------

    async def commits(self, ref: LocalRef) -> list[dict]:
        """The commits the branch added, newest first — the shape a forge returns.

        `base..branch` and not `base...branch`: what is wanted here is the commits *this branch*
        added, and a symmetric range would sweep in whatever the base has done since. The diff uses
        the three-dot form for the opposite reason — it wants the change as a whole, from the fork.
        """
        repo = Path(ref.path)
        base = ref.base or await self._default_branch(repo)
        raw = await self._git(repo, "log", f"{base}..{ref.branch}",
                              f"--format=%H{_UNIT}%h{_UNIT}%s{_UNIT}%B{_UNIT}%an{_UNIT}%aI{_RECORD}")
        rows = []
        for record in raw.split(_RECORD):
            fields = record.strip("\n").split(_UNIT)
            if len(fields) < 6 or not fields[0]:
                continue
            rows.append({"sha": fields[0], "short_id": fields[1], "title": fields[2],
                         "message": fields[3], "author": fields[4], "created_at": fields[5]})
        return rows

    async def commit_diff(self, ref: LocalRef, sha: str) -> list[FileEntry]:
        """One commit against its parent, shaped like the whole change's files.

        A root commit has no parent, so it is diffed against the empty tree rather than failing —
        the first commit of a branch is exactly the one a reviewer wants to read first.
        """
        repo = Path(ref.path)
        parent = await self._parent_of(repo, sha)
        return await self._files(repo, parent, sha)

    async def _parent_of(self, repo: Path, sha: str) -> str:
        try:
            return await self._git(repo, "rev-parse", f"{sha}^")
        except GitError:
            return _EMPTY_TREE

    # --- git ------------------------------------------------------------------

    async def _default_branch(self, repo: Path) -> str:
        """What the branch will merge into, when the reviewer did not say.

        `origin/HEAD` is the honest answer and is usually set by a clone. A repository where it is
        not gets the conventional names tried in turn, and an error rather than a guess if neither
        exists — reviewing against the wrong base produces a diff full of other people's work.
        """
        try:
            symbolic = await self._git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            return symbolic.partition("origin/")[2] or symbolic
        except GitError:
            pass
        for name in ("main", "master"):
            try:
                await self._git(repo, "rev-parse", "--verify", name)
                return name
            except GitError:
                continue
        raise GitError("cannot tell what this branch merges into — name a base")

    async def _fork_point(self, repo: Path, base: str, branch: str) -> str:
        """Where the branch left its base. A merge request diffs from here, not from the base's tip:
        a base that moved on carries other people's commits, and they are not under review."""
        return await self._git(repo, "merge-base", base, branch)

    async def _files(self, repo: Path, fork: str, head: str) -> list[FileEntry]:
        names = await self._git(repo, "diff", "--name-status", "-z", fork, head)
        entries: list[FileEntry] = []
        for status, path, old in _name_status(names):
            text = await self._git(repo, "diff", "--unified=3", fork, head, "--", path)
            entries.append(FileEntry(path=path, old_path=old, change_type=status,
                                     hunks=[{"diff": _body(text)}]))
        return entries

    @staticmethod
    async def _git(repo: Path, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate()
        if process.returncode != 0:
            raise GitError((err.decode() or out.decode()).strip() or f"git {args[0]} failed")
        return out.decode().strip()


def _name_status(raw: str):
    """`--name-status -z` as (change_type, path, old_path) — renames carry two paths, not one."""
    fields = [f for f in raw.split("\0") if f]
    index = 0
    while index < len(fields):
        code = fields[index]
        letter = code[0]
        if letter == "R" and index + 2 < len(fields):
            yield ChangeType.RENAMED, fields[index + 2], fields[index + 1]
            index += 3
            continue
        if index + 1 >= len(fields):
            return
        path = fields[index + 1]
        yield {"A": ChangeType.ADDED, "D": ChangeType.DELETED}.get(letter, ChangeType.MODIFIED), \
            path, None
        index += 2


def _body(text: str) -> str:
    """The hunks of a file's diff, without git's header — the same shape a forge hands over."""
    marker = text.find("\n@@")
    return text[marker + 1:] if marker != -1 else ""
