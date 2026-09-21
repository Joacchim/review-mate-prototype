# Architecture

What the pieces are and how they fit. [The README](../README.md) says what the tool does;
[the glossary](glossary.md) defines the terms used here.

## Layers

```mermaid
flowchart TD
    subgraph clients["Clients"]
        W["review_mate/web/<br/>browser"]
        T["review_mate/tui/<br/>terminal"]
        A["review_mate/mcp/<br/>agent seam"]
    end
    subgraph transport["Transport · review_mate/server/"]
        V["view_routes.py<br/>/api/stream · /api/cmd"]
        H["routes.py<br/>/api/sessions/…"]
    end
    subgraph view["View · review_mate/view/"]
        BUS["bus.py"]
        SC["hub.py · diffscope.py<br/>diffdoc.py · tokens.py"]
    end
    subgraph core["Core · review_mate/session/"]
        AC["actor · commands · events<br/>reducer · state · eventlog"]
    end
    subgraph edges["Edges"]
        HO["host/<br/>GitLab"]
        WS["workspace/<br/>mirrors · worktrees"]
        KB["kb/<br/>watermarks"]
    end

    W --> V
    T --> V
    W --> H
    A --> AC
    V --> BUS --> SC --> AC
    H --> AC
    SC --> HO
    SC --> WS
    SC --> KB
    AC --> HO
```

| layer | holds | knows about |
|---|---|---|
| `session/` | the event-sourced review model | nothing below it |
| `seams.py` | the Protocols the core is written against | nothing below it |
| `host/`, `workspace/`, `kb/` | the forge, the clone area, the reviewer's watermarks | the core |
| `view/` | folding state into scopes | the core and the edges |
| `server/` | HTTP and websocket transport | everything above |
| `web/`, `tui/`, `mcp/` | clients | the transport only |

The core carries no host, transport or UI knowledge. `tests/boundary/test_boundaries.py` enforces
that by parsing imports: `session/*.py` and `seams.py` may not import `review_mate.view` or
`review_mate.server`, nor anything host- or MCP-shaped.

## The core

A session is an actor: commands in, events appended to a log, state folded from the log. The log is
the source of truth and is fsynced before a command is acked, so an acked change survives a crash.
State is restored by replay at startup.

Every command carries an origin, and an authority matrix decides what each origin may do. That is
how the agent plane stays additive: the agent can add a card, and cannot post a comment to the host.

## The view protocol

Clients render, and derive nothing. The server folds state into named **scopes** and sends each one
whole; a client subscribes by name and displays what arrives.

Whole-scope replacement is the load-bearing choice. It means a client needs no merge logic, and it
makes the slow-consumer policy fall out: an undelivered update is worthless once a newer one exists,
so a subscriber holds at most one pending frame per scope and the newest wins.

### The wire

One websocket, `/api/stream`. Client frames:

```json
{"action": "subscribe",   "scopes": ["hub", "diff:<sid>:full"]}
{"action": "unsubscribe", "scopes": ["diff:<sid>:full"]}
```

Server frames:

```json
{"type": "scope", "scope": "hub", "seq": 12, "view": {…}}
{"type": "error", "scope": "hub", "reason": "RuntimeError: host is down"}
```

Writes go to `POST /api/cmd` as `{"cmd": …, "args": {…}}` — `session.open`, `session.close`,
`hub.refresh`. A rejection answers with a status code and a reason, and the status carries meaning:
400 on `session.open` means the reference did not parse.

### The scopes

| name | carries |
|---|---|
| `hub` | open reviews with their verdicts, and the host review queue |
| `diff:<sid>:<mode>` | the file list and the MR |
| `diff:<sid>:<mode>:<path>` | one file's hunks, lines and token spans |
| `blob:<sid>:<mode>:<path>` | a whole file at the resolved sha, for unfolding |
| `rail:<sid>` | the session's highlights with their cards and cheap context, and MR-level insights |

A file's scope name is the list's name with a path appended, so a client concatenates rather than
assembling a second name. Names are validated: a path may contain a colon, a session id and a mode
may not, and a malformed name reports `malformed-name` instead of being read as a plausible path.

### Rules a scope holds to

- **Building a scope never calls the host.** Host reads are separate methods, driven by a command or
  a one-shot loader, so a rebuild cannot turn into a fan-out of network calls.
- **A slow source reports itself.** A scope that must reach the host or the workspace reports
  `loading` and republishes when the answer lands, rather than blocking the frame.
- **Freshness tiers stay distinct.** Local state rebuilds on every publish. An automatic host read
  carries its own state field. A fan-out across reviews runs only on an explicit refresh, and its
  subjects report whether they have been checked.
- **Derivations live in the scope.** Verdicts, counts and row order are the server's. Two clients
  computing the same thing will eventually disagree.
- **A builder failure is an error frame**, never an exception at the client. The subscription
  survives, and a later publish can succeed.
- **Nothing is built for a scope nobody watches**, though `seq` still advances, so a late subscriber
  learns how current its first view is.
- **Work that only makes sense while someone is looking starts and stops with the watching.** The
  bus reports when a scope gains its first watcher and loses its last, and the tail on a session's
  events — which is what republishes its reading scopes when its state changes — runs exactly
  between those two moments.

### Reading a change

```mermaid
sequenceDiagram
    participant C as Client
    participant B as ViewBus
    participant D as DiffScopes
    participant G as GitLab

    C->>B: subscribe diff:s1:full
    B->>D: build
    D-->>B: file list from session state
    B-->>C: scope seq=0
    Note over C: the reader opens a file
    C->>B: subscribe diff:s1:full:a.py
    B->>D: build
    D-->>B: hunks, lines, token spans
    B-->>C: scope seq=0
    Note over C: the reader switches to since
    C->>B: subscribe diff:s1:since
    B->>D: build
    D-->>B: loading
    B-->>C: scope state=loading
    D->>G: mr_versions
    G-->>D: version bases
    D->>D: workspace resolves the diff
    D->>B: publish
    B-->>C: scope state=ready
```

Switching mode is a subscription, not a command — which is why no mode command exists to fall out of
step with what a client is showing.

## Session documents

There are two write paths, and the line between them is deliberate: **`/api/cmd` addresses the set
of reviews and the host; `/api/sessions/{id}/commands` addresses the contents of one review.**
Opening, closing and refreshing are about which reviews exist; highlights, cards, drafts and
messages are about what is inside one.

Alongside the view protocol, the session document is served directly: `GET /api/sessions/{id}`
returns the folded state, `POST /api/sessions/{id}/commands` submits a session command, and a
per-session websocket streams its events. A client reads what the scopes fold — highlights, their
cards and the cheap tier all arrive on `rail:<sid>` — and reaches for the document only for what no
scope carries: drafts under edit, threads, chat. The agent reaches the same sessions in-process
through the MCP bridge rather than over HTTP.

## Clients

The browser and the terminal client both render scopes and send commands, and neither models review
state. A client owns only presentation: mapping token kinds to a palette, deciding how much unfolded
context to show, choosing a layout.

That is what makes a second client cheap, and it is enforced by having one:
[the web UI testing method](testing/web-ui.md) covers the browser, and the terminal client's own
tests cover the same protocol from the other side.
