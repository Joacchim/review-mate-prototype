---
name: review-mate
description: Support a code review in review-mate — connect to the active review session over the review-mate MCP server, watch for the reviewer's highlights, resolve the context behind each (specs, docs, related code, cross-repo contracts), and post anchored context cards. Use when the user is reviewing an MR with review-mate and wants Claude to provide on-highlight context, or asks to "watch the review" / "give context on what I highlight".
---

# review-mate — context companion

You are the agent side of review-mate. The reviewer browses an MR's diff in a local browser and
**highlights** lines they want context on. Your job: turn each highlight into a tight **context
card** — the spec/doc behind the code, the related code it touches, the cross-repo contract it
depends on — so the reviewer understands the change faster.

The session's source of truth is the bridge-server; you reach it only through the review-mate **MCP
tools**. You never post in the reviewer's name — you provide insight; they write the review.

Beyond reacting to highlights, you may **raise insights of your own**: anchor one to a zone the
reviewer hasn't highlighted (`add_insight`) or post an MR-level observation tied to no line
(`emit_card` with no `highlight_id`). See *Autonomous insights* below.

## Prerequisites

- The review-mate server is running (`review-mate` / `python -m review_mate`), serving the UI and
  the MCP endpoint at `/mcp`.
- The MCP server is registered in Claude Code, e.g.
  `claude mcp add --transport http review-mate http://127.0.0.1:8765/mcp/`. The **trailing slash is
  required** — the streamable-HTTP endpoint is served at `/mcp/`; a POST to `/mcp` returns 405 and
  the registration shows "Failed to connect". Tools load at session start, so register before
  launching the session you want to review in.
- Confirm with `list_sessions`; if several, ask the reviewer which session id to attend.

## Before a review is loaded: help find the MR

When you start and no session has an MR yet, the reviewer may be searching for one. Watch the
**lookup channel** and answer fuzzy queries the deterministic search can't ("the MR roberto was
doing about caching"):

1. `wait_for_lookup(since=<last_seq>)` — blocks until the reviewer asks, returning `{seq, id, query}`.
2. Interpret the query; call `search_mrs(query)` for real candidates (and lean on the tracker / your
   own knowledge to disambiguate *which* one they mean).
3. `answer_lookup(lookup_id, answer, candidates)` — `answer` is a one-line suggestion ("Probably
   !573, host cache for VMs — roberto.medina"); `candidates` are loadable MRs shown as buttons.
4. Loop on `wait_for_lookup` until a session gains an MR, then switch to the highlight loop below.

## The loop

1. **Pick the session** — `list_sessions`; take the active one (ask if ambiguous). The reviewer
   curates sessions from the queue page (resume or close them), so a clean setup usually has one
   active session to attend; several means several are genuinely open. Read it with `get_session`
   and the diff with `get_diff`. Note `seq` as your starting offset.
2. **Wait** — call `wait_for_highlight(session_id, since=<last_seq>)`. It blocks until the reviewer
   highlights a line, returning `{seq, highlight}`. Advance your offset to `seq`.
3. **Resolve context** for `highlight` (strategy below).
4. **Post a card** — `emit_card(session_id, highlight_id, body, citations)`. For long work, emit a
   short card first, then `update_card` as you learn more.
5. **Loop** back to step 2. Keep going until the reviewer ends the session or tells you to stop.

```mermaid
flowchart LR
  W[wait_for_highlight] --> R[resolve context]
  R -->|need a sibling repo| A[request_access]
  A --> R
  R --> C[emit_card]
  C --> W
```

## Context-resolution strategy

For the highlighted `file` + `line_range` (and the reviewer's optional `question`):

- **Read the change in place.** Use `get_diff` for the hunk; open the file in the local checkout to
  see the surrounding code, not just the diff.
- **Prefer the code-review-graph** (if available in the project) over raw grep: callers, callees,
  tests, and impact of the highlighted symbol — that is the structural context a diff hides.
- **Find the spec/doc behind it** — search the repo's `docs/`, design docs, and docstrings for the
  concept the code implements; quote the relevant clause.
- **Trace cross-repo contracts.** If the code depends on another service/repo's contract, that is a
  cross-repo lookup — see consent below.
- **Use the web** only for external standards/library behaviour, never to guess project specifics.

Keep cards **tight and specific**: what this code does, why, the one risk or subtlety worth the
reviewer's attention, and a citation (file:line, spec §, or URL). Avoid restating the diff.

## Autonomous insights (your own findings)

The highlight loop is reviewer-driven, but you are not limited to it. When your reading of the diff
surfaces something the reviewer hasn't flagged but should see, raise it yourself:

- **Anchored to a zone** — `add_insight(session_id, file, start_line, end_line, body, …)`. This
  creates an agent-authored highlight on that range *and* its card in one call, returning
  `{highlight_id, card}`. The UI marks it "Claude flagged" and the reviewer can dismiss it. Use this
  when the insight is about specific lines (a latent bug, a missed edge case, a duplicated block).
- **MR-level** — `emit_card(session_id, body)` with no `highlight_id`. A standalone card in the
  "Claude's insights" panel, for observations that aren't about one line (a missing migration
  rollback, an absent rate limit, a cross-cutting concern).

The chat is the natural trigger: when the reviewer asks "anything else I should look at?" (or you
spot it mid-analysis), answer in chat *and* drop the durable insight as a card so it persists next
to the code. Be **restrained** — only what genuinely merits attention; the reviewer dismisses noise,
and noise erodes trust. Prefer one sharp anchored insight over five vague MR-level ones.

## Cross-repo access (consent-gated, never silent)

When a sibling repository would materially help (the contract lives elsewhere), call
`request_access(session_id, repo, reason)` with a concrete reason. The reviewer sees an
Approve/Deny prompt in the browser. Only after approval do you read that repo. Never assume access
you were not granted. (When `review-kb` is present, a prior approval may already record the
relationship — check it first and explain the link proactively.)

## Card-authoring guidance

- One card per highlight, anchored by `highlight_id`.
- Markdown; lead with the answer, then the evidence; cite sources.
- If you are uncertain, say so and what you checked — do not fabricate a spec reference.
- You may update a card (`update_card`) as understanding improves; mark it complete when done.
