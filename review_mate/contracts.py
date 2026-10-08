"""Contracts — the boundaries other review-mate units fill.

bridge-server defines these Protocols and data shapes; it never implements them. The host adapter
(`gitlab-host-adapter`) implements `MRSource`; the workspace unit (`workspace-manager`) implements
`Workspace`. They are injected into `SessionManager`, so the spine has no compile-time dependency
on them (AC-12). The agent contract (`mcp-bridge`) is simply the in-process `SessionManager` +
`SessionWriter.submit/subscribe` surface, so it needs no Protocol here.
"""
from __future__ import annotations

from typing import Protocol, Union, runtime_checkable

from pydantic import BaseModel

from review_mate.session.state import FileEntry, MRMetadata, ReviewThread


class MRRef(BaseModel):
    """A reference resolving to one merge request."""
    host: str
    project: str
    iid: int


class LocalRef(BaseModel):
    """A branch in a repository on this machine, reviewed against where it left its base.

    The other kind of thing worth reviewing. It is not an MRRef with the fields left blank: a branch
    has no merge-request number, and inventing one would put a fiction in the hub, the logs and the
    watermark key where it would read as a real one. What identifies it is the repository it lives
    in and its name.

    `base` is the branch it will eventually merge into. Empty means the repository's default, which
    is what a reviewer means when they do not say.
    """
    path: str                    # the working repository — not a checkout of it
    branch: str
    base: str = ""


# What a session can be about. A provider handles the kind it understands and no other.
SessionRef = Union[MRRef, LocalRef]


def ref_of(snapshot) -> "SessionRef | None":
    """The reference a session was opened with, rebuilt from what it applied.

    Topics reach for the provider with an address, and there is now more than one kind. The session
    does not store its reference, but the metadata carries everything either kind needs — which is
    why this can be rebuilt rather than kept: a second copy of the address would be one more thing
    to keep in step with a re-sync.
    """
    mr = getattr(snapshot, "mr", None)
    if mr is None:
        return None
    if mr.host == "local":
        return LocalRef(path=mr.clone_url, branch=mr.source_branch, base=mr.target_branch)
    return MRRef(host=mr.host, project=mr.project, iid=mr.iid)


def serves(provider, snapshot) -> bool:
    """Whether `provider` is the source this session was loaded from.

    A topic holds one provider for every session on the server, which was harmless while there was
    one kind of session. It is not any more: asking the forge to blame a file in a branch that has
    never left this machine sends a local directory name to a remote API, and the reviewer gets an
    error where the honest answer is that this host has nothing to say about that review.

    A provider that does not name a host serves everything, which keeps every stub and fake working
    without having to know about this.
    """
    host = getattr(provider, "host", None)
    if provider is None or host is None:
        return provider is not None
    return snapshot is not None and snapshot.mr is not None and snapshot.mr.host == host


class MRPayload(BaseModel):
    """What a host returns for an MR — the data the loader applies into a session."""
    mr: MRMetadata
    files: list[FileEntry]
    threads: list[ReviewThread] = []
    clone_url: str = ""   # so workspace-manager can materialize the checkout
    checkout_path: str = ""   # the code is already on disk here — do not materialize a copy of it


class RepoRef(BaseModel):
    host: str
    project: str
    clone_url: str


class CheckoutHandle(BaseModel):
    """An isolated checkout materialized under ~/.review-mate/ (the workspace boundary)."""
    repo: str
    commit: str
    path: str


@runtime_checkable
class MRSource(Protocol):
    """Host contract → gitlab-host-adapter."""
    async def load(self, ref: MRRef) -> MRPayload: ...
    async def fetch_threads(self, ref: MRRef) -> list[ReviewThread]: ...


class RepoUnreadable(RuntimeError):
    """git could not reach the repository — credentials or the network, not the review.

    Worth its own type because it is the one git failure a reviewer can act on, and because it is
    the one that looks like something else: the forge half of a review keeps working, so the merge
    request loads and only the parts read from the repository are missing.
    """


@runtime_checkable
class Workspace(Protocol):
    """Workspace contract → workspace-manager."""
    async def materialize(self, repo: RepoRef, commit: str) -> CheckoutHandle: ...
