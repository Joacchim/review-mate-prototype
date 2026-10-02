"""Every forge this server is configured for, addressed by the host a reference names.

Deliberately thin. It resolves *which* forge answers for a reference and otherwise stays out of the
way: callers then hold a real provider and keep asking it what it can do. A router that implemented
the whole surface and delegated would answer `hasattr` for every method whether or not the forge
behind it had one, and those guards are how a forge that cannot version a diff or mirror a thread
turns those features off — a local branch already relies on exactly that.

Three calls are not about one change at all. The review queue, a search and a repository lookup ask
about everywhere the reviewer works, so they are asked of every forge and the answers merged; they
are the only behaviour here.

**A single forge answers for every host.** One configured forge behaves exactly as it did before
there was a router, a stub provider needs no host to be injected in a test, and routing only starts
discriminating once there is something to discriminate between.
"""
from __future__ import annotations


class Forges:
    def __init__(self, by_host: dict[str, object]) -> None:
        self._by_host = {h: p for h, p in by_host.items() if p is not None}

    @classmethod
    def of(cls, provider) -> "Forges | None":
        """A router, a bare provider, or nothing — so a caller holding one need not care which."""
        if provider is None or isinstance(provider, cls):
            return provider
        return cls({getattr(provider, "host", "") or "": provider})

    def __bool__(self) -> bool:
        return bool(self._by_host)

    def all(self) -> list:
        return list(self._by_host.values())

    def pick(self, host: str | None):
        """The forge serving `host`, or None.

        A forge that did not load this review must not answer for it — that is how a branch on this
        machine, whose host is `local`, gets no diff versions and no threads from a GitLab server
        that happens to be configured. So a known host that matches nothing here resolves to
        nothing, and the caller degrades exactly as it did before there was a router.

        The one forge that answers for anything is one that never said what host it serves: a stub
        injected in a test, which is the only way that happens.
        """
        if host and host in self._by_host:
            return self._by_host[host]
        if len(self._by_host) == 1:
            only = next(iter(self._by_host.values()))
            if not getattr(only, "host", ""):
                return only          # it claims no host, so it is not claiming not to serve this
        return None

    def can(self, method: str) -> bool:
        """Whether any configured forge offers `method` at all.

        For the decisions taken once at wiring time rather than per review — whether to stand up
        the cross-repo broker, say. Per review the question is different and is asked of the forge
        itself, because what one can do says nothing about the other.
        """
        return any(hasattr(provider, method) for provider in self.all())

    def for_session(self, snapshot):
        """The forge that loaded this review, read off the review itself."""
        mr = getattr(snapshot, "mr", None) if snapshot is not None else None
        return self.pick(getattr(mr, "host", None))

    # --- everywhere the reviewer works, rather than one change ------------------------------

    async def review_queue_items(self) -> list[dict]:
        out: list[dict] = []
        for provider in self.all():
            if hasattr(provider, "review_queue_items"):
                out.extend(await provider.review_queue_items())
        return out

    async def search(self, query: str, limit: int = 15) -> list[dict]:
        out: list[dict] = []
        for provider in self.all():
            if hasattr(provider, "search"):
                out.extend(await provider.search(query, limit))
        return out

    async def locate_repo(self, name: str) -> dict | None:
        for provider in self.all():
            if hasattr(provider, "locate_repo"):
                found = await provider.locate_repo(name)
                if found is not None:
                    return found
        return None
