"""Sending a prepared review: post what is drafted, mirror what came back, then approve.

One unit rather than three calls a client makes in order, because the order is the interesting
part. A comment the host rejects must not sink the rest of the review. The discussions have to be
re-mirrored before the reviewer goes looking for the comment they just posted, or the inline
"your comment" block has no thread to find. And approving comes after posting, so a review that
half-failed is still approved on purpose rather than by an ordering accident.

`Writeback` stays the thin contract with the host. This is the sequence over it, so a second client
sending a review cannot get the sequence subtly different.
"""
from __future__ import annotations

from review_mate.contracts import MRRef
from review_mate.forges import Forges
from review_mate.session.commands import MarkDraftPosted, PostMessage, ReplaceThreads
from review_mate.session.state import DraftStatus, Origin, Subject, SubjectKind


def _nowhere_to_post(snapshot) -> bool:
    """Whether this review has no forge to send comments to.

    Read off the capabilities rather than the host's name: a branch on this machine is the case
    that exists today, and any host that cannot take a comment is in the same position. Distinct
    from a writer that could not be built — there the reviewer means the merge request, and quietly
    sending their review somewhere else would be a worse answer than saying it is unavailable.
    """
    capabilities = (snapshot.mr.capabilities or {}) if snapshot.mr else {}
    # Both said, and both said no. An absent capability is not a claim that nothing is possible —
    # a host that advertises half of itself would otherwise have its reviews quietly redirected.
    return (capabilities.get("inline_comments") is False
            and capabilities.get("mr_comments") is False)


class ReviewSubmitter:
    def __init__(self, manager, writeback, provider=None, kb=None) -> None:
        self._manager = manager
        self._writeback = writeback
        self._forges = Forges.of(provider)
        self._kb = kb

    async def submit(self, session_id: str, *, approve: bool = False) -> dict:
        """Post every pending draft. Returns what landed, per draft, and what approving did.

        Never raises for a draft the host refused: the reviewer gets the rest of their review
        posted and a per-comment error, rather than an all-or-nothing failure they cannot act on.
        """
        writer = self._manager.get(session_id)
        if writer is None:
            return {"error": "unknown session"}
        snapshot = writer.snapshot()
        if snapshot.mr is None:
            return {"error": "no MR loaded"}
        pending = [d for d in snapshot.drafts if d.status is DraftStatus.DRAFT]

        # A review of a branch on this machine has nowhere to post: there is no merge request and
        # nobody else reading it. What there is, is the agent that wrote the code — so sending the
        # review hands it the comments rather than dropping them. They travel as messages on the
        # subjects they were written against, which is where the answer will appear and what puts
        # them in the agent's backlog; nothing new had to be invented to carry them.
        if _nowhere_to_post(snapshot):
            return await self._hand_over(writer, snapshot, pending)

        if self._writeback is None:
            return {"error": "review posting unavailable"}

        ref = MRRef(host=snapshot.mr.host, project=snapshot.mr.project, iid=snapshot.mr.iid)
        by_id = {h.id: h for h in snapshot.highlights}
        results = [await self._post(writer, snapshot, ref, draft, by_id) for draft in pending]
        posted = sum(1 for r in results if r["ok"])

        if posted:
            await self._remirror(writer, ref)
        approved, approve_error = await self._approve(ref) if approve else (False, None)
        if self._kb is not None and snapshot.mr.sha:   # submitting advances the reviewed watermark
            self._kb.set_watermark(snapshot.mr.host, snapshot.mr.project, snapshot.mr.iid,
                                   snapshot.mr.sha)
        return {"posted": posted, "total": len(pending), "results": results,
                "approved": approved, "approve_error": approve_error}

    async def _hand_over(self, writer, snapshot, pending) -> dict:
        """Give the comments to the agent, as messages on the subjects they were written against.

        The same two acts as posting to a forge, in the same order: the comment goes somewhere, and
        the draft is marked as having gone. A draft is private prose until the reviewer sends it,
        and sending is still what changes that — the recipient is the agent rather than a merge
        request, which is the only difference.
        """
        by_id = {h.id: h for h in snapshot.highlights}
        results = []
        for draft in pending:
            body = self._compose(draft, by_id.get(draft.highlight_id) if draft.highlight_id else None)
            anchor = (Subject(kind=SubjectKind.HIGHLIGHT, id=draft.highlight_id)
                      if draft.highlight_id else None)
            try:
                await writer.submit(PostMessage(body=body, anchor=anchor), Origin.BROWSER)
                await writer.submit(MarkDraftPosted(highlight_id=draft.highlight_id,
                                                    url=None, thread_id=None), Origin.BROWSER)
                results.append({"highlight_id": draft.highlight_id, "ok": True, "url": None})
            except Exception as exc:    # one bad subject must not sink the rest of the review
                results.append({"highlight_id": draft.highlight_id, "ok": False, "error": str(exc)})
        posted = sum(1 for r in results if r["ok"])
        if self._kb is not None and snapshot.mr.sha:
            self._kb.set_watermark(snapshot.mr.host, snapshot.mr.project, snapshot.mr.iid,
                                   snapshot.mr.sha)
        return {"posted": posted, "total": len(pending), "results": results,
                "approved": False, "approve_error": None, "handed_to": "agent"}

    # --- the steps -----------------------------------------------------------

    async def _post(self, writer, snapshot, ref: MRRef, draft, by_id) -> dict:
        try:
            body = self._compose(draft, by_id.get(draft.highlight_id) if draft.highlight_id else None)
            answer = await self._writeback.post_comment(snapshot.id, draft.highlight_id, body, ref)
            # an anchored comment comes back as a discussion {id, notes:[…]}; an MR-level note
            # comes back as the note itself
            note = (answer.get("notes") or [answer])[0] if isinstance(answer, dict) else {}
            url = (f"{snapshot.mr.url}#note_{note.get('id')}"
                   if note.get("id") and snapshot.mr.url else None)
            thread_id = (str(answer["id"]) if isinstance(answer, dict) and draft.highlight_id
                         and answer.get("id") is not None else None)
            await writer.submit(MarkDraftPosted(highlight_id=draft.highlight_id, url=url,
                                               thread_id=thread_id), Origin.BROWSER)
            return {"highlight_id": draft.highlight_id, "ok": True, "url": url}
        except Exception as exc:   # one bad anchor must not sink the rest of the review
            return {"highlight_id": draft.highlight_id, "ok": False, "error": str(exc)}

    @staticmethod
    def _compose(draft, highlight) -> str:
        """The reviewer's prose, plus a fenced suggestion block when they wrote one.

        A suggestion needs the anchored line span to say what it replaces, so an MR-level draft
        carries prose only however it was written.
        """
        body = draft.body or ""
        if not (draft.suggestion and highlight is not None):
            return body
        span = max(highlight.line_range.end - highlight.line_range.start, 0)
        block = f"```suggestion:-0+{span}\n{draft.suggestion}\n```"
        return f"{body}\n\n{block}" if body.strip() else block

    async def _remirror(self, writer, ref: MRRef) -> None:
        """Re-pull the discussions so a just-posted comment is a thread the reviewer can resolve.

        Best-effort: the review is already posted by this point, and failing here would report a
        successful submission as an error.
        """
        forge = self._forges.pick(ref.host) if self._forges else None
        if forge is None or not hasattr(forge, "fetch_threads"):
            return
        try:
            threads = await forge.fetch_threads(ref)
        except Exception:
            return
        # the host is the single source of truth for threads, so this reconciles wholesale
        await writer.submit(ReplaceThreads(threads=threads), Origin.SYSTEM)

    async def _approve(self, ref: MRRef) -> tuple[bool, str | None]:
        try:
            await self._writeback.approve(ref)   # capability-gated in the writer
            return True, None
        except Exception as exc:
            return False, str(exc)
