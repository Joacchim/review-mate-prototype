"""The MCP server — a thin FastMCP wrapper exposing AgentBridge methods as MCP tools.

Each tool delegates to the bridge; the bridge (not this module) holds the logic, so the tools stay
declarative. This is what a Claude Code session connects to as the agent contract.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from review_mate.mcp.bridge import AgentBridge
from review_mate.session.state import Subject, SubjectKind


def build_mcp_server(bridge: AgentBridge, *, mountable: bool = False) -> FastMCP:
    # `mountable` configures the streamable-HTTP app to serve at the mount root, statelessly,
    # so it can be mounted inside bridge-server and share its SessionManager.
    if mountable:
        mcp = FastMCP("review-mate", stateless_http=True, streamable_http_path="/")
    else:
        mcp = FastMCP("review-mate")

    @mcp.tool()
    def list_sessions() -> list[dict]:
        """List the review sessions currently open."""
        return [s.model_dump(mode="json") for s in bridge.list_sessions()]

    @mcp.tool()
    async def open_local_review(path: str, branch: str, base: str = "") -> dict:
        """Open a review of a branch in a repository on this machine, and get the link to it.

        For work you have just written and nobody has seen: the reviewer reads it here, comments on
        it here, and you answer them here — before it becomes a merge request anyone else is asked
        to look at. The diff is the branch against where it left its base, the same one a merge
        request would show. `base` defaults to whatever the repository merges into.

        **Give the reviewer the `url`, not the session id.** A link is somewhere to look; an id is
        homework. Then watch the session as you would any other — their comments arrive as messages
        on subjects, and `get_session` lists what they are waiting on you for in `chat.asks`.

        Nothing is copied: the review points at the working repository, so you can edit the code you
        are being asked about and the diff follows.
        """
        return await bridge.open_local_review(path, branch, base)

    @mcp.tool()
    async def get_session(session_id: str) -> dict:
        """The session as the reviewer sees it: the merge request, the annotations, the chats and
        their `asks`, the discussions, and the consent list.

        `chat.asks` is your backlog — what the reviewer is waiting on you for, already worked out.
        Do not re-derive it from the highlights and messages; that predicate lives in one place and
        this is it. An ask of kind `check` carries a `note`: the words to verify, which are the
        reviewer's own comment or something you said. Answer it in the chat on its subject.

        `checkout_path` is the on-disk worktree of the merge request — the root for Read, Grep, LSP
        and the code-graph CLI. The diff is not here: `get_diff` has it.

        What the reviewer has prepared but not yet posted is deliberately absent. Once they post it,
        it is a discussion and you will find it in `threads`.
        """
        return await bridge.view(session_id)

    @mcp.tool()
    async def get_diff(session_id: str, path: str | None = None) -> dict:
        """What changed in this merge request: the file list with per-file line counts.

        This is a map, not the change itself. `get_session` gives you `checkout_path` — a real git
        worktree of the merge request at its head — so read the files there, with their imports and
        their callers around them, rather than from a diff. `mr.diff_refs` carries the base and head
        shas, so from that checkout you can also recover any old side (`git show <base>:<path>`) or
        diff any pair yourself, including "what arrived since the reviewer last looked".

        Pass `path` for one file's unified diff text. That is the fallback for a session with no
        checkout — materialization is best-effort and a clone or auth failure leaves it unset. With
        a checkout in hand it is a worse copy of what is already on disk: no surrounding lines,
        nothing greppable, and it costs context whether or not you read it.
        """
        return await bridge.diff(session_id, path=path)

    @mcp.tool()
    async def wait_for_highlight(session_id: str, since: int = 0,
                                 timeout: float | None = 30.0) -> dict | None:
        """Wait for the reviewer's next highlight after `since`; returns {seq, highlight} or null."""
        result = await bridge.wait_for_highlight(session_id, since=since, timeout=timeout)
        if result is None:
            return None
        return {"seq": result["seq"], "highlight": result["highlight"].model_dump(mode="json")}

    @mcp.tool()
    async def emit_card(session_id: str, body: str, highlight_id: str | None = None,
                        citations: list[str] | None = None, theme: str | None = None,
                        criticality: str | None = None, about: str = "") -> dict:
        """Post a context card (markdown). Anchor it to a highlight by id, or omit highlight_id
        for an MR-level insight (a standalone card not tied to any zone).

        Classify it with `theme` and `criticality` — give both or neither. The reviewer sorts and
        filters on the pair, so a change with forty findings can be read highest-first or narrowed
        to one kind, which is the whole point of saying anything about a finding beyond its text.

        - `theme`: bug · security · performance · test · docs · style · naming · complexity
        - `criticality`: low · medium · high

        `about` is one short line the two words cannot carry — "the retry path", "only on cold
        start" — so a row is worth reading before the card is opened.

        Judge honestly. Everything marked high is the same as nothing marked high, and a real bug
        marked low is the failure that costs something. A nitpick is `style` at `low`; there is no
        separate level for "you may ignore this", because you are not the one who decides that.
        """
        return (await bridge.emit_card(session_id, highlight_id, body, citations,
                                       theme=theme, criticality=criticality,
                                       about=about)).model_dump()

    @mcp.tool()
    async def record_addressed(session_id: str, subject_kind: str, subject_id: str, sha: str,
                               summary: str = "") -> dict:
        """Say that you changed the code in answer to something, and what it became.

        The other kind of answer. When a reviewer writes "this retry is unbounded" they usually mean
        fix it, and a card explaining that it is unbounded is not that. Commit the change, then
        record it here against the subject it answers, with the new sha and one line saying what you
        did.

        Recording it is what makes the change legible. Five open comments and one new commit is a
        matching exercise the reviewer should not have to do — and it is what stops their annotations
        filling with stale warnings about their own progress, because a subject whose code moved
        with one of these against it moved *because* you fixed it.

        `subject_kind` is `highlight`, `insight` or `thread`. Answer in the chat as well if
        there is anything to say; the record is not a substitute for talking to them.
        """
        return (await bridge.record_addressed(session_id, subject_kind, subject_id, sha,
                                              summary)).model_dump()

    @mcp.tool()
    async def label_card(session_id: str, card_id: str, theme: str, criticality: str,
                         about: str = "") -> dict:
        """Classify an insight you already posted, or change how you classified it.

        Use it when you learn more — a finding you called `medium` turns out to be reachable from
        the public API. The reviewer can relabel too, and their word replaces yours: if they moved
        your `bug`/`high` to `style`/`low`, that is an answer, not something to set back.
        """
        return (await bridge.label_card(session_id, card_id, theme, criticality,
                                        about)).model_dump()

    @mcp.tool()
    async def add_insight(session_id: str, file: str, start_line: int, end_line: int, body: str,
                          side: str = "new", citations: list[str] | None = None) -> dict:
        """Proactively flag a zone the reviewer did not highlight: creates an agent-authored
        highlight on file:start_line-end_line and a card on it. Returns {highlight_id, card}.
        Use sparingly for things genuinely worth the reviewer's attention; they can dismiss it."""
        return await bridge.add_insight(session_id, file, start_line, end_line, body,
                                        side=side, citations=citations)

    @mcp.tool()
    async def update_card(session_id: str, card_id: str, body: str | None = None,
                          status: str | None = None) -> dict:
        """Update a previously emitted card; status is 'streaming' or 'complete'."""
        from review_mate.session.state import CardStatus
        st = CardStatus(status) if status else None
        return (await bridge.update_card(session_id, card_id, body=body, status=st)).model_dump()

    @mcp.tool()
    async def request_access(session_id: str, repo: str, reason: str) -> dict:
        """Ask the reviewer to approve read access to another local repository."""
        return (await bridge.request_access(session_id, repo, reason)).model_dump()

    @mcp.tool()
    async def access_state(session_id: str) -> list[dict]:
        """What you asked to read, what the reviewer answered, and where it landed.

        One row per request: `status` is theirs (pending / approved / denied) and `state` is what
        the approval produced (materializing / ready / failed, or null if nothing has started).
        Read `path` only when `state` is "ready" — that is the checkout you may read, and the only
        one. A denied repository stays denied; asking again for what was refused is a worse move
        than working without it, and says so to the reviewer.
        """
        return await bridge.access_state(session_id)

    @mcp.tool()
    async def wait_for_access(session_id: str, since: int = 0,
                              timeout: float | None = 30.0) -> dict | None:
        """Wait for a consent request to move — decided, or materialized. Null on timeout.

        Returns as readily on a refusal as on an approval. Do not treat a timeout as a no: it means
        nobody has answered yet, and the reviewer may be mid-review. Say what you can without the
        repository rather than waiting on it.
        """
        return await bridge.wait_for_access(session_id, since=since, timeout=timeout)

    @mcp.tool()
    async def post_message(session_id: str, body: str, anchor_kind: str | None = None,
                           anchor_id: str | None = None) -> dict:
        """Post a chat message to the reviewer (the agent side of the chat).

        Leave the anchor out to speak in the review's own chat. To answer where the
        reviewer asked, name the subject: `anchor_kind` is highlight, insight or thread, and
        `anchor_id` is that row's id — both as they arrive on an inbound message's `anchor`.
        """
        anchor = (Subject(kind=SubjectKind(anchor_kind), id=anchor_id)
                  if anchor_kind is not None and anchor_id is not None else None)
        return (await bridge.post_message(session_id, body, anchor)).model_dump()

    @mcp.tool()
    async def wait_for_message(session_id: str, since: int = 0,
                               timeout: float | None = 60.0) -> dict | None:
        """Wait for the reviewer's next chat message after `since`; returns {seq, message} or null."""
        return await bridge.wait_for_message(session_id, since=since, timeout=timeout)

    @mcp.tool()
    async def search_mrs(query: str) -> list[dict]:
        """Search the host for MRs matching a free-text query; returns loadable candidates
        [{host, project, iid, title, url}]. Use to back a lookup answer with real MRs."""
        return await bridge.search_mrs(query)

    @mcp.tool()
    async def wait_for_lookup(since: int = 0, timeout: float | None = 60.0) -> dict | None:
        """Wait for the reviewer's next MR-lookup request after `since`; returns {seq, id, query}
        or null. Use this before a review is loaded to help the reviewer find an MR: interpret the
        (often fuzzy) query, call search_mrs, then answer_lookup with your pick and candidates."""
        return await bridge.wait_for_lookup(since=since, timeout=timeout)

    @mcp.tool()
    def answer_lookup(lookup_id: str, answer: str, candidates: list[dict] | None = None) -> dict:
        """Answer a lookup request the reviewer is waiting on: `answer` is your prose suggestion;
        `candidates` are loadable MRs [{host, project, iid, title, url}] shown as buttons."""
        return bridge.answer_lookup(lookup_id, answer, candidates)

    return mcp
