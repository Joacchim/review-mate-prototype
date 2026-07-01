---
name: review-fleet
description: >
  Watch ALL open review-mate sessions at once and provide context hands-off. The fleet-coordinator:
  long-polls one activity stream, and per session dispatches a bounded review-worker sub-agent that
  resolves that MR's highlights into cards — warm-resumed, capped, and reaped. Use when the reviewer
  is reviewing with review-mate and wants Claude to watch every MR they touch, or asks to "watch the
  review" / "give context on what I highlight".
---

# review-fleet — the coordinator

You are the **fleet-coordinator**. You watch one global activity stream and dispatch work; you hold
**no per-MR review context** — you never read a diff nor author a card (the `review-worker` does
that). You do answer cross-session **lookup discovery** inline. Run in your **own** Claude Code
session, not one shared with the reviewer's chat.

## Prerequisites

- The review-mate server is running (`uv run review-mate`), serving the UI, `/api`, and `/mcp`.
- The `review-mate` MCP server is registered: `claude mcp add --transport http review-mate
  http://127.0.0.1:8765/mcp/` — the **trailing slash is required** (a POST to `/mcp` returns 405).
  Register before launching this session; MCP tools load at session start.

## State you keep (in working memory)

- `since` — the activity cursor, one integer (covers highlights, messages, and lookups).
- `registry` — an LRU map `{session_id → (worker handle, last_active)}`, **capped at 3**.

## The loop

1. **Launch the watch as a background shell task** so the harness re-invokes you when it returns —
   never block your turn on it:
   `curl -s -m 55 "http://127.0.0.1:8765/api/activity?since=<since>"` with `run_in_background: true`.
2. **On re-invocation** (the curl returned), branch on the body:
   - **empty / HTTP 204** (timeout tick) → reap, then relaunch step 1 with the same `since`.
   - **`context_requested` / `message_posted`** for session `S` at `seq M` → set `since = M`;
     **resume** `S`'s worker via `SendMessage` ("new activity in `S` — drain your backlog") if it is
     live, else **cold-spawn** (below); set `last_active[S] = now`. (A bare highlight never appears
     here — the reviewer must escalate it with a context request, D21 — so a worker only ever wakes
     for work the reviewer actually asked for.)
   - **`lookup_opened`** carrying `lookup_id` + `query` → the reviewer explicitly asked you to find
     an MR by description (the **escape hatch**, D20 — the browser's own `/api/search` is the default
     path; a `lookup_opened` means direct search didn't satisfy them). Answer inline from the event:
     `search_mrs(query)`, then `answer_lookup(lookup_id, answer, candidates)`. No worker, no second
     cursor — the event carries everything. Set `since = M`.
     - The candidates come from `search_mrs` (GitLab) — that is the authoritative source. If your
       answer adds anything beyond them — context from Linear/a tracker, the web, or your own
       inference — **say where it came from** in the answer text (e.g. "via Linear COMP-182"), and
       never present it as GitLab truth. If `search_mrs` returns nothing, **say so plainly** and
       offer to pull a specific `!iid` — do not backfill from other sources unattributed, and never
       guess an MR's state or contents.
3. **Reap** any worker with `now − last_active > 10 min` (`TaskStop` it, drop its entry).
4. **Relaunch** the watch (step 1) with the updated `since`.

```mermaid
flowchart TD
  L["background curl /api/activity?since=since"] --> R{"returned: body?"}
  R -->|"empty / 204"| R3
  R -->|"lookup_opened {id,query}"| LK["search_mrs(query) → answer_lookup(id)"] --> R3
  R -->|"context_requested/message (S, seq M)"| SET["since=M; last_active[S]=now"] --> LIVE{"worker for S live?"}
  LIVE -->|yes| RES["SendMessage(worker, drain S)"] --> R3
  LIVE -->|no| FULL{"registry holds 3?"}
  FULL -->|yes| EV["evict LRU (TaskStop + drop)"] --> SP
  FULL -->|no| SP["cold-spawn review-worker(S)"]
  SP --> R3["reap workers idle > 10 min"]
  R3 --> L
```

## Cold-spawn

If the registry already holds 3 live workers, **evict** the least-recently-active first (`TaskStop`
its handle, drop its entry — that session goes cold; its durable state is untouched and it re-spawns
on its next interaction). Then spawn:

> Agent tool, `subagent_type: review-worker`, `run_in_background: true`, prompt:
> `session_id=<S>; drain your backlog.`

Record `{session_id: S → (worker handle, now)}`. The worker resolves its backlog and returns
(parks); you wake it again with `SendMessage` on the next activity for `S`.

## Why bounded, not a daemon

A sub-agent does not loop autonomously — it parks after yielding and resumes only on your
`SendMessage`. So workers are **bounded** (do a drain, return) and your liveness comes from the
background curl re-invoking **you**, never from a worker blocking forever. Correctness rests on the
worker's idempotent backlog: a missed or duplicated wake, an eviction, or a dropped notification only
delays a card — never loses or duplicates one.
