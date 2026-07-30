# review-mate

> [!WARNING]
> **This repository is a prototype.** The UX/UI is not yet refined and is sometimes clunky —
> rough edges, half-finished affordances and abrupt interactions are expected rather than
> surprising. It is shared in that state on purpose. **Feedback and merge/pull requests are very
> welcome**, especially on the parts that get in your way.

A code-review companion. Browse a merge request's diff in a local browser, highlight the code you
have questions about, and get context back — cheap deterministic context from the host for free, and
a deeper answer from Claude when you ask for it. The comments you write flow back to the host from
the same place you read the code.

## How it works

review-mate runs as a local server that the browser talks to, and that Claude Code can attach to
over MCP:

```
browser  ⟷  local Python server (UI + /api + /mcp)  ⟷  Claude Code (MCP)
                        ⟷  GitLab
```

The design splits into two planes, and the split is load-bearing:

- **The host plane** — everything that talks to the forge: the diff, the review queue, search,
  comments, discussion threads, approvals. The server does all of it directly. **The whole review
  workflow works with no agent attached.**
- **The agent plane** — context cards, insights, chat, cross-repo lookups. Purely additive. Claude
  enriches the review; it never carries a host action the server can perform itself.

Sessions are event-sourced (an append-only log per session under `~/.review-mate/`), so a review
survives a server restart and resumes where you left it.

## Supported features

**Reading a change**

- Per-file diff with a file tree, unified or side-by-side, with basic syntax highlighting
- Unfold the context between hunks, or show the rest of the file, fetched from the MR head
- Browse files the MR did not touch — the whole repo tree is available, not just the diff
- Render Markdown files as formatted text instead of a diff
- Renamed files shown as a path divergence (`test/{a,b}/file.py`), both ends visible
- Per-commit review: step through the MR one commit at a time
- **Since your last review** — mark a baseline, and later see a normal per-file diff of what the
  author changed since, with target-branch/rebase noise excluded
- Refresh a session against the live MR (metadata, diff, threads) without reopening it

**Finding work**

- The review queue: MRs where you are reviewer or assignee
- A session hub listing your open reviews, with per-review status (updated, discussions, merged,
  closed) and a close action
- Direct load by MR URL or `group/project!iid`
- Search GitLab for an MR by title/description, with live suggestions
- **Track** an MR from the queue to start its review session without leaving the queue
- Every entry is a real link — middle-click or open-in-new-tab to fan reviews out into tabs

**Asking questions about code**

- Highlight a line range by dragging over the diff
- A **cheap context tier** answers immediately with host-computed facts (last touch / blame, linked
  issues) — no agent involved, no agent turn spent
- Escalate a highlight to Claude explicitly (**✦ Ask Claude for context**) when you want more; the
  answer comes back as a card anchored to the highlight
- Chat with Claude in the session, referencing cards by number
- Claude can volunteer insights of its own, anchored or MR-level, which you can dismiss
- Cross-repo context is consent-gated: Claude asks for access to a sibling repository and you
  approve or deny it in the UI
- A presence indicator says whether an agent is actually watching, so a slow answer never looks
  like a lost one

**Writing the review**

- Draft comments per highlight, or at MR level, and edit them before anything is sent
- Batch submit: nothing reaches the host until you submit the review
- Approve the MR as part of the submission
- Read the MR's existing discussion threads, filter to unresolved, and jump from a thread to the
  line it anchors to
- Reply to a thread, resolve it, and edit or delete your own notes

**Hosts**

- GitLab (read and write) — self-hosted or gitlab.com
- The review model itself is host-neutral and the provider is pluggable, but GitLab is the only
  implementation today

## Requirements

- Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/)
- `git`
- A GitLab account and an API token — or the [`glab`](https://gitlab.com/gitlab-org/cli) CLI already
  authenticated, which review-mate will read credentials from
- Optional: [Claude Code](https://claude.com/claude-code), for the agent plane

## Setup

```bash
git clone git@github.com:Joacchim/review-mate-prototype.git
cd review-mate-prototype
uv sync
```

### Credentials

review-mate resolves GitLab credentials from the environment first, then falls back to `glab`'s own
authentication. If you already use `glab`, there is nothing to configure:

```bash
glab auth login       # if not already done
uv run review-mate
```

Otherwise, set them explicitly:

```bash
export REVIEW_MATE_GITLAB_TOKEN=glpat-…          # API token
export REVIEW_MATE_GITLAB_USER=your.username     # used to build your review queue
export REVIEW_MATE_GITLAB_URL=https://gitlab.example.com/api/v4   # self-hosted only
uv run review-mate
```

With no credentials at all the server still starts, but no host is configured, so there is no queue
and no MR to load.

### Run it

```bash
uv run review-mate                          # http://127.0.0.1:8765
uv run review-mate --host 0.0.0.0 --port 9000
```

Open <http://127.0.0.1:8765>. Ctrl-C exits in about two seconds (long-polls are force-drained).

Clones go to an isolated workspace under `~/.review-mate/` — a bare mirror per repository plus
per-MR worktrees. **Your own working clones are never touched.** Cloning uses whichever protocol
`glab` is configured for (`git_protocol`, ssh or https), so it inherits credentials you already
have; override with `REVIEW_MATE_GIT_PROTOCOL`.

### Attaching Claude (optional)

The agent plane needs a Claude Code session attached to the running server.

- The MCP endpoint is registered by the checked-in `.mcp.json` — approve it once when Claude Code
  prompts. No `claude mcp add` needed.
- A `SessionStart` hook (`.claude/hooks/ensure-review-mate.sh`) starts the server if it is not
  already up, so the MCP tools bind cleanly.
- Run `/review-mate` in a Claude Code session **of its own** — not the one you use for other work.
  It watches every open review session, and per session dispatches a bounded `review-worker`
  sub-agent that turns your escalated highlights into cards.

**The server must be running before the Claude Code session starts.** MCP tools bind at session
start, so starting the server midway will not make them appear — relaunch the session instead.

## Using it

1. **Pick something to review.** The landing page shows your open reviews on top and your GitLab
   review queue below. Click an entry to open it, or **Track** it to start its session and keep
   triaging. You can also paste an MR URL or `group/project!iid` into the toolbar box.
2. **Read the diff.** Toggle the file tree (◧), the context panel (◨), unified/side-by-side (⇄), and
   per-commit review (⑃) from the header. Click the bands between hunks to unfold context.
3. **Highlight what you want to know about.** Drag across the lines. The highlight appears in the
   right-hand rail with the cheap context tier already filled in.
4. **Escalate if that is not enough.** Open the highlight and use **✦ Ask Claude for context**,
   optionally with a specific question. The answer arrives as a card on that highlight. The header
   light tells you whether an agent is actually listening.
5. **Write your review.** Draft a comment on a highlight, or an MR-level one. Drafts stay local.
   When you are done, the review bar submits them all at once, optionally approving the MR.
6. **Come back later.** **Mark reviewed** sets a baseline; on your next visit, *Since your last
   review* shows only what the author changed since — as a normal diff, not a diff of diffs.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `REVIEW_MATE_GITLAB_TOKEN` | GitLab API token (`GITLAB_TOKEN` also accepted) | from `glab` |
| `REVIEW_MATE_GITLAB_USER` | Your username, used to build the review queue (`GITLAB_USER` also accepted) | from `glab` |
| `REVIEW_MATE_GITLAB_URL` | API base URL, e.g. `https://gitlab.example.com/api/v4` | `glab`'s host, else `https://gitlab.com/api/v4` |
| `REVIEW_MATE_GIT_PROTOCOL` | `ssh` or `https`, for cloning | `glab`'s `git_protocol`, else `https` |
| `REVIEW_MATE_HOME` | Where sessions, mirrors and the review KB live | `~/.review-mate` |

## Development

```bash
uv run pytest          # unit + functional tests
```

Layout:

| Path | What lives there |
|---|---|
| `review_mate/server/` | ASGI app, HTTP routes, websocket stream |
| `review_mate/session/` | Event-sourced session model (commands → events → state) |
| `review_mate/host/` | Host providers — GitLab read/write, credential resolution |
| `review_mate/workspace/` | The isolated clone workspace (mirrors, worktrees, diffs) |
| `review_mate/mcp/` | The agent seam, mounted at `/mcp` |
| `review_mate/web/` | The browser UI (vanilla JS, no build step) |
| `.claude/` | The Claude Code skill, worker agent, and startup hook |

The UI is served uncached, so a reload picks up `app.js` / `index.html` edits immediately. Python is
frozen at launch — **restart the server after backend changes**. Sessions are restored on startup, so
nothing is lost.

## Troubleshooting

- **"Failed to connect" from the MCP client** — the endpoint is `http://127.0.0.1:8765/mcp/`, with
  the trailing slash. Without it, a POST returns 405.
- **The MCP tools are missing in Claude Code** — the server was not up when the session started.
  Start it, then relaunch the session.
- **The UI calls an endpoint that 404s** — the browser reloaded but the server did not. Restart it.
- **`glab auth status` reports an invalid token but everything else works** — some `glab` versions
  report a false negative for OAuth logins. Trust `glab api user` instead. Note the server resolves
  the token at launch, so re-authenticate *then* restart it.

## Feedback

This is a prototype and it is meant to be pushed on. Issues and merge/pull requests are welcome —
particularly on the interactions that feel clunky, since that is exactly what has not been refined
yet.
