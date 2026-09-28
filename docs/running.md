# Running it

review-mate is meant to sit there: you open the UI when you have something to read, and an agent
connects when there is something to answer. Neither wants to start a server first — an agent in
particular registers the MCP endpoint when *it* starts, so the server has to be up before it is.

## Credentials

review-mate resolves GitLab credentials from the environment first, then falls back to `glab`'s own
authentication. If you already use `glab`, there is nothing to configure:

```bash
glab auth login       # if not already done
review-mate
```

Otherwise, set them explicitly:

```bash
export REVIEW_MATE_GITLAB_TOKEN=glpat-…          # API token
export REVIEW_MATE_GITLAB_USER=your.username     # used to build your review queue
export REVIEW_MATE_GITLAB_URL=https://gitlab.example.com/api/v4   # self-hosted only
review-mate
```

A token set here is read once at launch, so refreshing it means restarting the server. Left to
`glab`, it is re-read from disk when the host refuses the one in hand — which is why the unit below
is written to keep tokens out of it.

With no credentials at all the server still starts. There is then no queue and no merge request to
load — but reviewing a branch on this machine needs git and nothing else, so that still works.

## Run it by hand

```bash
review-mate                          # http://127.0.0.1:8765
review-mate --host 0.0.0.0 --port 9000
```

Open <http://127.0.0.1:8765>. Ctrl-C exits in about two seconds (long-polls are force-drained).

Clones go to an isolated workspace under `~/.review-mate/` — a bare mirror per repository plus
per-MR worktrees. **Your own working clones are never touched.** Cloning uses whichever protocol
`glab` is configured for (`git_protocol`, ssh or https), so it inherits credentials you already
have; override with `REVIEW_MATE_GIT_PROTOCOL`.

## As a systemd user unit

review-mate is meant to sit there: a reviewer opens the UI when they have something to read, and an
agent connects when there is something to answer. Neither wants to start a server first — an agent
in particular registers the MCP endpoint when *it* starts, so the server has to be up before it is.

A **user** unit, not a system one. It reads your git credentials, writes under your `$HOME`, and
exists to be talked to by clients running as you; a system service would have to be handed each of
those back one awkward piece at a time.

**1. Install it somewhere stable.** Include the `tui` extra even if you only want the server — the
terminal client is installed either way, and without the extra it fails on its first import.

```bash
uv tool install 'review-mate[tui] @ git+https://github.com/Joacchim/review-mate-prototype'
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

**Upgrading.**

```bash
uv tool install --force 'review-mate[tui] @ git+https://github.com/Joacchim/review-mate-prototype'
systemctl --user restart review-mate
```

`--force` is what makes it replace the installed version; without it an existing install is audited
and left alone, which looks like success and changes nothing.
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

## The terminal client

The browser is one client of the server, not the server's only face. A terminal client ships
alongside it and talks the same protocol:

```bash
uv sync --extra tui
review-mate-tui                             # against http://127.0.0.1:8765
review-mate-tui --url http://127.0.0.1:9000
```

It shows the review hub — your open reviews with their state, and your GitLab queue — with
`j`/`k` to move, `enter` to start a review from the queue, `c` to close one, `r` to check the
host for updates, `q` to quit. Both clients render state the server folds for them, so neither
holds its own copy of the review model.

## Attaching Claude

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

## Troubleshooting

- **An install did not bring the change you were expecting** — `uv tool install` from a `git+` URL
  resolves the repository's **default branch**. A fix that has not landed there yet is not in what
  you installed, however recently you ran it. Install from a checkout of the branch you want
  (`uv tool install --force '.[tui]'` inside it) to get that one instead.
- **The service exits immediately with `No module named 'mcp.server.fastmcp'`** — an installed
  build resolved a newer major of the `mcp` package than it was written against. Reinstall with
  `--force` from a version that caps it; `systemctl --user status review-mate` and
  `journalctl --user -u review-mate` show the traceback that says which import failed.
- **"Failed to connect" from the MCP client** — the endpoint is `http://127.0.0.1:8765/mcp/`, with
  the trailing slash. Without it, a POST returns 405.
- **The MCP tools are missing in Claude Code** — the server was not up when the session started.
  Start it, then relaunch the session.
- **The UI calls an endpoint that 404s** — the browser reloaded but the server did not. Restart it.
- **`glab auth status` reports an invalid token but everything else works** — some `glab` versions
  report a false negative for OAuth logins. Trust `glab api user` instead. Re-authenticating takes
  effect on the running server: when the host refuses the token in hand, it is re-read from disk and
  the request retried, so `glab auth login` needs no restart. A token pinned in the environment is
  the exception — that one is read once at launch.
