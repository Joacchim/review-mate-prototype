"""A host stub for the fixture server: enough for the queue, and able to fail on request."""
from __future__ import annotations


class StubHost:
    username = "reviewer"

    def __init__(self, queue=None) -> None:
        self.queue = list(queue or [])
        self.files: dict[str, str] = {}
        self.blame_lines: list[dict] = []
        self.issues: list[dict] = []
        self.versions: list[dict] = []
        self.commit_list: list[dict] = []
        self.commit_files: dict[str, list] = {}
        self.repo_tree: list[str] = []
        self.fail_with: Exception | None = None
        self.search_hits: list[dict] = []
        self.search_fails: Exception | None = None
        self.calls = 0

    async def review_queue_items(self):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.queue)

    async def search(self, query: str):
        if self.search_fails is not None:
            raise self.search_fails
        return list(self.search_hits)

    async def mr_versions(self, ref):
        return list(self.versions)

    async def commits(self, ref):
        return list(self.commit_list)

    async def commit_diff(self, ref, sha: str):
        return list(self.commit_files.get(sha, []))

    async def blame(self, project: str, path: str, ref: str, start: int, end: int):
        """Last-touch for a line range — the cheap context tier the rail folds in."""
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.blame_lines)

    async def linked_issues(self, project: str, iid):
        return list(self.issues)

    async def get_repo_tree(self, project: str, ref: str, max_pages: int = 30) -> list[str]:
        """Every path in the repository, for browsing beyond the diff."""
        return list(self.repo_tree)

    async def get_file(self, project: str, path: str, ref: str) -> str:
        """Whole-file content, which unfolding context and the markdown view both read."""
        if self.fail_with is not None:
            raise self.fail_with
        return self.files.get(path, "")
