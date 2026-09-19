"""A host stub for the fixture server: enough for the queue, and able to fail on request."""
from __future__ import annotations


class StubHost:
    username = "reviewer"

    def __init__(self, queue=None) -> None:
        self.queue = list(queue or [])
        self.fail_with: Exception | None = None
        self.calls = 0

    async def review_queue_items(self):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.queue)

    async def search(self, query: str):
        return []
