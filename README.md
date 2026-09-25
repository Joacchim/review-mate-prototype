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

### Keep it running

review-mate is meant to sit there: a reviewer opens the UI when they have something to read, and an
agent connects when there is something to answer. Neither wants to start a server first — an agent
in particular registers the MCP endpoint when *it* starts, so the server has to be up before it is.

A **user** unit, not a system one. It reads your git credentials, writes under your `$HOME`, and
exists to be talked to by clients running as you; a system service would have to be handed each of
those back one awkward piece at a time.

**1. Install it somewhere stable.** Include the `tui` extra even if you only want the server — the
terminal client is installed either way, and without the extra it fails on its first import.

```bash
uv tool install '.[tui]'                 # review-mate and review-mate-tui, on ~/.local/bin
```

**2. Authenticate once.** `glab auth login`, and leave it at that — see below for why not to put a
token in the unit.

**3. Install and start the unit.**

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/review-mate.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now review-mate
```

Adjust `ExecStart` if you installed it elsewhere. `loginctl enable-linger $USER` keeps it up when
you are not logged in.

**4. Check it took.**

```bash
systemctl --user status review-mate
curl -s http://127.0.0.1:8765/api/sessions      # `[]` on a fresh install
journalctl --user -u review-mate -n 50          # when it did not
```

Then open <http://127.0.0.1:8765>.

**5. Let an agent reach it, from any repository.** The `.mcp.json` in this repo registers the
endpoint for this project only, which is not much use when the thing you want reviewed is somewhere
else. Register it once for yourself instead:

```bash
claude mcp add --scope user --transport http review-mate http://127.0.0.1:8765/mcp/
```

**Upgrading.** `git pull && uv tool install --force '.[tui]' && systemctl --user restart review-mate`.
Your reviews survive it — they are event-sourced under `~/.review-mate` and restored at startup.

Three things worth knowing:

**Leave your token out of the unit file.** A token in `Environment=` is read once at exec and
frozen for the life of the process, so refreshing it means restarting the service — and it shows up
in `systemctl show` and the journal. Left to `glab`, it is re-read from disk whenever the host
refuses the one in hand, so `glab auth login` takes effect on the running server with no restart.
That is the whole reason the credential resolution falls back to `glab` rather than requiring
environment variables.

**A user unit has no ssh agent.** Cloning uses whatever git credentials you already have, which over
ssh means an agent your login session started and the unit does not inherit. Point `SSH_AUTH_SOCK`
at it in the unit (there are commented lines for the two usual places), or use `https` with a
credential helper. Without either, reviews still work over the host API — but nothing is cloned, so
there is no local checkout, and an agent loses grep, LSP and the code graph with it.

**Idling costs nothing.** Measured on an idle server: 66 MB resident and no measurable CPU. Every
background task — the presence ticker, a session's event tail, the consent watch — starts when a
client first watches something and stops when the last one goes. A restart drops only the in-flight
notification stream, which is designed to be re-derived from durable state rather than replayed.

Keep it on loopback. Nothing on `/mcp` or `/api` is authenticated — the trust boundary is your user
account, exactly as it is for the files it reads.

### The terminal client

The browser is one client of the server, not the server's only face. A terminal client ships
alongside it and talks the same protocol:

```bash
uv sync --extra tui
uv run review-mate-tui                      # against http://127.0.0.1:8765
uv run review-mate-tui --url http://127.0.0.1:9000
```

It shows the review hub — your open reviews with their state, and your GitLab queue — with
`j`/`k` to move, `enter` to start a review from the queue, `c` to close one, `r` to check the
host for updates, `q` to quit. Both clients render state the server folds for them, so neither
holds its own copy of the review model.

### Attaching Claude (optional)

The agent plane needs a Claude Code session attached to the running server.

- **In this repository**, the MCP endpoint is registered by the checked-in `.mcp.json` — approve it
  once when Claude Code prompts, and nothing else is needed. **Anywhere else**, register it for
  yourself once (see *Keep it running*, step 5); `.mcp.json` only covers the project it sits in,
  which is no use when the branch you want reviewed is in another repository.
- A `SessionStart` hook (`.claude/hooks/ensure-review-mate.sh`) starts the server if it is not
  already up, so the MCP tools bind cleanly. Redundant once the systemd unit is running, and
  harmless.
- Run `/review-mate` in a Claude Code session **of its own** — not the one you use for other work.
  It watches every open review session, and per session dispatches a bounded `review-worker`
  sub-agent that turns your escalated highlights into cards.

**The server must be running before the Claude Code session starts.** MCP tools bind at session
start, so starting the server midway will not make them appear — relaunch the session instead. This
is the main reason to run it as a unit rather than starting it by hand.

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
| `REVIEW_MATE_BLOB_BUDGET_MB` | How much whole-file text is kept for unfolding, oldest version dropped first | `16` |
| `REVIEW_MATE_URL` | Where a reviewer reads this server, for links an agent hands them | `http://127.0.0.1:8765` |

## Development

```bash
uv run pytest          # unit + functional tests
```

Design documentation lives under `docs/`: [the architecture](docs/architecture.md),
[a glossary](docs/glossary.md), [behaviour that surprises people](docs/surprises.md), and
[how the web UI is tested](docs/testing/web-ui.md).

Layout:

| Path | What lives there |
|---|---|
| `review_mate/server/` | ASGI app, HTTP routes, websocket stream |
| `review_mate/session/` | Event-sourced session model (commands → events → state) |
| `review_mate/view/` | Server-folded client state — scopes, the view bus, the hub scope |
| `review_mate/host/` | Host providers — GitLab read/write, credential resolution |
| `review_mate/workspace/` | The isolated clone workspace (mirrors, worktrees, diffs) |
| `review_mate/mcp/` | The agent seam, mounted at `/mcp` |
| `review_mate/web/` | The browser UI (vanilla JS, no build step) |
| `review_mate/tui/` | The terminal client — a renderer over the view protocol |
| `docs/` | Architecture, glossary, known surprises, testing method |
| `packaging/` | A systemd user unit, for keeping the server up |
| `.claude/` | The Claude Code skills (watching a fleet, reviewing your own branch), worker agent, and startup hook |

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
