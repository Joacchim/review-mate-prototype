---
name: self-review
description: >
  Put your own branch under review before anyone else sees it. Opens a review-mate session on a
  local branch, hands the reviewer a link, then answers their comments directly — discussing where
  a question is asked and changing the code where a change is wanted, one comment at a time. Use
  when you have just written something and want it read before it becomes a merge request, or when
  asked to "let me review this first" / "open a review on this branch".
---

# self-review — your own branch, read by the person about to put their name on it

You wrote this code. The reviewer is about to sign off on it and has not seen it yet. **You are the
author under review**, not a context service: you hold the whole reason the code looks like this,
which is exactly what a comment on it needs answering with.

That makes this the opposite role to the `review-mate` skill. That one is the fleet coordinator —
it watches every session, holds no per-MR context, and dispatches `review-worker` sub-agents.
**Do not dispatch a worker here.** A worker would be the one party in the room without the context
that makes the answer worth anything.

## Prerequisites

The `review-mate` MCP, and a server the reviewer can open (`review-mate`, default
`http://127.0.0.1:8765`; `REVIEW_MATE_URL` overrides the link the reviewer is given). Nothing else — a branch on disk
needs git, so this works with no forge configured at all.

## 1. Open it

```
git branch --show-current
```

Then `open_local_review(path=<repo root>, branch=<that>, base="")` — an empty base means whatever
the repository merges into, which is what you mean unless the reviewer said otherwise.

**Give them the `url`.** One line, plainly, and not the session id: a link is somewhere to look, an
id is homework. Say that you will watch it and answer as they comment, so they know not to come back
to the terminal.

## 2. Watch it

Background long-poll, exactly as the coordinator does — `run_in_background: true`:

```
curl -s -m 55 "http://127.0.0.1:8765/api/activity?since=<seq>"
```

On re-invocation: **ignore events for any other session**, set `since` to the event's `seq`, and
relaunch the poll promptly. An empty body is a timeout tick — relaunch with the same `since`. The
kinds (`context_requested`, `message_posted`, `insights_requested`, `check_requested`,
`access_settled`) all mean the same thing to you: *this session has work*. The name is a hint, never
the work list.

## 3. Take one at a time

`get_session(session_id)` → `chat.asks` is what they are waiting on you for, already worked out.
**Take the oldest one, finish it completely, then look again.** They review by giving you one thing
at a time and watching what happens to it; draining four at once turns the review into a diff they
have to read all over again.

## 4. Discuss, or change

- A **question** — answer it in the conversation. `post_message` on that subject. Not everything is
  a request for a change, and rewriting code to answer "why is this here?" is its own kind of wrong.
- A **request to change something** — change it. This is the part that makes this loop worth having:
  they should not have to write the fix out in prose for you to apply.
- **Unsure which it is?** Ask them, in the conversation, before touching anything.

## 5. Where the change goes — think before you commit

This is an unmerged branch, so **a fix for something this branch introduced belongs in the commit
that introduced it**, not stacked on top. The branch keeps the shape it will be read in.

Find the commit that introduced the lines the comment is about:

```
git log --oneline <base>..HEAD -- <path>
git log -S '<a distinctive line>' --oneline <base>..HEAD
```

Then fold it in:

```
git commit --fixup=<that sha>
GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash <base>
```

The `-i` is required — `--autosquash` is ignored by a non-interactive rebase, which exits 0 and
folds nothing, so a missing `-i` looks like success and leaves a `fixup!` commit on the branch.
Stubbing the sequence editor is what keeps it from blocking on one.

**Folding rewrites every sha from that commit onward.** Read the new head *after* the rebase for
the record below, and know that shas you recorded earlier in this review may now name commits that
no longer exist — see `docs/surprising-behaviors.md`.

**It is its own commit, ordered ahead, when the story is not that commit's** — a pre-existing bug
the review merely revealed, or a prerequisite you discovered while fixing something else. The test
is *"is this edit needed for that commit to be right?"* If no, it has its own story.

**When it is genuinely unclear, ask them in the conversation.** The branch's shape is the
reviewer's to decide as much as yours, and one message costs less than a history they did not want.

**If the rebase conflicts, stop fighting it.** `git rebase --abort`, make it a plain commit on top,
and say in the conversation that it could not be folded and why. A branch with an honest extra
commit beats a reviewer watching you wrestle git.

## 6. Record it, then let the review see it

Two calls, in this order, every time you change code:

1. `record_addressed(session_id, subject_kind, subject_id, sha, summary)` — `sha` is the **branch
   head after the change has landed**, read *after* any rebase. It is what the reviewer's diff now
   shows, and it is what stops their rail reading your fix as their highlight going stale.
2. `session.resync` — `POST /api/cmd {"cmd": "session.resync", "args": {"session": "<id>"}}`.
   Committing changes the branch; nothing tells the review until it is asked to look again.

Then **say something in the conversation as well**. The record is what the rail draws; it is not a
reply, and a fix that arrives with nothing said reads as being ignored.

## 7. Stop

The reviewer closes the session when they are done — `get_session` then reports `state: "ended"`.
Stop polling and say what the branch now looks like.

**Do not open the merge request.** That is theirs to authorise, separately, in their own words —
and it is not something this skill does even when asked nicely mid-review.

## What not to do

- **Do not dispatch a `review-worker`.** See the top: it would not have the context.
- **Do not answer four comments at once** because they arrived together.
- **Do not post to a forge.** There is no merge request here; the write verbs refuse, and trying is
  a sign you are in the wrong loop.
- **Do not treat silence as approval.** A reviewer reading is not a reviewer agreeing; wait for them
  to close the session.
