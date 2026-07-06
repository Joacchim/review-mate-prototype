"""HTTP + WebSocket handlers — thin: translate requests into manager/actor calls.

The browser is the only HTTP caller, so HTTP commands run with `Origin.BROWSER`; the authority
matrix in the core rejects anything it may not do. The agent reaches the session in-process
(the `mcp-bridge` seam), not through these routes.
"""
from __future__ import annotations

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from review_mate.seams import MRRef, RepoRef
from review_mate.session.commands import (
    ApplyFiles, ApplyMRMetadata, ApplyThread, MarkDraftPosted, parse_command,
)
from review_mate.session.manager import SessionManager
from review_mate.session.state import DraftStatus, Origin

# server-side long-poll ceiling for GET /api/activity: under common idle cutoffs, and short enough
# that the coordinator gets a regular tick (to re-evaluate the idle-reap bound) even when quiet.
ACTIVITY_TIMEOUT = 50.0


def build_routes(manager: SessionManager, resolve_ref=None, provider=None, broker=None,
                 writeback=None, activity_broker=None, kb=None) -> list:
    async def create_session(request: Request) -> JSONResponse:
        body = await _maybe_json(request)
        raw = body.get("ref") if isinstance(body, dict) else None
        ref: MRRef | None = None
        if isinstance(raw, dict):
            ref = MRRef(**raw)
        elif isinstance(raw, str) and raw.strip() and resolve_ref is not None:
            ref = resolve_ref(raw)
            if ref is None:
                return JSONResponse({"error": f"could not parse reference: {raw!r}"}, status_code=400)
        try:
            sid = await manager.create(ref=ref)
        except Exception as exc:  # a bad ref / GitLab failure shouldn't 500 the UI
            return JSONResponse({"error": f"failed to load MR: {exc}"}, status_code=502)
        return JSONResponse({"id": sid, "ref_resolved": ref is not None})

    async def review_queue(request: Request) -> JSONResponse:
        if provider is None or not hasattr(provider, "review_queue_items"):
            return JSONResponse([])  # no host configured → empty queue (baseline still runs)
        try:
            return JSONResponse(await provider.review_queue_items())
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    async def search(request: Request) -> JSONResponse:
        q = request.query_params.get("q", "").strip()
        if provider is None or not q or not hasattr(provider, "search"):
            return JSONResponse([])  # no host / empty query → no suggestions
        try:
            return JSONResponse(await provider.search(q))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    async def open_lookup(request: Request) -> JSONResponse:
        body = await _maybe_json(request)
        query = (body.get("query") if isinstance(body, dict) else "") or ""
        if broker is None or not query.strip():
            return JSONResponse({"error": "lookup unavailable"}, status_code=400)
        req = broker.create(query.strip())
        if activity_broker is not None:  # surface the lookup on the agent's activity stream (D16)
            activity_broker.publish("lookup_opened", lookup_id=req.id, query=query.strip())
        return JSONResponse({"id": req.id, "seq": req.seq})

    async def activity(request: Request) -> Response:
        if activity_broker is None:
            return Response(status_code=204)  # baseline: no activity channel configured
        try:
            since = int(request.query_params.get("since", "0"))
        except ValueError:
            since = 0
        event = await activity_broker.wait(since, timeout=ACTIVITY_TIMEOUT)
        if event is None:
            return Response(status_code=204)  # timed out — the caller re-polls
        return JSONResponse(event.model_dump(mode="json"))

    async def poll_lookup(request: Request) -> JSONResponse:
        if broker is None:
            return JSONResponse({"error": "lookup unavailable"}, status_code=400)
        req = await broker.wait_for_answer(request.path_params["id"], timeout=25.0)
        if req is None:
            return JSONResponse({"error": "unknown lookup"}, status_code=404)
        return JSONResponse(req.model_dump(mode="json"))

    async def repo_tree(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        mr = actor.snapshot().mr
        if provider is None or mr is None or not hasattr(provider, "get_repo_tree"):
            return JSONResponse([])
        try:
            return JSONResponse(await provider.get_repo_tree(mr.project, mr.sha))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    async def get_file(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        mr = actor.snapshot().mr
        path = request.query_params.get("path", "")
        if provider is None or mr is None or not path or not hasattr(provider, "get_file"):
            return JSONResponse({"error": "unavailable"}, status_code=400)
        try:
            return JSONResponse({"path": path, "content": await provider.get_file(mr.project, path, mr.sha)})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    async def list_sessions(request: Request) -> JSONResponse:
        return JSONResponse([s.model_dump(mode="json") for s in manager.list()])

    async def get_session(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        return JSONResponse(actor.snapshot().model_dump(mode="json"))

    async def submit_command(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        try:
            command = parse_command(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse({"error": "malformed command"}, status_code=400)
        result = await actor.submit(command, Origin.BROWSER)
        if not result.ok:
            return JSONResponse({"ok": False, "reason": result.reason}, status_code=400)
        return JSONResponse({"ok": True, "seq": result.seq})

    async def submit_review(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if writeback is None:
            return JSONResponse({"error": "review posting unavailable"}, status_code=400)
        snap = actor.snapshot()
        if snap.mr is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        ref = MRRef(host=snap.mr.host, project=snap.mr.project, iid=snap.mr.iid)
        pending = [d for d in snap.drafts if d.status is DraftStatus.DRAFT]
        by_id = {h.id: h for h in snap.highlights}
        results = []
        for d in pending:
            try:
                # compose prose + an optional fenced suggestion block (line-anchored only)
                body = d.body or ""
                hl = by_id.get(d.highlight_id) if d.highlight_id else None
                if d.suggestion and hl is not None:
                    span = max(hl.line_range.end - hl.line_range.start, 0)
                    block = f"```suggestion:-0+{span}\n{d.suggestion}\n```"
                    body = f"{body}\n\n{block}" if body.strip() else block
                res = await writeback.post_comment(snap.id, d.highlight_id, body, ref)
                # anchored comments return a discussion {id, notes:[…]}; an MR-level note returns the note
                note = (res.get("notes") or [res])[0] if isinstance(res, dict) else {}
                url = (f"{snap.mr.url}#note_{note.get('id')}"
                       if note.get("id") and snap.mr.url else None)
                thread_id = (str(res["id"]) if isinstance(res, dict) and d.highlight_id
                             and res.get("id") is not None else None)
                await actor.submit(MarkDraftPosted(highlight_id=d.highlight_id, url=url,
                                                   thread_id=thread_id), Origin.BROWSER)
                results.append({"highlight_id": d.highlight_id, "ok": True, "url": url})
            except Exception as exc:  # one bad anchor shouldn't sink the rest of the review
                results.append({"highlight_id": d.highlight_id, "ok": False, "error": str(exc)})
        posted = sum(1 for r in results if r["ok"])
        approved = False
        approve_error = None
        body = await _maybe_json(request)
        if isinstance(body, dict) and body.get("approve"):
            try:
                await writeback.approve(ref)   # capability-gated in the writer
                approved = True
            except Exception as exc:
                approve_error = str(exc)
        if kb is not None and snap.mr.sha:   # submitting a review advances the reviewed watermark
            kb.set_watermark(snap.mr.host, snap.mr.project, snap.mr.iid, snap.mr.sha)
        return JSONResponse({"posted": posted, "total": len(pending), "results": results,
                             "approved": approved, "approve_error": approve_error})

    async def mark_reviewed(request: Request) -> JSONResponse:
        """Advance the reviewed watermark to the current head without submitting (diff-versions)."""
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        snap = actor.snapshot()
        if snap.mr is None or kb is None:
            return JSONResponse({"error": "unavailable"}, status_code=400)
        kb.set_watermark(snap.mr.host, snap.mr.project, snap.mr.iid, snap.mr.sha)
        return JSONResponse({"ok": True, "watermark": snap.mr.sha})

    async def review_status(request: Request) -> JSONResponse:
        """Whether the MR advanced past the reviewer's watermark (diff-versions awareness)."""
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        snap = actor.snapshot()
        if snap.mr is None:
            return JSONResponse({"behind": False, "watermark": None, "head": None})
        wm = kb.get_watermark(snap.mr.host, snap.mr.project, snap.mr.iid) if kb is not None else None
        return JSONResponse({"head": snap.mr.sha, "watermark": wm,
                             "behind": bool(wm and wm != snap.mr.sha)})

    async def since_last(request: Request) -> JSONResponse:
        """The delta since the reviewer's watermark, with target-branch (rebase) noise excluded.
        Preferred form is a *normal* unified diff against the reviewed version (mode "diff"); it
        falls back to the raw git range-diff (mode "rangediff") only when a replay conflicts.
        Greyed (available:False) where unsupported."""
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        snap = actor.snapshot()
        if snap.mr is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        workspace = getattr(manager, "_workspace", None)
        cap = (snap.mr.capabilities or {}).get("diff_versions", False)
        if provider is None or not hasattr(provider, "mr_versions") or workspace is None or not cap:
            return JSONResponse({"available": False})   # the UI greys the "Since last review" toggle
        wm = kb.get_watermark(snap.mr.host, snap.mr.project, snap.mr.iid) if kb is not None else None
        if not wm or wm == snap.mr.sha:
            return JSONResponse({"available": True, "empty": True, "mode": "diff", "diff": ""})  # nothing new
        ref = MRRef(host=snap.mr.host, project=snap.mr.project, iid=snap.mr.iid)
        versions = await provider.mr_versions(ref)
        new = versions[0] if versions else None
        if new is None:
            return JSONResponse({"available": True, "empty": True, "mode": "diff", "diff": "",
                                 "note": "no diff versions on this MR"})
        old = next((v for v in versions if v["head_sha"] == wm), None)
        old_base = old["base_sha"] if old else None   # unknown → since_diff does a plain wm..head diff
        repo = RepoRef(host=snap.mr.host, project=snap.mr.project, clone_url=snap.mr.clone_url)
        bases = (old_base, wm, new["base_sha"], new["head_sha"])
        # preferred: a normal per-file diff against the reviewed version (never a diff-of-diffs)
        if hasattr(workspace, "since_diff"):
            res, err = None, "since-last unavailable"
            try:
                res = await workspace.since_diff(repo, *bases)
            except Exception as exc:
                err = str(exc)
            if res is not None:
                payload = {"available": True, "mode": "diff", "empty": not res["diff"].strip(),
                           "files": _split_unified_diff(res["diff"])}
                if not res.get("clean", True):
                    payload["note"] = ("the reviewed version was rebased — showing the raw diff, "
                                       "which may include target-branch changes")
                return JSONResponse(payload)
            return JSONResponse({"available": True, "error": err})
        # workspace without since_diff → the raw range-diff
        try:
            text = await workspace.range_diff(repo, old_base, wm, new["base_sha"], new["head_sha"])
        except Exception as exc:
            return JSONResponse({"available": True, "error": str(exc)})
        return JSONResponse({"available": True, "mode": "rangediff",
                             "empty": _interdiff_empty(text), "interdiff": text})

    async def commits(request: Request) -> JSONResponse:
        """The MR's commits, for per-commit review. Greyed (available:False) where unsupported."""
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        snap = actor.snapshot()
        cap = snap.mr and (snap.mr.capabilities or {}).get("commits", False)
        if snap.mr is None or provider is None or not hasattr(provider, "commits") or not cap:
            return JSONResponse({"available": False, "commits": []})
        ref = MRRef(host=snap.mr.host, project=snap.mr.project, iid=snap.mr.iid)
        return JSONResponse({"available": True, "commits": await provider.commits(ref)})

    async def commit_diff(request: Request) -> JSONResponse:
        """One commit's per-file diff (files shaped like the full diff, so the browser reuses its
        per-file renderer)."""
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        snap = actor.snapshot()
        if snap.mr is None or provider is None or not hasattr(provider, "commit_diff"):
            return JSONResponse({"error": "unavailable"}, status_code=400)
        files = await provider.commit_diff(snap.mr.project, request.path_params["sha"])
        return JSONResponse({"files": [f.model_dump(mode="json") for f in files]})

    async def _remirror_threads(actor, ref) -> list:
        """Re-pull the MR's discussions from the host and re-mirror them into session state
        (host is the single source of truth for threads). No-op without a provider."""
        if provider is None or not hasattr(provider, "fetch_threads"):
            return []
        threads = await provider.fetch_threads(ref)
        for t in threads:
            await actor.submit(ApplyThread(thread=t), Origin.SYSTEM)
        return threads

    def _thread_ref(actor):
        snap = actor.snapshot()
        if snap.mr is None:
            return None
        return MRRef(host=snap.mr.host, project=snap.mr.project, iid=snap.mr.iid)

    async def reply_thread(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if writeback is None:
            return JSONResponse({"error": "review posting unavailable"}, status_code=400)
        ref = _thread_ref(actor)
        if ref is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        body = await _maybe_json(request)
        text = body.get("body", "").strip() if isinstance(body, dict) else ""
        if not text:
            return JSONResponse({"error": "empty reply"}, status_code=400)
        try:
            await writeback.reply(ref, request.path_params["tid"], text)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await _remirror_threads(actor, ref)
        return JSONResponse({"ok": True})

    async def resolve_thread(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if writeback is None:
            return JSONResponse({"error": "review posting unavailable"}, status_code=400)
        ref = _thread_ref(actor)
        if ref is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        body = await _maybe_json(request)
        resolved = bool(body.get("resolved", True)) if isinstance(body, dict) else True
        try:
            await writeback.resolve(ref, request.path_params["tid"], resolved)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await _remirror_threads(actor, ref)
        return JSONResponse({"ok": True, "resolved": resolved})

    async def context(request: Request) -> JSONResponse:
        """The cheap, deterministic context tier for a highlighted line range (D21): host-computed
        last-touch + linked issues, no agent. Each source degrades independently (best-effort)."""
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        snap = actor.snapshot()
        if snap.mr is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        file = request.query_params.get("file", "")
        try:
            start = int(request.query_params.get("start", "0"))
            end = int(request.query_params.get("end", str(start)))
        except ValueError:
            start = end = 0
        out: dict = {"blame": [], "linked_issues": []}
        if provider is not None and file and hasattr(provider, "blame"):
            out["blame"] = await provider.blame(snap.mr.project, file, snap.mr.sha, start, end)
        if provider is not None and hasattr(provider, "linked_issues"):
            out["linked_issues"] = await provider.linked_issues(snap.mr.project, snap.mr.iid)
        return JSONResponse(out)

    async def edit_note(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if writeback is None:
            return JSONResponse({"error": "review posting unavailable"}, status_code=400)
        ref = _thread_ref(actor)
        if ref is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        body = await _maybe_json(request)
        text = body.get("body", "").strip() if isinstance(body, dict) else ""
        if not text:
            return JSONResponse({"error": "empty body"}, status_code=400)
        try:
            await writeback.edit_note(ref, request.path_params["tid"], request.path_params["nid"], text)
        except Exception as exc:  # host enforces ownership → 403 surfaces here
            return JSONResponse({"error": str(exc)}, status_code=400)
        await _remirror_threads(actor, ref)
        return JSONResponse({"ok": True})

    async def delete_note(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if writeback is None:
            return JSONResponse({"error": "review posting unavailable"}, status_code=400)
        ref = _thread_ref(actor)
        if ref is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        try:
            await writeback.delete_note(ref, request.path_params["tid"], request.path_params["nid"])
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await _remirror_threads(actor, ref)
        return JSONResponse({"ok": True})

    async def whoami(request: Request) -> JSONResponse:
        """The reviewer's own host username — so the UI can mark 'your' notes (edit/delete)."""
        return JSONResponse({"username": getattr(provider, "username", None)})

    async def refresh_threads(request: Request) -> JSONResponse:
        actor = manager.get(request.path_params["id"])
        if actor is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        ref = _thread_ref(actor)
        if ref is None:
            return JSONResponse({"error": "no MR loaded"}, status_code=400)
        # Full re-sync from the host (the single source of truth): MR head + diff + discussions.
        # A thread-only refresh left snap.mr.sha frozen at session creation, so an updated MR was
        # never noticed — review-status compared the watermark against a stale head equal to it and
        # "Since last review" (diff-versions) never engaged. Re-pull metadata + files when the host
        # supports a full load; fall back to a thread-only re-mirror otherwise.
        if provider is not None and hasattr(provider, "load"):
            payload = await provider.load(ref)
            await actor.submit(ApplyMRMetadata(mr=payload.mr), Origin.SYSTEM)
            await actor.submit(ApplyFiles(files=payload.files), Origin.SYSTEM)
            for t in payload.threads:
                await actor.submit(ApplyThread(thread=t), Origin.SYSTEM)
            return JSONResponse({"threads": len(payload.threads), "head": payload.mr.sha})
        threads = await _remirror_threads(actor, ref)
        return JSONResponse({"threads": len(threads)})

    async def end_session(request: Request) -> JSONResponse:
        try:
            await manager.end(request.path_params["id"])
        except KeyError:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        return JSONResponse({"ok": True})

    async def stream(ws: WebSocket) -> None:
        await ws.accept()
        actor = manager.get(ws.path_params["id"])
        if actor is None:
            await ws.close(code=4404)
            return
        try:
            since = int(ws.query_params.get("since", "0"))
        except ValueError:
            since = 0
        try:
            async for event in actor.subscribe(since=since):
                await ws.send_text(event.model_dump_json())
        except WebSocketDisconnect:
            return
        except Exception:
            pass  # transport boundary — don't let a send/serialize error escape as a task crash
        try:
            await ws.close()
        except Exception:  # pragma: no cover - already closing/closed
            pass

    return [
        Route("/api/queue", review_queue, methods=["GET"]),
        Route("/api/search", search, methods=["GET"]),
        Route("/api/lookup", open_lookup, methods=["POST"]),
        Route("/api/lookup/{id}", poll_lookup, methods=["GET"]),
        Route("/api/activity", activity, methods=["GET"]),
        Route("/api/sessions/{id}/repo-tree", repo_tree, methods=["GET"]),
        Route("/api/sessions/{id}/file", get_file, methods=["GET"]),
        Route("/api/sessions", create_session, methods=["POST"]),
        Route("/api/sessions", list_sessions, methods=["GET"]),
        Route("/api/sessions/{id}", get_session, methods=["GET"]),
        Route("/api/sessions/{id}", end_session, methods=["DELETE"]),
        Route("/api/sessions/{id}/commands", submit_command, methods=["POST"]),
        Route("/api/sessions/{id}/submit-review", submit_review, methods=["POST"]),
        Route("/api/sessions/{id}/threads/{tid}/reply", reply_thread, methods=["POST"]),
        Route("/api/sessions/{id}/threads/{tid}/resolve", resolve_thread, methods=["POST"]),
        Route("/api/sessions/{id}/threads/{tid}/notes/{nid}/edit", edit_note, methods=["POST"]),
        Route("/api/sessions/{id}/threads/{tid}/notes/{nid}/delete", delete_note, methods=["POST"]),
        Route("/api/sessions/{id}/refresh-threads", refresh_threads, methods=["POST"]),
        Route("/api/sessions/{id}/mark-reviewed", mark_reviewed, methods=["POST"]),
        Route("/api/sessions/{id}/review-status", review_status, methods=["GET"]),
        Route("/api/sessions/{id}/since-last", since_last, methods=["GET"]),
        Route("/api/sessions/{id}/commits", commits, methods=["GET"]),
        Route("/api/sessions/{id}/commit/{sha}", commit_diff, methods=["GET"]),
        Route("/api/me", whoami, methods=["GET"]),
        Route("/api/sessions/{id}/context", context, methods=["GET"]),
        WebSocketRoute("/api/sessions/{id}/stream", stream),
    ]


def _split_unified_diff(text: str) -> list:
    """Split `git diff` output into per-file entries shaped like FileEntry (path, old_path,
    change_type, hunks:[{diff}]) so the browser renders the since-last diff file-by-file, exactly
    like the full diff. The hunk `diff` is the text from the first @@ onward (what the row renderer
    consumes); the file-header lines are parsed for metadata, not shown."""
    files: list = []
    cur = None
    inhunk = False
    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            cur = {"path": "", "old_path": None, "change_type": "modified", "hunks": [{"diff": ""}]}
            rest = line[len("diff --git "):]
            if rest.startswith("a/"):
                idx = rest.find(" b/")
                if idx != -1:
                    cur["old_path"], cur["path"] = rest[2:idx], rest[idx + 3:]
            files.append(cur)
            inhunk = False
        elif cur is None:
            continue
        elif inhunk:
            cur["hunks"][0]["diff"] += line + "\n"
        elif line.startswith("@@"):
            inhunk = True
            cur["hunks"][0]["diff"] += line + "\n"
        elif line.startswith("new file"):
            cur["change_type"] = "added"
        elif line.startswith("deleted file"):
            cur["change_type"] = "deleted"
        elif line.startswith("rename from "):
            cur["old_path"] = line[len("rename from "):]; cur["change_type"] = "renamed"
        elif line.startswith("rename to "):
            cur["path"] = line[len("rename to "):]; cur["change_type"] = "renamed"
        elif line.startswith("--- ") and not line.endswith("/dev/null"):
            cur["old_path"] = line[6:] if line.startswith("--- a/") else line[4:]
        elif line.startswith("+++ ") and not line.endswith("/dev/null"):
            cur["path"] = line[6:] if line.startswith("+++ b/") else line[4:]
    for f in files:
        if not f["path"] and f["old_path"]:      # deletion: +++ was /dev/null
            f["path"] = f["old_path"]
        if f["old_path"] == f["path"]:
            f["old_path"] = None
    return files


def _interdiff_empty(text: str) -> bool:
    """A range-diff always prints a per-commit summary line, so 'empty' means no actual patch
    content evolved — a pure rebase (all commits unchanged) rather than a real edit."""
    for raw in text.splitlines():
        s = raw.lstrip()
        if s.startswith(("@@ ", "+", "-")) or " ! " in raw or " < " in raw:
            return False
    return True


async def _maybe_json(request: Request):
    try:
        return await request.json()
    except Exception:
        return {}
