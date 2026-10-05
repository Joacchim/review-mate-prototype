# Working on review-mate

## Getting set up

```bash
git clone git@github.com:Joacchim/review-mate-prototype.git
cd review-mate-prototype
uv sync
uv run review-mate            # http://127.0.0.1:8765
```

`uv run` is the development path — it runs from the checkout, so an edit is one restart away.

To try the packaged form — what a user gets, executables and all — install from the checkout rather
than from the URL in the README, which resolves the repository's default branch:

```bash
uv tool install --reinstall --force '.[tui]'
```

**`--reinstall` is not optional here, and `--force` does not stand in for it.** `--force` replaces
the installed tool; it says nothing about where the build came from. uv caches the wheel it builds
from a directory, and the project version alone does not invalidate that cache — so a plain install
from a checkout happily redeploys a build from days ago, writing fresh files with stale content.
`--reinstall` implies `--refresh`, which is what rebuilds. Verified by installing into an empty
tool directory: without it, a checkout carrying a day of commits deployed none of them.

That is also how to test a change to packaging, or a fix that has not landed on the default branch
yet. For running it as a service, see [running it](docs/running.md).

The UI is served uncached, so a reload picks up `app.js` / `index.html` edits immediately. Python is
frozen at launch — **restart the server after backend changes**. Sessions are restored on startup,
so nothing is lost.

## Tests

```bash
uv run pytest --ignore=tests/webui            # unit, functional, integration, boundary
uv run pytest tests/webui --browser chromium  # the browser suite
uv run pytest tests/webui --browser firefox
```

The browser suite needs the `webtest` extra (`uv sync --extra webtest`) and its browsers
(`uv run playwright install chromium firefox`). It runs the production application over staged
sessions, so a test failure is the product's, not a mock's —
[how the web UI is tested](docs/testing/web-ui.md) explains the arrangement and what belongs there.

Screenshots in the documentation are generated, not taken:

```bash
uv run --extra webtest python tools/screenshots.py
```

That drives the real application too. Regenerate them when a screen changes rather than describing
the difference in prose.

## Dependencies

`uv.lock` pins what a checkout gets. It does **not** pin what an install gets: `uv tool install`
resolves afresh, so a dependency with no upper bound picks up whatever major is current that day.
A green checkout is therefore not evidence that `uv tool install` works — nor is a successful
install evidence that it installed *this* code. Check the artifact, not the command's exit status:
`grep` the installed tree under `~/.local/share/uv/tools/` for a line the change introduced. That
is the only check that has been shown to tell the truth; the dist-info records a source timestamp
that stays old across a correct install, so it reports stale on a build that is fine.

So direct dependencies whose API we reach into are capped at the major they were built against, and
raising a cap is a migration with its own commit rather than a bump. A boundary test holds the line
for `mcp`, where this has already cost an afternoon.

## Layout

| Path | What lives there |
|---|---|
| `review_mate/server/` | ASGI app, HTTP routes, websocket stream |
| `review_mate/session/` | Event-sourced session model (commands → events → state) |
| `review_mate/view/` | Server-folded client state — topics, the view bus, the hub topic |
| `review_mate/host/` | Host providers — GitLab read/write, a local branch, credential resolution |
| `review_mate/workspace/` | The isolated clone workspace (mirrors, worktrees, diffs) |
| `review_mate/writeback/` | Posting a review, and the thread verbs |
| `review_mate/mcp/` | The agent contract, mounted at `/mcp` |
| `review_mate/web/` | The browser UI (vanilla JS, no build step) |
| `review_mate/tui/` | The terminal client — a renderer over the view protocol |
| `docs/` | Architecture, features, running it, glossary, surprising behaviours, testing method |
| `tools/` | Documentation machinery — the screenshot generator |
| `packaging/` | A systemd user unit, and a launchd agent for macOS |
| `.claude/` | Claude Code skills (watching a fleet, reviewing your own branch), the worker agent, the startup hook |

## Reading the design first

- [Architecture](docs/architecture.md) — the view protocol, the two planes, what each topic carries.
  Read this before adding a topic or a command; both have one place they belong.
- [Glossary](docs/glossary.md) — the words, used precisely and consistently.
- [Surprising behaviours](docs/surprising-behaviors.md) — behaviour that is correct by design and still catches
  people out. If you find yourself explaining something twice, it belongs there.
- [How the web UI is tested](docs/testing/web-ui.md) — and the map of what that suite covers.

## Feedback

This is a prototype and it is meant to be pushed on. Issues and merge/pull requests are welcome —
particularly on the interactions that feel clunky, since that is exactly what has not been refined
yet.
