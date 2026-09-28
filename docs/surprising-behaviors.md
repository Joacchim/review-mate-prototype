# Surprising behaviours

Behaviour that is correct by design and still catches people out. Each entry says what happens, why
it is that way, and where the reasoning actually lives — this is an index, not a second description,
so nothing here is the owner of a fact explained elsewhere.

If you find yourself explaining one of these twice, it belongs here.

## Any later word closes a double-check

Asking the agent to verify something records an ask, and **any** message it posts about that subject
afterwards closes it — whether or not it verified anything. A reviewer who asks "double-check this"
and then asks "what does this function do?" in the same chat will see the check counted as
answered by the answer to the second question.

The alternative was a verb for the agent to close a check with, which is a second way of saying what
a message already says, and which an agent could forget to call — leaving the reviewer waiting on
work that was done. Waiting on nothing is the worse failure, so this is the side the ambiguity falls
on. See `_answered` in `review_mate/view/asks.py`.

## A chat outlives the discussion it was about

When a host re-sync drops a thread, what the reviewer wrote about it privately stays. The
chat is then reachable on the wire and from no annotation, which costs an orphan.

The host reconciling is not the reviewer discarding — a discussion can leave because someone
resolved and deleted it, or because a system note was filtered — and losing the reviewer's own notes
to that is the worse mistake. Explained in `docs/architecture.md`, "Chats".

## A request survives the code it was about

A highlight, a review pass and a double-check each record the sha they were made at, and none of
them is cleared when the branch moves. A highlight and a pass read as *stale* instead, and a pass
additionally re-opens the control that raised it — so a reviewer sees an old ask and a live button
at once.

That duplication is the point: an ask that vanished would take its waiting cue with it, leaving a
reviewer watching something that stopped, with nothing arriving and nothing said. A late answer
still has somewhere to land. `Highlight.created_sha` is the oldest instance of the rule; `_pass` in
`review_mate/view/annotations.py` is where `stale` and `available` are deliberately kept as two facts.

## Asking Claude to find a merge request leaves no trace

A lookup — the "ask Claude to find it" channel on the landing page — is held in memory and nowhere
else. Claude's answer and the candidates it offered are gone on a server restart, and there is no
history of what was ever asked. Nothing records that it happened.

That is what the channel is for. Discovery runs before any session exists and is a question the
reviewer asks once: coming back to the hub means looking for something else, not resuming the last
search. Putting it in the durable log would make every idle query a permanent record of a thing
nobody wanted to keep, and the session log is for a review, which a lookup is not yet. The cost is
that a lookup cannot be audited or replayed. `review_mate/lookup/broker.py` states the same rule.

## Folding a fix makes an earlier "addressed at abc123" name a commit that is gone

While reviewing a branch that has not left the machine, a fix for something that branch introduced
belongs in the commit that introduced it, so the agent folds it in — which rewrites every sha from
that commit onward. Records made earlier in the same review still carry the shas they were made
with, and those commits no longer exist.

Nothing resolves those shas, so nothing breaks: the mark that matters is that a record *exists*,
which is what distinguishes a subject the agent answered from one whose lines merely drifted. The
sha is there to say what the code became at the time, and after a fold the honest answer for the
latest change is the new head, which is what gets recorded. The alternative was not folding, which
would leave the reviewer a branch shaped like the chat instead of like the work.

## Submitting advances the watermark even when it posts nothing

The reviewed-up-to mark moves to the current head on **any** submit — no drafts prepared, no
approval given, a submit where every post failed. The mark says where the reviewer has read to, and
they read to that head whatever came of it; tying it to a successful post would mean a reviewer who
found nothing worth saying stayed permanently behind. `ReviewSubmitter.submit` in
`review_mate/writeback/submit.py`.
