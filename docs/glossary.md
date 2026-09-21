# Glossary

Terms used across the code and the docs. What each thing *is*; how they fit together is
[the architecture](architecture.md).

## The two planes

**Host plane** — everything that talks to the forge: the diff, the review queue, comments, threads,
approvals. The server does all of it directly, and the review workflow is complete without an agent.

**Agent plane** — context cards, insights, chat, cross-repo lookups. Additive. The agent enriches a
review and never carries a host action the server can perform itself.

## The session

**Session** — one review of one merge request. Event-sourced, in an append-only log under
`~/.review-mate/`, so it survives a restart.

**Command** — an intent submitted to a session (`add_highlight`, `post_message`). Validated, then
recorded as events.

**Event** — a fact appended to the session's log. The log is the source of truth.

**Session state** — the document set folded from the events: the MR, files, highlights, cards,
access requests, threads, messages, drafts.

**Actor** — the single writer for one session. Serialises commands, appends events, publishes them.

**Origin** — who submitted a command: `BROWSER`, `AGENT`, `SYSTEM`. An authority matrix rejects what
an origin may not do.

## The view protocol

**View** — a document the server folds for clients to render. Holds no logic and no history: a
client displays what a view carries and derives nothing from it.

**Scope** — a named view, and the unit of both change and transfer. A client subscribes to scopes by
name and is sent each one whole; when it changes, the whole thing is sent again. There are no
partial updates, which is why a client needs no merge logic.

**Frame** — one message on the stream. Either a scope update (`scope`, `seq`, `view`) or an error.

**Scope family** — a parameterised scope. The kind before the first colon selects the builder and
the rest is its argument, so `diff:<sid>:<mode>:<path>` needs no registration per file. A scope with
no argument — `hub` — is a singleton.

**seq** — a per-scope counter of changes. It orders replacements and exposes a gap. Not a resume
token: a reconnecting client re-subscribes and is sent each scope's current view.

**Mode** — which version of a change is being read: `full`, `since`, or `commit@<sha>`. Part of a
scope's name rather than server state, because it is a property of the reader.

**hub** — the scope shown before a review is open: open reviews with their verdicts, and the host
review queue.

**diff** — the scope family for reading a change. Without a path it is the file list and the MR;
with one, that file's hunks, lines and token spans.

**blob** — the scope family carrying a whole file at a resolved sha, which is what a client splices
from when a reader unfolds the context between hunks.

**rail** — the scope family carrying a session's highlights with their cards and their cheap
context tier, plus the MR-level insights. One scope per session rather than per file: the numbering
is session-wide, and a card arriving would otherwise republish a whole tokenized file.

**#N** — a highlight's number, fixed when it is created and never reassigned. It is a reference a
reviewer uses in conversation and an agent cites in a card, so removing a highlight leaves a gap
rather than renumbering the rest — the way issue numbers behave.

**Token kind** — a semantic label on a span of source text (`keyword`, `string`, `comment`). The
server lexes and sends kinds; each client maps them to its own palette. An unknown kind renders
plain.

## Reviewing

**Highlight** — a line range a reviewer marked to ask about.

**Cheap context tier** — host-computed facts answered immediately for a highlight — last touch,
linked issues — with no agent involved.

**Card** — an answer anchored to a highlight, or an MR-level insight the agent volunteered.

**Draft** — a review comment written locally. Nothing reaches the host until the review is submitted.

**Thread** — a discussion on the MR, owned by the host and mirrored into session state.

**Watermark** — the MR head a reviewer last marked as reviewed. What `since` measures from.

**head-aligned** — whether a view's line numbers are in the session's head coordinates. A `since`
view computed against a newer head is not, and cannot anchor a comment.

## The host and the workspace

**Host** — the forge. GitLab is the only implementation.

**Provider** — the host-facing implementation behind the seam.

**Seam** — a Protocol the core is written against, so the core carries no host, transport or UI
knowledge.

**Workspace** — the isolated clone area under `~/.review-mate/`: a bare **mirror** per repository,
and **checkouts** (worktrees) materialised from it. A reviewer's own clones are never touched.

**Client** — anything that renders scopes and sends commands. The browser and the terminal client
are two of them; the agent seam is a third, with a narrower command set.
