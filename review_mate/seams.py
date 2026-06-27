"""Seam contracts — the boundaries other review-mate units fill.

bridge-server defines these Protocols and data shapes; it never implements them. The host adapter
(`gitlab-host-adapter`) implements `MRSource`; the workspace unit (`workspace-manager`) implements
`Workspace`. They are injected into `SessionManager`, so the spine has no compile-time dependency
on them (AC-12). The agent seam (`mcp-bridge`) is simply the in-process `SessionManager` +
`SessionActor.submit/subscribe` surface, so it needs no Protocol here.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from review_mate.session.state import FileEntry, MRMetadata, ReviewThread


class MRRef(BaseModel):
    """A reference resolving to one merge request."""
    host: str
    project: str
    iid: int


class MRPayload(BaseModel):
    """What a host returns for an MR — the data the loader applies into a session."""
    mr: MRMetadata
    files: list[FileEntry]
    threads: list[ReviewThread] = []
    clone_url: str = ""   # so workspace-manager can materialize the checkout


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
    """Host seam → gitlab-host-adapter."""
    async def load(self, ref: MRRef) -> MRPayload: ...


@runtime_checkable
class Workspace(Protocol):
    """Workspace seam → workspace-manager."""
    async def materialize(self, repo: RepoRef, commit: str) -> CheckoutHandle: ...
