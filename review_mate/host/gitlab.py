"""GitLab implementation of HostProvider (GitLab REST v4). Implements the MRSource seam.

Talks to GitLab over an injected httpx.AsyncClient (tests drive it with MockTransport), maps the
JSON to review-mate's host-neutral models. Host specifics are confined here (host-confinement guard).
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

from review_mate.host.base import CapabilityError, GITLAB_CAPABILITIES, parse_reference
from review_mate.seams import MRPayload, MRRef
from review_mate.session.state import (
    ChangeType, FileEntry, MRMetadata, ReviewThread, ThreadComment,
)


class GitLabProvider:
    def __init__(self, base_url: str, token: str, username: str, host: str = "gitlab",
                 client: httpx.AsyncClient | None = None):
        self.base_url = base_url
        self.token = token
        self.username = username
        self.host = host
        self._client = client or httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )

    def capabilities(self) -> dict[str, bool]:
        return dict(GITLAB_CAPABILITIES)

    async def load(self, ref: MRRef) -> MRPayload:
        pid = quote(ref.project, safe="")
        proj = await self._get(f"/projects/{pid}")
        mr = await self._get(f"/projects/{pid}/merge_requests/{ref.iid}")
        changes = await self._get(f"/projects/{pid}/merge_requests/{ref.iid}/changes")
        discussions = await self._get(f"/projects/{pid}/merge_requests/{ref.iid}/discussions")

        metadata = MRMetadata(
            host=ref.host,
            project=proj.get("path_with_namespace", ref.project),
            iid=mr["iid"], title=mr["title"],
            source_branch=mr["source_branch"], target_branch=mr["target_branch"],
            sha=mr["sha"], author=(mr.get("author") or {}).get("username", ""),
            url=mr.get("web_url", ""), capabilities=self.capabilities(),
            diff_refs=mr.get("diff_refs") or {},
        )
        return MRPayload(
            mr=metadata,
            files=[_to_file(c) for c in changes.get("changes", [])],
            threads=[_to_thread(d) for d in discussions],
            clone_url=proj.get("http_url_to_repo", ""),
        )

    async def review_queue(self) -> list[MRRef]:
        refs: list[MRRef] = []
        for key in ("reviewer_username", "assignee_username"):
            items = await self._get("/merge_requests",
                                    params={"scope": "all", key: self.username, "state": "opened"})
            refs.extend(r for it in items if (r := parse_reference(it.get("web_url", ""), self.host)))
        seen, unique = set(), []
        for r in refs:
            if (r.project, r.iid) not in seen:
                seen.add((r.project, r.iid))
                unique.append(r)
        return unique

    async def get_repo_tree(self, project: str, ref: str, max_pages: int = 30) -> list[str]:
        """All blob paths in the repo at `ref` (for browsing beyond the diff). Capped for safety."""
        pid = quote(project, safe="")
        paths: list[str] = []
        for page in range(1, max_pages + 1):
            rows = await self._get(f"/projects/{pid}/repository/tree",
                                   params={"ref": ref, "recursive": "true",
                                           "per_page": 100, "page": page})
            paths.extend(r["path"] for r in rows if r.get("type") == "blob")
            if len(rows) < 100:
                break
        return paths

    async def get_file(self, project: str, path: str, ref: str) -> str:
        """Raw content of one file at `ref` (for viewing related, non-diff code)."""
        pid = quote(project, safe="")
        resp = await self._client.get(
            f"/projects/{pid}/repository/files/{quote(path, safe='')}/raw",
            params={"ref": ref}, headers={"Authorization": f"Bearer {self.token}"})
        resp.raise_for_status()
        return resp.text

    async def review_queue_items(self) -> list[dict]:
        """The review queue with display fields for a picker: project, iid, title, url."""
        seen, items = set(), []
        for key in ("reviewer_username", "assignee_username"):
            rows = await self._get("/merge_requests",
                                   params={"scope": "all", key: self.username, "state": "opened"})
            for it in rows:
                ref = parse_reference(it.get("web_url", ""), self.host)
                if ref and (ref.project, ref.iid) not in seen:
                    seen.add((ref.project, ref.iid))
                    items.append({"host": ref.host, "project": ref.project, "iid": ref.iid,
                                  "title": it.get("title", ""), "url": it.get("web_url", "")})
        return items

    async def search(self, query: str, limit: int = 15) -> list[dict]:
        """Suggest opened MRs matching a free-text query — the flexible lookup box's source.

        Global MR text search (title/description) plus, when the query reads like a project path,
        that project's opened MRs. Returns display items: {host, project, iid, title, url}.
        """
        query = (query or "").strip()
        if not query:
            return []
        seen: set = set()
        items: list[dict] = []

        def add(it: dict) -> None:
            ref = parse_reference(it.get("web_url", ""), self.host)
            if ref and (ref.project, ref.iid) not in seen:
                seen.add((ref.project, ref.iid))
                items.append({"host": ref.host, "project": ref.project, "iid": ref.iid,
                              "title": it.get("title", ""), "url": it.get("web_url", "")})

        try:
            rows = await self._get("/search", params={"scope": "merge_requests",
                                                      "search": query, "state": "opened"})
            for it in rows:
                add(it)
        except httpx.HTTPError:
            pass

        if "/" in query and len(items) < limit:
            try:
                projects = await self._get("/projects", params={"search": query.rsplit("/", 1)[-1],
                                                               "simple": "true", "per_page": 5})
                for p in projects:
                    pid = quote(p.get("path_with_namespace", ""), safe="")
                    if not pid:
                        continue
                    mrs = await self._get(f"/projects/{pid}/merge_requests",
                                          params={"state": "opened", "per_page": 5})
                    for it in mrs:
                        add(it)
            except httpx.HTTPError:
                pass

        return items[:limit]

    async def issue_related_mrs(self, project: str, issue_iid: int) -> list[MRRef]:
        items = await self._get(
            f"/projects/{quote(project, safe='')}/issues/{issue_iid}/related_merge_requests"
        )
        return [r for it in items if (r := parse_reference(it.get("web_url", ""), self.host))]

    async def _get(self, path: str, params: dict | None = None):
        resp = await self._client.get(path, params=params,
                                      headers={"Authorization": f"Bearer {self.token}"})
        resp.raise_for_status()
        return resp.json()


class GitLabWriter:
    """The write side: reviewer-authored comments, threads, suggestions, approvals — capability-gated."""

    def __init__(self, base_url: str, token: str, capabilities: dict[str, bool],
                 client: httpx.AsyncClient | None = None):
        self.base_url = base_url
        self.token = token
        self._caps = dict(capabilities)
        self._client = client or httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )

    def capabilities(self) -> dict[str, bool]:
        return dict(self._caps)

    def _require(self, capability: str) -> None:
        if not self._caps.get(capability):
            raise CapabilityError(capability)

    async def post_comment(self, ref: MRRef, position: dict, body: str) -> dict:
        self._require("inline_comments")
        return await self._post(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}/discussions",
            json={"body": body, "position": _position(position)},
        )

    async def reply(self, ref: MRRef, discussion_id: str, body: str) -> dict:
        self._require("threads")
        return await self._post(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}"
            f"/discussions/{discussion_id}/notes",
            json={"body": body},
        )

    async def resolve(self, ref: MRRef, discussion_id: str) -> dict:
        self._require("threads")
        return await self._put(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}"
            f"/discussions/{discussion_id}",
            params={"resolved": "true"},
        )

    async def approve(self, ref: MRRef) -> dict:
        self._require("approvals")
        return await self._post(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}/approve"
        )

    async def suggest(self, ref: MRRef, position: dict, suggestion: str) -> dict:
        self._require("suggestions")
        body = f"```suggestion:-0+0\n{suggestion}\n```"
        return await self._post(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}/discussions",
            json={"body": body, "position": _position(position)},
        )

    async def _post(self, path: str, json: dict | None = None) -> dict:
        resp = await self._client.post(path, json=json or {},
                                       headers={"Authorization": f"Bearer {self.token}"})
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, params: dict | None = None) -> dict:
        resp = await self._client.put(path, params=params,
                                      headers={"Authorization": f"Bearer {self.token}"})
        resp.raise_for_status()
        return resp.json()


def _position(position: dict) -> dict:
    pos = {"position_type": "text", "new_path": position.get("new_path"),
           "new_line": position.get("new_line")}
    for key in ("base_sha", "head_sha", "start_sha"):
        if position.get(key):
            pos[key] = position[key]
    sha = position.get("sha")  # fallback when diff_refs are unknown
    if sha:
        pos.setdefault("base_sha", sha)
        pos.setdefault("head_sha", sha)
        pos.setdefault("start_sha", sha)
    return pos


def _to_file(change: dict) -> FileEntry:
    if change.get("new_file"):
        ct = ChangeType.ADDED
    elif change.get("deleted_file"):
        ct = ChangeType.DELETED
    elif change.get("renamed_file"):
        ct = ChangeType.RENAMED
    else:
        ct = ChangeType.MODIFIED
    path = change.get("new_path") or change.get("old_path") or ""
    old = change.get("old_path")
    return FileEntry(path=path, old_path=old if old and old != path else None,
                     change_type=ct, hunks=[{"diff": change.get("diff", "")}])


def _to_thread(discussion: dict) -> ReviewThread:
    notes = discussion.get("notes", []) or []
    comments = [
        ThreadComment(id=str(n.get("id")), author=(n.get("author") or {}).get("username", ""),
                      body=n.get("body", ""), created_at=n.get("created_at", ""))
        for n in notes
    ]
    first = notes[0] if notes else {}
    pos = first.get("position") or {}
    anchor = {"file": pos.get("new_path"), "line": pos.get("new_line")} if pos else None
    # GitLab v4 carries `resolved` per-note (no discussion-level field); a thread is resolved when
    # any of its resolvable notes is resolved.
    resolved = any(n.get("resolved") for n in notes)
    return ReviewThread(id=str(discussion.get("id")), anchor=anchor, comments=comments,
                        resolved=resolved)
