"""GitLab implementation of HostProvider (GitLab REST v4). Implements the MRSource seam.

Talks to GitLab over an injected httpx.AsyncClient (tests drive it with MockTransport), maps the
JSON to review-mate's host-neutral models. Host specifics are confined here (host-confinement guard).
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx

from review_mate.host.base import CapabilityError, GITLAB_CAPABILITIES, parse_reference
from review_mate.seams import MRPayload, MRRef
from review_mate.session.state import (
    ChangeType, FileEntry, MRMetadata, ReviewThread, ThreadComment,
)


def is_auth_error(exc: Exception) -> bool:
    """A host auth failure (401/403) — should surface, not be masked as an empty result."""
    resp = getattr(exc, "response", None)
    return resp is not None and resp.status_code in (401, 403)


async def _authed(client: httpx.AsyncClient, obj, method: str, url: str, **kw):
    """Send with obj.token as a Bearer; on 401/403 reload the token once (so a side `glab auth`
    refresh takes effect live) and retry. A still-failing auth error propagates (surfaced)."""
    resp = await client.request(method, url, headers={"Authorization": f"Bearer {obj.token}"}, **kw)
    if resp.status_code in (401, 403) and getattr(obj, "_reload_token", None) is not None:
        fresh = obj._reload_token()
        if fresh and fresh != obj.token:
            obj.token = fresh
            resp = await client.request(method, url,
                                        headers={"Authorization": f"Bearer {obj.token}"}, **kw)
    resp.raise_for_status()
    return resp


class GitLabProvider:
    def __init__(self, base_url: str, token: str, username: str, host: str = "gitlab",
                 client: httpx.AsyncClient | None = None, reload_token=None,
                 git_protocol: str = "https"):
        self.base_url = base_url
        self.token = token
        self.username = username
        self.host = host
        self.git_protocol = git_protocol   # the git access method the user chose in glab (ssh|https)
        self._reload_token = reload_token   # () -> fresh token | None (live credential reload)
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
        threads = await self.fetch_threads(ref)

        metadata = MRMetadata(
            host=ref.host,
            project=proj.get("path_with_namespace", ref.project),
            iid=mr["iid"], title=mr["title"],
            source_branch=mr["source_branch"], target_branch=mr["target_branch"],
            sha=mr["sha"], author=(mr.get("author") or {}).get("username", ""),
            url=mr.get("web_url", ""), clone_url=_clone_url(proj, self.git_protocol),
            capabilities=self.capabilities(),
            diff_refs=mr.get("diff_refs") or {},
        )
        return MRPayload(
            mr=metadata,
            files=[_to_file(c) for c in changes.get("changes", [])],
            threads=threads,
            clone_url=_clone_url(proj, self.git_protocol),
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

        Primary intent: when the query names a project (a full path like `group/sub/proj` or a bare
        name like `proj`), list THAT project's opened MRs. Fuzzy free text falls back to a global MR
        title/description search. Returns display items: {host, project, iid, title, url}.
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

        # 1. Project-first: resolve the projects the query might name, then list their opened MRs
        #    concurrently (the query need not contain a slash — a bare name resolves too).
        paths = await self._resolve_projects(query)
        if paths:
            fetched = await asyncio.gather(*(self._project_open_mrs(p) for p in paths),
                                           return_exceptions=True)
            for rows in fetched:
                if isinstance(rows, Exception):
                    if is_auth_error(rows):
                        raise rows        # auth failure must surface, not read as "no matches"
                elif isinstance(rows, list):
                    for it in rows:
                        add(it)

        # 2. Fuzzy fallback: only when the query named no project. A global MR text search matches
        #    titles/descriptions across every visible project — useful for free text, but noise once
        #    we already resolved the project the reviewer meant, so we skip it then.
        if not items:
            try:
                rows = await self._get("/search", params={"scope": "merge_requests",
                                                          "search": query, "state": "opened"})
                for it in rows:
                    add(it)
            except httpx.HTTPError as exc:
                if is_auth_error(exc):
                    raise

        return items[:limit]

    async def _resolve_projects(self, query: str, limit: int = 5) -> list[str]:
        """path_with_namespace values the query might name: an exact full-path hit first, then a
        name search (the last path segment), so a bare name resolves as well as a full path. When
        the query carries a namespace, name-search matches are narrowed to paths that contain it.

        The name search is scoped to the reviewer's memberships (`membership=true`): on a large host
        an unscoped search for a common name (e.g. "control-plane") is swamped by unrelated public
        projects, burying the one the reviewer actually works on. A full path they can see but are
        not a member of still resolves via the exact-path hit above."""
        q = query.strip("/")
        paths: list[str] = []
        if "/" in q:  # exact full-path project id (url-encoded), e.g. group/sub/proj
            try:
                proj = await self._get(f"/projects/{quote(q, safe='')}")
                pwn = proj.get("path_with_namespace")
                if pwn:
                    paths.append(pwn)
            except httpx.HTTPError as exc:
                if is_auth_error(exc):
                    raise          # a 404 here just means "not a real path"; a 401/403 must surface
        try:
            projects = await self._get("/projects", params={"search": q.rsplit("/", 1)[-1],
                                                            "membership": "true",
                                                            "simple": "true", "per_page": 20})
            cands = [p.get("path_with_namespace") for p in projects if p.get("path_with_namespace")]
            if "/" in q:  # a namespaced query disambiguates: keep only paths that contain it
                cands = [c for c in cands if q in c] or cands
            for c in cands:
                if c not in paths:
                    paths.append(c)
        except httpx.HTTPError as exc:
            if is_auth_error(exc):
                raise
        return paths[:limit]

    async def _project_open_mrs(self, path: str, per_page: int = 50) -> list[dict]:
        return await self._get(f"/projects/{quote(path, safe='')}/merge_requests",
                               params={"state": "opened", "order_by": "updated_at", "per_page": per_page})

    async def mr_versions(self, ref: MRRef) -> list[dict]:
        """The MR's diff versions (newest first): per-push base/head SHAs, for a base-aware
        interdiff (diff-versions). Best-effort: [] on error / when unsupported."""
        try:
            rows = await self._get(
                f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}/versions")
        except httpx.HTTPError:
            return []
        return [{"base_sha": r.get("base_commit_sha"), "head_sha": r.get("head_commit_sha"),
                 "start_sha": r.get("start_commit_sha"), "created_at": r.get("created_at", "")}
                for r in rows]

    async def commits(self, ref: MRRef) -> list[dict]:
        """The MR's commits (oldest→newest, for per-commit review): sha, short id, title, message,
        author. Best-effort: [] on error."""
        try:
            rows = await self._get(
                f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}/commits")
        except httpx.HTTPError:
            return []
        # GitLab returns newest-first; reverse so review reads in authoring order
        return [{"sha": c.get("id", ""), "short_id": c.get("short_id", ""),
                 "title": c.get("title", ""), "message": c.get("message", ""),
                 "author": c.get("author_name", ""), "created_at": c.get("created_at", "")}
                for c in reversed(rows)]

    async def mr_summary(self, ref: MRRef) -> dict:
        """A light MR fetch for the hub: current head sha + lifecycle state (opened/closed/merged).
        One call gives both. Best-effort — {} on error."""
        try:
            mr = await self._get(f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}")
        except httpx.HTTPError:
            return {}
        return {"head": mr.get("sha", ""), "state": mr.get("state", "")}

    async def approvals(self, ref: MRRef) -> dict:
        """The MR's approval state: the usernames who approved, and whether the reviewer themself
        has. Best-effort — {} on error / when unsupported."""
        try:
            data = await self._get(
                f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}/approvals")
        except httpx.HTTPError:
            return {"approved_by": [], "you_approved": False}
        by = [(a.get("user") or {}).get("username", "") for a in (data.get("approved_by") or [])]
        by = [u for u in by if u]
        return {"approved_by": by, "you_approved": self.username in by}

    async def commit_diff(self, project: str, sha: str) -> list[FileEntry]:
        """The per-file diff of one commit (against its parent) — the file set + hunks for reviewing
        a single commit, shaped like the MR diff's files."""
        rows = await self._get(f"/projects/{quote(project, safe='')}/repository/commits/{sha}/diff")
        return [_to_file(c) for c in rows]

    async def fetch_threads(self, ref: MRRef) -> list[ReviewThread]:
        """The MR's discussions, mapped to the host-neutral review model — the read side of the
        thread-conversation surface (used by initial load and by on-demand refresh)."""
        pid = quote(ref.project, safe="")
        discussions = await self._get(f"/projects/{pid}/merge_requests/{ref.iid}/discussions")
        # drop system-note-only discussions (approvals, pushes, label changes, …) — not review threads
        return [t for d in discussions if (t := _to_thread(d)).comments]

    async def blame(self, project: str, path: str, ref: str, start: int, end: int) -> list[dict]:
        """Last-touch info for a line range (the cheap context tier, D21) — GitLab file blame at
        `ref`. Best-effort: returns [] on any error so the tier degrades rather than failing."""
        try:
            rows = await self._get(
                f"/projects/{quote(project, safe='')}/repository/files/{quote(path, safe='')}/blame",
                params={"ref": ref, "range[start]": start, "range[end]": end},
            )
        except httpx.HTTPError:
            return []
        out, line = [], start
        for r in rows:
            commit = r.get("commit") or {}
            n = max(len(r.get("lines") or []), 1)
            out.append({"lines": [line, line + n - 1], "commit": (commit.get("id") or "")[:12],
                        "author": commit.get("author_name", ""), "date": commit.get("committed_date", ""),
                        "summary": commit.get("title", "")})
            line += n
        return out

    async def linked_issues(self, project: str, iid: int) -> list[dict]:
        """Issues this MR closes (the cheap context tier, D21). Best-effort: [] on any error."""
        try:
            rows = await self._get(
                f"/projects/{quote(project, safe='')}/merge_requests/{iid}/closes_issues")
        except httpx.HTTPError:
            return []
        return [{"iid": r.get("iid"), "title": r.get("title", ""), "url": r.get("web_url", "")}
                for r in rows]

    async def issue_related_mrs(self, project: str, issue_iid: int) -> list[MRRef]:
        items = await self._get(
            f"/projects/{quote(project, safe='')}/issues/{issue_iid}/related_merge_requests"
        )
        return [r for it in items if (r := parse_reference(it.get("web_url", ""), self.host))]

    async def _get(self, path: str, params: dict | None = None):
        resp = await _authed(self._client, self, "GET", path, params=params)
        return resp.json()


class GitLabWriter:
    """The write side: reviewer-authored comments, threads, suggestions, approvals — capability-gated."""

    def __init__(self, base_url: str, token: str, capabilities: dict[str, bool],
                 client: httpx.AsyncClient | None = None, reload_token=None):
        self.base_url = base_url
        self.token = token
        self._caps = dict(capabilities)
        self._reload_token = reload_token   # () -> fresh token | None (live credential reload)
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

    async def post_mr_comment(self, ref: MRRef, body: str) -> dict:
        """An MR-level note (a review summary) — a general comment with no diff position."""
        self._require("mr_comments")
        return await self._post(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}/notes",
            json={"body": body},
        )

    async def reply(self, ref: MRRef, discussion_id: str, body: str) -> dict:
        self._require("threads")
        return await self._post(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}"
            f"/discussions/{discussion_id}/notes",
            json={"body": body},
        )

    async def resolve(self, ref: MRRef, discussion_id: str, resolved: bool = True) -> dict:
        self._require("threads")
        return await self._put(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}"
            f"/discussions/{discussion_id}",
            params={"resolved": "true" if resolved else "false"},
        )

    async def approve(self, ref: MRRef) -> dict:
        self._require("approvals")
        return await self._post(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}/approve"
        )

    async def edit_note(self, ref: MRRef, discussion_id: str, note_id: str, body: str) -> dict:
        self._require("threads")
        return await self._put(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}"
            f"/discussions/{discussion_id}/notes/{note_id}",
            params={"body": body},
        )

    async def delete_note(self, ref: MRRef, discussion_id: str, note_id: str) -> dict:
        self._require("threads")
        await self._delete(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}"
            f"/discussions/{discussion_id}/notes/{note_id}",
        )
        return {"ok": True}

    async def suggest(self, ref: MRRef, position: dict, suggestion: str) -> dict:
        self._require("suggestions")
        body = f"```suggestion:-0+0\n{suggestion}\n```"
        return await self._post(
            f"/projects/{quote(ref.project, safe='')}/merge_requests/{ref.iid}/discussions",
            json={"body": body, "position": _position(position)},
        )

    async def _post(self, path: str, json: dict | None = None) -> dict:
        return (await _authed(self._client, self, "POST", path, json=json or {})).json()

    async def _put(self, path: str, params: dict | None = None) -> dict:
        return (await _authed(self._client, self, "PUT", path, params=params)).json()

    async def _delete(self, path: str) -> None:
        await _authed(self._client, self, "DELETE", path)


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


def _clone_url(proj: dict, git_protocol: str) -> str:
    """The clone URL matching the git access method the user chose in glab. SSH → ssh_url_to_repo
    (the workspace clones with the user's ambient SSH key — no token, no secret on disk); otherwise
    the HTTPS URL. Falls back to HTTPS if the host didn't advertise an SSH URL."""
    if git_protocol == "ssh" and proj.get("ssh_url_to_repo"):
        return proj["ssh_url_to_repo"]
    return proj.get("http_url_to_repo", "")


def _to_thread(discussion: dict) -> ReviewThread:
    # keep only human notes — GitLab system notes (approved/unapproved, pushed commits, label and
    # description changes, …) are MR-history noise, not review discussion
    notes = [n for n in (discussion.get("notes") or []) if not n.get("system")]
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
