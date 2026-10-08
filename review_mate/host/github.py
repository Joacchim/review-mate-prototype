"""GitHub implementation of the host contract — REST v3 for most of it, GraphQL where REST cannot.

Host specifics are confined here, the way the GitLab adapter confines its own. What GitHub calls a
pull request this calls a merge request throughout, because the review model is host-neutral and
renaming it per forge would push the difference into every unit above.

Two things are read over GraphQL rather than REST, and neither is a preference:

- **Discussions.** REST lists review comments but will not say whether a thread is resolved, and
  has no way to resolve one. GraphQL's `reviewThreads` gives the state, the thread's own id, and
  the comments in one request — so replying and resolving address the thread directly instead of
  guessing at it from a comment id.
- **Blame.** REST has no blame at all.

And one thing is deliberately absent. GitHub has no equivalent of a merge request's *versions*, so
`diff_versions` is false and there is no `mr_versions` here to call. Reviewing "since you last
looked" is computed from the reviewer's own watermark against the clone instead, which is a
different fact about a different thing — see the diff topic.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlparse

import httpx

from review_mate.contracts import MRPayload, MRRef
from review_mate.host.base import CapabilityError
from review_mate.session.state import (
    ChangeType, FileEntry, MRMetadata, ReviewThread, ThreadComment,
)

# What a pull request can carry. Read against GITLAB_CAPABILITIES: what differs, differs because
# GitHub does not offer it, not because it is unimplemented here.
GITHUB_CAPABILITIES: dict[str, bool] = {
    "inline_comments": True,
    "multiline_comments": True,
    "file_comments": True,
    "mr_comments": True,
    "threads": True,
    "suggestions": True,
    "approvals": True,
    "draft_reviews": True,
    "diff_versions": False,   # no versions API; "since you last looked" is derived from the clone
    "commits": True,
    # a review comment takes any commit of the pull request as its `commit_id`, keeps the remark in
    # the conversation and flags it outdated once the line moves — exercised against a real PR
    "commit_comments": True,
    "reactions": True,
    "labels": True,
    "reviewers": True,
}

_PR_URL = re.compile(r"/pull/(\d+)")
_STATUS = {"added": ChangeType.ADDED, "removed": ChangeType.DELETED,
           "renamed": ChangeType.RENAMED, "copied": ChangeType.ADDED}


def parse_github_reference(s: str, default_host: str) -> MRRef | None:
    """A pull request URL, or the `owner/repo#123` shorthand.

    The separator is what disambiguates a shorthand between forges: `#` is how GitHub writes one and
    `!` is how GitLab does, so neither parser can claim the other's and a reference never needs a
    forge named alongside it.
    """
    s = (s or "").strip()
    if not s:
        return None
    if "://" in s or s.startswith("http"):
        u = urlparse(s if "://" in s else "https://" + s)
        m = _PR_URL.search(u.path)
        if not m:
            return None
        project = u.path.split("/pull/")[0].strip("/")
        return MRRef(host=u.netloc, project=project, iid=int(m.group(1))) if project else None
    if "#" in s:
        left, _, right = s.partition("#")
        if "/" in left and right.isdigit():
            return MRRef(host=default_host, project=left.strip("/"), iid=int(right))
    return None


async def _authed(client: httpx.AsyncClient, obj, method: str, url: str,
                  headers: dict | None = None, **kw):
    """Send as obj; on 401/403 reload the token once (so a side `gh auth` refresh takes effect
    live) and retry. A still-failing auth error propagates (surfaced). The headers are rebuilt
    for the retry rather than reused, which is what carries the fresh token; a caller's own
    headers (the raw-content Accept) still win over the defaults.
    """
    def _h():
        return {**obj._headers(), **(headers or {})}
    resp = await client.request(method, url, headers=_h(), **kw)
    if resp.status_code in (401, 403) and getattr(obj, "_reload_token", None) is not None:
        fresh = obj._reload_token()
        if fresh and fresh != obj.token:
            obj.token = fresh
            resp = await client.request(method, url, headers=_h(), **kw)
    resp.raise_for_status()
    return resp


class GitHubProvider:
    """Reads a pull request. `project` is `owner/repo`; `iid` is the number people cite."""

    def __init__(self, base_url: str, token: str, username: str, host: str = "github.com",
                 graphql_url: str | None = None, client: httpx.AsyncClient | None = None,
                 git_protocol: str = "https", reload_token=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.username = username
        self.host = host
        self.git_protocol = git_protocol
        self._reload_token = reload_token   # () -> fresh token | None (live credential reload)
        # Enterprise splits them: the REST root ends /api/v3 while GraphQL sits at /api/graphql.
        self.graphql_url = graphql_url or (
            "https://api.github.com/graphql" if "api.github.com" in self.base_url
            else self.base_url.replace("/api/v3", "") + "/api/graphql")
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=httpx.Timeout(15.0, connect=5.0))

    def capabilities(self) -> dict[str, bool]:
        return dict(GITHUB_CAPABILITIES)

    # --- talking to it ---------------------------------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    async def _get(self, path: str, params: dict | None = None):
        resp = await _authed(self._client, self, "GET", path, params=params)
        return resp.json()

    async def _paged(self, path: str, params: dict | None = None, max_pages: int = 20) -> list:
        out: list = []
        page = 1
        while page <= max_pages:
            rows = await self._get(path, params={**(params or {}), "per_page": 100, "page": page})
            if not isinstance(rows, list) or not rows:
                break
            out.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        return out

    async def _graphql(self, query: str, **variables):
        resp = await _authed(self._client, self, "POST", self.graphql_url,
                             json={"query": query, "variables": variables})
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"github graphql: {body['errors'][0].get('message', 'failed')}")
        return body.get("data") or {}

    # --- the change ------------------------------------------------------------------------

    async def load(self, ref: MRRef) -> MRPayload:
        pr = await self._get(f"/repos/{ref.project}/pulls/{ref.iid}")
        base, head = pr.get("base") or {}, pr.get("head") or {}
        repo = base.get("repo") or {}
        clone = repo.get("ssh_url") if self.git_protocol == "ssh" else repo.get("clone_url")
        return MRPayload(
            mr=MRMetadata(
                host=self.host, project=ref.project, iid=ref.iid,
                title=pr.get("title") or "", source_branch=head.get("ref") or "",
                target_branch=base.get("ref") or "", sha=head.get("sha") or "",
                author=((pr.get("user") or {}).get("login") or ""),
                url=pr.get("html_url") or "", clone_url=clone or "", ref_mark="#",
                capabilities=self.capabilities(),
                # `base.sha` is the base branch as this pull request sees it, not a merge base —
                # enough for the agent to diff a pair itself, and the since-diff computes its own
                diff_refs={"base_sha": base.get("sha") or "", "head_sha": head.get("sha") or "",
                           "start_sha": base.get("sha") or ""},
            ),
            files=await self._files(ref),
            threads=await self.fetch_threads(ref),
            clone_url=clone or "",
        )

    async def _files(self, ref: MRRef) -> list[FileEntry]:
        rows = await self._paged(f"/repos/{ref.project}/pulls/{ref.iid}/files")
        return [_entry(row) for row in rows]

    async def commits(self, ref: MRRef) -> list[dict]:
        rows = await self._paged(f"/repos/{ref.project}/pulls/{ref.iid}/commits")
        out = []
        # GitHub lists a pull request's commits oldest-first already, which is the order the review
        # reads in — and the order the picker's "reviewed up to here" ticks depend on. Reversing
        # here, as the GitLab adapter must, turned the list round and inverted those ticks.
        for row in rows:
            commit = row.get("commit") or {}
            message = commit.get("message") or ""
            out.append({"sha": row.get("sha") or "", "short_id": (row.get("sha") or "")[:8],
                        "title": message.split("\n", 1)[0], "message": message,
                        "author": ((commit.get("author") or {}).get("name") or ""),
                        "created_at": ((commit.get("author") or {}).get("date") or "")})
        return out

    async def commit_diff(self, ref: MRRef, sha: str) -> list[FileEntry]:
        row = await self._get(f"/repos/{ref.project}/commits/{sha}")
        return [_entry(f) for f in (row.get("files") or [])]

    async def get_file(self, project: str, path: str, ref: str) -> str:
        resp = await _authed(
            self._client, self, "GET", f"/repos/{project}/contents/{quote(path)}",
            params={"ref": ref}, headers={"Accept": "application/vnd.github.raw"})
        return resp.text

    async def get_repo_tree(self, project: str, ref: str, max_pages: int = 30) -> list[str]:
        tree = await self._get(f"/repos/{project}/git/trees/{ref}", params={"recursive": "1"})
        return [n["path"] for n in (tree.get("tree") or []) if n.get("type") == "blob"]

    async def mr_summary(self, ref: MRRef) -> dict:
        pr = await self._get(f"/repos/{ref.project}/pulls/{ref.iid}")
        state = "merged" if pr.get("merged_at") else (pr.get("state") or "")
        return {"state": state, "draft": bool(pr.get("draft")),
                "head": ((pr.get("head") or {}).get("sha") or "")}

    async def approvals(self, ref: MRRef) -> dict:
        rows = await self._paged(f"/repos/{ref.project}/pulls/{ref.iid}/reviews")
        # the last word per reviewer: an approval withdrawn by a later request for changes is not one
        latest: dict[str, str] = {}
        for row in rows:
            who = ((row.get("user") or {}).get("login") or "")
            if who and row.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[who] = row["state"]
        approved = [who for who, state in latest.items() if state == "APPROVED"]
        return {"approved_by": [{"username": who} for who in approved],
                "approved": bool(approved),
                "user_has_approved": self.username in approved}

    # --- discussions, over GraphQL because REST cannot say whether one is resolved ------------

    _THREADS = """
    query($owner:String!,$name:String!,$number:Int!){
      repository(owner:$owner,name:$name){ pullRequest(number:$number){
        reviewThreads(first:100){ nodes{
          id isResolved path line
          comments(first:100){ nodes{ databaseId author{login} body createdAt } } } } } }
    }"""

    async def fetch_threads(self, ref: MRRef) -> list[ReviewThread]:
        """The pull request's review threads, with their resolution and their own ids.

        The id is GitHub's node id for the thread rather than a comment's: replying and resolving
        both address the thread itself, so nothing downstream has to reconstruct which comment
        started it.
        """
        owner, _, name = ref.project.partition("/")
        try:
            data = await self._graphql(self._THREADS, owner=owner, name=name, number=ref.iid)
        except (httpx.HTTPError, RuntimeError):
            return []        # discussions degrade rather than failing the load, as on GitLab
        pr = ((data.get("repository") or {}).get("pullRequest") or {})
        out = []
        for node in ((pr.get("reviewThreads") or {}).get("nodes") or []):
            comments = [
                ThreadComment(id=str(c.get("databaseId")),
                              author=((c.get("author") or {}).get("login") or ""),
                              body=c.get("body") or "", created_at=c.get("createdAt") or "")
                for c in ((node.get("comments") or {}).get("nodes") or [])
            ]
            if not comments:
                continue
            anchor = {"file": node.get("path"), "line": node.get("line")} if node.get("path") else None
            out.append(ReviewThread(id=str(node.get("id")), anchor=anchor, comments=comments,
                                    resolved=bool(node.get("isResolved"))))
        return out

    _BLAME = """
    query($owner:String!,$name:String!,$ref:String!,$path:String!){
      repository(owner:$owner,name:$name){ object(expression:$ref){ ... on Commit {
        blame(path:$path){ ranges{ startingLine endingLine commit{
          oid committedDate messageHeadline author{name} } } } } } }
    }"""

    async def blame(self, project: str, path: str, ref: str, start: int, end: int) -> list[dict]:
        """Who last touched a line range. GraphQL because REST has no blame at all.

        Best-effort, like its GitLab counterpart: the host context is an extra a reviewer gets for
        free, and a forge that will not answer should cost them nothing.
        """
        owner, _, name = project.partition("/")
        try:
            data = await self._graphql(self._BLAME, owner=owner, name=name, ref=ref, path=path)
        except (httpx.HTTPError, RuntimeError):
            return []
        ranges = (((data.get("repository") or {}).get("object") or {}).get("blame") or {}).get("ranges") or []
        out = []
        for r in ranges:
            lo, hi = r.get("startingLine") or 0, r.get("endingLine") or 0
            if hi < start or lo > end:          # GitHub blames the file; keep the asked-for window
                continue
            commit = r.get("commit") or {}
            out.append({"lines": [max(lo, start), min(hi, end)],
                        "commit": (commit.get("oid") or "")[:12],
                        "author": ((commit.get("author") or {}).get("name") or ""),
                        "date": commit.get("committedDate") or "",
                        "summary": commit.get("messageHeadline") or ""})
        return out

    _CLOSES = """
    query($owner:String!,$name:String!,$number:Int!){
      repository(owner:$owner,name:$name){ pullRequest(number:$number){
        closingIssuesReferences(first:20){ nodes{ number title url } } } }
    }"""

    async def linked_issues(self, project: str, iid: int) -> list[dict]:
        """Issues this pull request closes. GitHub keeps them as a reference list, not a REST route."""
        owner, _, name = project.partition("/")
        try:
            data = await self._graphql(self._CLOSES, owner=owner, name=name, number=iid)
        except (httpx.HTTPError, RuntimeError):
            return []
        pr = ((data.get("repository") or {}).get("pullRequest") or {})
        return [{"iid": n.get("number"), "title": n.get("title") or "", "url": n.get("url") or ""}
                for n in ((pr.get("closingIssuesReferences") or {}).get("nodes") or [])]

    # --- everywhere the reviewer works ------------------------------------------------------

    async def review_queue_items(self) -> list[dict]:
        """Pull requests waiting on this reviewer, as a picker wants them."""
        seen, items = set(), []
        for q in (f"is:open is:pr review-requested:{self.username}",
                  f"is:open is:pr assignee:{self.username}"):
            try:
                found = await self._get("/search/issues", params={"q": q, "per_page": 50})
            except httpx.HTTPError:
                continue
            for row in (found.get("items") or []):
                ref = parse_github_reference(row.get("html_url") or "", self.host)
                if ref and (ref.project, ref.iid) not in seen:
                    seen.add((ref.project, ref.iid))
                    items.append({"host": ref.host, "project": ref.project, "iid": ref.iid,
                                  "title": row.get("title") or "", "ref_mark": "#",
                                  "url": row.get("html_url") or ""})
        return items

    async def review_queue(self) -> list[MRRef]:
        return [MRRef(host=i["host"], project=i["project"], iid=i["iid"])
                for i in await self.review_queue_items()]

    async def search(self, query: str, limit: int = 15) -> list[dict]:
        try:
            found = await self._get("/search/issues",
                                    params={"q": f"is:pr is:open {query}", "per_page": limit})
        except httpx.HTTPError:
            return []
        out = []
        for row in (found.get("items") or [])[:limit]:
            ref = parse_github_reference(row.get("html_url") or "", self.host)
            if ref:
                out.append({"host": ref.host, "project": ref.project, "iid": ref.iid,
                            "title": row.get("title") or "", "ref_mark": "#",
                            "url": row.get("html_url") or ""})
        return out

    async def locate_repo(self, name: str) -> dict | None:
        """Where a repository the agent asked to read can be cloned from, if this forge has it."""
        try:
            repo = await self._get(f"/repos/{name}") if "/" in name else None
        except httpx.HTTPError:
            repo = None
        if repo is None:
            return None
        clone = repo.get("ssh_url") if self.git_protocol == "ssh" else repo.get("clone_url")
        return {"host": self.host, "project": repo.get("full_name") or name,
                "clone_url": clone or "", "ref": repo.get("default_branch") or "HEAD"}


def _entry(row: dict) -> FileEntry:
    """One changed file. GitHub's `patch` is already the hunks without a header, which is the shape
    the review model holds — the same one a local branch's diff is trimmed to."""
    return FileEntry(path=row.get("filename") or "",
                     old_path=row.get("previous_filename"),
                     change_type=_STATUS.get(row.get("status") or "", ChangeType.MODIFIED),
                     hunks=[{"diff": row.get("patch") or ""}])


class GitHubWriter:
    """The write side. REST where GitHub has a route, GraphQL for the two things it does not.

    A thread's id here is GitHub's node id (see `fetch_threads`), which is what both replying and
    resolving take — so neither has to work out which comment began the thread.
    """

    def __init__(self, base_url: str, token: str, host: str = "github.com",
                 capabilities: dict[str, bool] | None = None,
                 graphql_url: str | None = None, client: httpx.AsyncClient | None = None,
                 reload_token=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.host = host
        self._caps = dict(capabilities or GITHUB_CAPABILITIES)
        self._reload_token = reload_token   # () -> fresh token | None (live credential reload)
        self.graphql_url = graphql_url or (
            "https://api.github.com/graphql" if "api.github.com" in self.base_url
            else self.base_url.replace("/api/v3", "") + "/api/graphql")
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=httpx.Timeout(15.0, connect=5.0))

    def capabilities(self) -> dict[str, bool]:
        return dict(self._caps)

    def _require(self, capability: str) -> None:
        if not self._caps.get(capability):
            raise CapabilityError(capability)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    async def _send(self, method: str, path: str, json: dict | None = None) -> dict:
        resp = await _authed(self._client, self, method, path, json=json)
        return resp.json() if resp.content else {}

    async def _graphql(self, query: str, **variables) -> dict:
        resp = await _authed(self._client, self, "POST", self.graphql_url,
                             json={"query": query, "variables": variables})
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"github graphql: {body['errors'][0].get('message', 'failed')}")
        return body.get("data") or {}

    # --- what the reviewer writes ------------------------------------------------------------

    async def post_comment(self, ref: MRRef, position: dict, body: str) -> dict:
        self._require("inline_comments")
        return await self._send("POST", f"/repos/{ref.project}/pulls/{ref.iid}/comments",
                                json={"body": body, **_pos(position)})

    async def post_mr_comment(self, ref: MRRef, body: str) -> dict:
        self._require("mr_comments")
        # a pull request is an issue as far as its general comments go
        return await self._send("POST", f"/repos/{ref.project}/issues/{ref.iid}/comments",
                                json={"body": body})

    async def suggest(self, ref: MRRef, position: dict, suggestion: str) -> dict:
        self._require("suggestions")
        body = "```suggestion\n" + suggestion + "\n```"
        return await self._send("POST", f"/repos/{ref.project}/pulls/{ref.iid}/comments",
                                json={"body": body, **_pos(position)})

    async def approve(self, ref: MRRef) -> dict:
        self._require("approvals")
        return await self._send("POST", f"/repos/{ref.project}/pulls/{ref.iid}/reviews",
                                json={"event": "APPROVE"})

    _REPLY = """
    mutation($thread:ID!,$body:String!){
      addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$thread,body:$body}){
        comment{ databaseId } } }"""

    async def reply(self, ref: MRRef, discussion_id: str, body: str) -> dict:
        self._require("threads")
        return await self._graphql(self._REPLY, thread=discussion_id, body=body)

    _RESOLVE = """
    mutation($thread:ID!){ resolveReviewThread(input:{threadId:$thread}){
      thread{ isResolved } } }"""
    _UNRESOLVE = """
    mutation($thread:ID!){ unresolveReviewThread(input:{threadId:$thread}){
      thread{ isResolved } } }"""

    async def resolve(self, ref: MRRef, discussion_id: str, resolved: bool = True) -> dict:
        """Resolution is GraphQL-only on GitHub — REST will not even report it, let alone set it."""
        self._require("threads")
        return await self._graphql(self._RESOLVE if resolved else self._UNRESOLVE,
                                   thread=discussion_id)

    async def edit_note(self, ref: MRRef, discussion_id: str, note_id: str, body: str) -> dict:
        # a review comment is addressed by its own id, so the thread it sits in is not needed here
        return await self._send("PATCH", f"/repos/{ref.project}/pulls/comments/{note_id}",
                                json={"body": body})

    async def delete_note(self, ref: MRRef, discussion_id: str, note_id: str) -> dict:
        return await self._send("DELETE", f"/repos/{ref.project}/pulls/comments/{note_id}")


def _pos(position: dict) -> dict:
    """The host-neutral position onto GitHub's. `commit_id` is the commit the line numbers belong
    to: an intermediate one when the reviewer was reading it, otherwise the head — which the review
    model calls `head_sha`, falling back to the session's own head.

    GitHub takes a comment on any commit of the pull request and keeps it in the conversation,
    pinned to that commit's line and flagged outdated once the line changes. Verified against a
    real pull request rather than read off the documentation: the thread comes back `isOutdated:
    true` with `originalLine` set and `line` null, which is the honest shape for a remark about a
    version that has been written over.
    """
    return {"path": position.get("new_path"),
            "line": position.get("new_line"),
            "side": "RIGHT",
            "commit_id": (position.get("commit_sha")
                          or position.get("head_sha") or position.get("sha"))}
