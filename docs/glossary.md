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

**Session writer** — the single writer for one session, and the only thing that appends to its
log. Serialises commands, appends events, publishes them; because exactly one task mutates the
state, writes need no locks.

**Origin** — who submitted a command: `BROWSER`, `AGENT`, `SYSTEM`. An authority matrix rejects what
an origin may not do.

## The view protocol

**View** — a document the server folds for clients to render. Holds no logic and no history: a
client displays what a view carries and derives nothing from it.

**Topic** — a named view, and the unit of both change and transfer. A client subscribes to topics by
name and is sent each one whole; when it changes, the whole thing is sent again. There are no
partial updates, which is why a client needs no merge logic.

**Frame** — one message on the stream. Either a topic update (`topic`, `seq`, `view`) or an error.

**Topic family** — a parameterised topic. The kind before the first colon selects the builder and
the rest is its argument, so `diff:<sid>:<mode>:<path>` needs no registration per file. A topic with
no argument — `hub` — is a singleton.

**Diff view mode** — which version of a change is being read: `full`, `since`, or
`commit@<sha>`. Part of a topic's name rather than server state, because it is a property of the
reader. Always said in full: the browser has a split mode and a light/dark mode too, and they are
different axes.

**hub** — the topic shown before a review is open: open reviews with their verdicts, and the host
review queue.

**diff** — the topic family for reading a change. Without a path it is the file list and the MR;
with one, that file's hunks, lines and token spans.

**blob** — the topic family carrying a whole file at a resolved sha, which is what a client splices
from when a reader unfolds the context between hunks.

**annotations** — the topic family carrying a session's highlights with their cards and their host
context, plus the MR-level insights. One topic per session rather than per file: the numbering
is session-wide, and a card arriving would otherwise republish a whole tokenized file.

**chat** — an exchange between the reviewer and the agent about one **subject**, or about the
review as a whole. Private to the reviewer. Distinct from a **discussion**, which is the host's own
and which other participants see: they differ in who can read them and in how a message reaches
them. Both are conversations in the ordinary sense, which is why neither is called one.

**subject** — what a chat is about: a highlight, an MR-level insight, or a host thread,
addressed by kind and id. The set is exactly what a client can open a detail panel on.

**presence** — whether an agent is consuming the activity stream at all (`attached`, `parked`,
`last_seen`). A property of the stream, so one fact for the whole fleet, and it decays by clock
rather than by event. It says nothing about whether an answer is being worked on — that is the join
with what is outstanding, which the chat topic publishes as the **agent state**.

**#N** — a highlight's number, fixed when it is created and never reassigned. It is a reference a
reviewer uses in conversation and an agent cites in a card, so removing a highlight leaves a gap
rather than renumbering the rest — the way issue numbers behave.

**Token kind** — a semantic label on a span of source text (`keyword`, `string`, `comment`). The
server lexes and sends kinds; each client maps them to its own palette. An unknown kind renders
plain.

## Reviewing

**Highlight** — a line range a reviewer marked to ask about.

**Host context** — what the host can already tell you about the lines you marked: who last
touched them, and the issues the change closes. It arrives immediately and needs no agent.

**Card** — an answer anchored to a highlight, or an MR-level insight the agent volunteered.

**Draft** — a review comment written locally. Nothing reaches the host until the review is submitted.

**Discussion** — a conversation on the MR that every participant sees, owned by the host and
mirrored into session state. `thread` is its name on the wire.

**Watermark** — the MR head a reviewer last marked as reviewed. What `since` measures from.

**head-aligned** — whether a view's line numbers are in the session's head coordinates. A `since`
view computed against a newer head is not, and cannot anchor a comment.

## The host and the workspace

**Host** — the forge. GitLab is the only implementation.

**Provider** — the host-facing implementation behind the contract.

**Contract** — a Protocol the core is written against, so the core carries no host, transport or UI
knowledge.

**Workspace** — the isolated clone area under `~/.review-mate/`: a bare **mirror** per repository,
and **checkouts** (worktrees) materialised from it. A reviewer's own clones are never touched.

**Client** — anything that renders topics and sends commands. The browser and the terminal client
are two of them; the agent contract is a third, with a narrower command set.
