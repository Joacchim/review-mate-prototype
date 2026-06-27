"""ReviewKB — the user-wide review knowledge base (D12).

A repo catalog, cross-repo relationships, and configured preferences/integrations (D11), persisted
as one JSON document under ~/.review-mate/knowledge/ — user-wide, distinct from per-project memory.
The context-skill consults it; the crossrepo-broker enriches it (each approval → a relationship).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from review_mate.config import review_mate_home


class RepoInfo(BaseModel):
    project: str
    group: str = ""
    purpose: str = ""
    mirror_path: str = ""


class Relationship(BaseModel):
    src: str
    dst: str
    note: str = ""


class _KBData(BaseModel):
    repos: dict[str, RepoInfo] = Field(default_factory=dict)
    relationships: list[Relationship] = Field(default_factory=list)
    prefs: dict[str, object] = Field(default_factory=dict)


class ReviewKB:
    def __init__(self, root: Path | str | None = None):
        base = Path(root) if root is not None else review_mate_home()
        self.dir = base / "knowledge"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "knowledge.json"
        self._data = self._load()

    # --- repos --------------------------------------------------------------

    def record_repo(self, project: str, *, group: str | None = None,
                    purpose: str | None = None, mirror_path: str | None = None) -> None:
        info = self._data.repos.get(project) or RepoInfo(project=project)
        if group is not None:
            info.group = group
        if purpose is not None:
            info.purpose = purpose
        if mirror_path is not None:
            info.mirror_path = mirror_path
        self._data.repos[project] = info
        self._save()

    def get_repo(self, project: str) -> RepoInfo | None:
        return self._data.repos.get(project)

    def list_repos(self) -> list[RepoInfo]:
        return list(self._data.repos.values())

    # --- relationships ------------------------------------------------------

    def record_relationship(self, src: str, dst: str, note: str = "") -> None:
        for r in self._data.relationships:
            if r.src == src and r.dst == dst:
                if note:
                    r.note = note
                self._save()
                return
        self._data.relationships.append(Relationship(src=src, dst=dst, note=note))
        self._save()

    def related(self, project: str) -> list[str]:
        return [r.dst for r in self._data.relationships if r.src == project]

    # --- preferences / integrations ----------------------------------------

    def set_pref(self, key: str, value) -> None:
        self._data.prefs[key] = value
        self._save()

    def get_pref(self, key: str, default=None):
        return self._data.prefs.get(key, default)

    # --- seeding ------------------------------------------------------------

    def seed(self, repos: list[RepoInfo]) -> None:
        """Merge catalog entries without clobbering existing per-repo enrichment."""
        for incoming in repos:
            existing = self._data.repos.get(incoming.project)
            if existing is None:
                self._data.repos[incoming.project] = incoming
            else:
                for field in ("group", "purpose", "mirror_path"):
                    if not getattr(existing, field) and getattr(incoming, field):
                        setattr(existing, field, getattr(incoming, field))
        self._save()

    # --- persistence --------------------------------------------------------

    def _load(self) -> _KBData:
        if self.path.exists():
            return _KBData.model_validate_json(self.path.read_text())
        return _KBData()

    def _save(self) -> None:
        self.path.write_text(self._data.model_dump_json(indent=2))
