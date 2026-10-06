# Testing the web UI

Every change to `review_mate/web/` ships browser tests. The UI is the half of the system no other
gate covers: the protocol tests prove what the server sends, and nothing else proves the browser
does the right thing with it.

Topics, views, frames and modes are defined in [the glossary](../glossary.md); how the view protocol
fits together is in [the architecture](../architecture.md).

## Stack

- **Playwright** through `pytest-playwright`, so the browser suite is part of `uv run pytest` and
  there is one gate.
- **Chromium and Firefox**, both on every run. pytest-playwright runs chromium alone unless told
  otherwise, so `tests/conftest.py` defaults the list to both; `--browser` still narrows it.
- Browsers install with `playwright install --with-deps chromium firefox`.

```bash
uv sync --extra webtest
uv run playwright install chromium firefox
uv run pytest tests/webui                       # both browsers
uv run pytest tests/webui --browser firefox     # one
uv run pytest tests/webui --headed --slowmo 300 # watch it
```

Run this suite in its own invocation. pytest-playwright's fixtures are synchronous and the other
suites are `asyncio_mode = auto`, so collecting both in one session leaves the async tests
unawaited and reports them as failures that have nothing to do with the code. The gate is
`uv run pytest --ignore=tests/webui` plus `uv run pytest tests/webui`, and CI runs them as separate
jobs for the same reason.

## Architecture

### Stub the host, never the protocol

The fixture server is the production server with its data source replaced. `build_view_routes`,
`ViewBus` and every topic builder are the real ones, so frames on the wire are produced by the same
code as in production and validated by the same models. No test writes protocol JSON by hand.

```mermaid
flowchart LR
    T["a test"] -->|stages state| F["FakeManager<br/>SessionState objects"]
    F --> S["HubTopic · DiffTopics · BlobTopics · AnnotationsTopic<br/><i>production code</i>"]
    S --> B["ViewBus<br/><i>production code</i>"]
    B --> R["build_view_routes<br/><i>production code</i>"]
    R -->|"/api/stream · /api/cmd"| P["the page under test"]
    P -->|"a click, as a command"| C["handle() · reduce()<br/><i>production code</i>"]
    C -->|"the event the command produced"| F
    T -->|asserts on| P
```

Scenarios are built from the real state models — `MRMetadata`, `FileEntry`, `Highlight` — never
from dicts, and commands run the production `handle()` and `reduce()`, so a click lands in staged
state the way it lands in production and the session tail republishes the topics that hold it.
What `FakeManager` fakes is durability: there is no event log, and `seq` is counted in the fake.

A test can also act as a plane the browser is not: `as_agent` submits a command on the fixture
server's own loop, which is how a card arrives on a view that is already open.

### Two entry points

| fixture | server | use for |
|---|---|---|
| `staged_app` | `FakeManager` + real view layer | the default. Any state, set directly: a queue that fails, a file still resolving, `head_aligned` false, a malformed topic name |
| `live_app` | real `create_app` + stub GitLab host | the anchor set. Proves the real server reaches the states `staged_app` stages |

Most tests use `staged_app`, because staging a state beats choreographing a host into producing it.
The `live_app` set stays small and covers one path per surface, end to end.

### What can still drift

Protocol conformance needs no test: `staged_app` *is* `create_app`, so the topic builders, the bus
and the routes are the production objects and a fixture cannot serve a different protocol from the
application. What can drift is the manager beneath them — a method renamed on one side only, which
would surface as a broken page rather than a failing test.

`MANAGER_SURFACE` names what the application reaches for on a manager, and
`test_fixture_conformance.py` asserts both `FakeManager` and `SessionManager` carry all of it.
Renaming a method on either side turns it red.

### Page objects

Selectors live in `tests/webui/pages/`, never in a test. A test reads as behaviour:

```python
async def test_tracking_moves_an_mr_into_open_reviews(hub: HubPage):
    await hub.queue.track("g/p!7")
    await expect(hub.open_reviews.row("g/p!7")).to_be_visible()
    await expect(hub.queue.row("g/p!7")).to_have_count(0)
```

One page object per surface: `HubPage`, `DiffPage`, `AnnotationsPage`, `ShellPage`, `DetailPage`,
`ReviewBarPage`, `ThreadsPage`, `ConsentPage`. Each exposes
intent (`track`, `unfold`, `highlight_lines`, `submit`), not clicks.

Use Playwright's web-first assertions (`expect(...).to_*`) rather than reading values and asserting
on them. They retry, which is what a websocket-driven UI needs. A test that sleeps is a bug.

### Layout

```
tests/webui/
  conftest.py          staged_app, live_app, page fixtures, failure artifacts
  fixtures/
    manager.py         FakeManager, and builders for SessionState scenarios
    scenarios.py       named states: EMPTY_HUB, QUEUE_FAILED, TWO_FILE_REVIEW, REBASED_MR …
  pages/               one page object per surface
  test_hub.py
  test_diff.py
  test_highlights.py
  test_review.py
  test_reviewed_files.py
  test_protocol_edges.py
```

Scenarios are named and shared. A test that needs a one-off state extends `scenarios.py` rather
than building state inline, so the next test can reuse it.

### On failure

`pytest_exception_interact` saves, per failing test: a screenshot, the page's console log, and the
frames the client received. The frame log is the one that matters — it says whether the UI
mis-rendered a correct view or rendered a wrong one faithfully.

## What to test here

Browser tests cover **what only a browser can**: that a view renders, that an interaction sends the
right command, and that a pushed update repaints. Review logic is the protocol suite's job.

| surface | file | covered here |
|---|---|---|
| Hub | `test_hub.py` | open reviews and their state chips, the queue and its filter, track, close, unsubmitted drafts, check-for-updates, a failing queue read, a review as a real link |
| Diff | `test_diff.py` | file tree and selection, side and line numbering, what is selectable, syntax colour, unfold from the blob topic and folding it back — a step from either end or the whole gap, leaving nothing held — side-by-side, markdown toggle, since-last and per-commit modes — including the lines a later commit rewrites, named and clickable, and only where the commit itself wrote them — a conflicted replay warning, the read-only repo browser |
| Highlights | `test_highlights.py` | drag-selecting a range, the number the session gave a row, a stale highlight, marks in the diff, the host-context, escalation, a card arriving, dismissing an insight, a selection surviving a frame mid-drag |
| Channels | `test_channels.py` | the two channels as tabs, that neither can leave by the other, the review as a subject like any other, one chat at a time, and doubting a claim — Claude's or your own |
| Agent state | `test_agent_state.py` | what the server says is outstanding, and what it says once answered |
| Review | `test_review.py` | drafting per highlight and at MR level, editing one, the counts, batch submit and what landed, approve, the discussion list and its filter, jump to line, reply, resolve, and the review-pass control in all three of its states |
| Reviewed files | `test_reviewed_files.py` | marking the open file read and unmarking it, the tree's tick and the progress count, a mark that arrived with the session, and one the author has changed under — stale rather than cleared, and settled by reading it again |
| Consent | `test_consent.py` | what a cross-repo ask shows, allowing, refusing, an already-decided ask, and each repository answered on its own |
| Full view | `test_full_view.py` | the panel taking the window, reading width, the toggle both ways, and what survives the diff view mode |
| Annotation zones | `test_annotation_zones.py` | the pin outside the scroller, its cap, and the index still reachable past a run of insights |
| Addressed | `test_addressed.py` | a subject the agent changed the code over reading as addressed rather than stale, and the drifted case still warning |
| Insight labels | `test_insight_labels.py` | worst-first ordering, unclassified sorting last rather than lowest, the free line, narrowing to one theme, and the reviewer overriding a label without losing the finding |
| Readability | `test_readable.py` | that an active toggle or filter is still legible — its text not the colour of its own background — in both themes |
| Lookup | `test_lookup.py` | host search hitting, missing and failing, that Claude is offered in all three, and that the description sent to Claude is separate from the term sent to the host, and that a hub frame arriving does not take the results away |
| Protocol edges | `test_protocol_edges.py` | a topic republished under an open view repaints it and nothing else |

The edge states — `error`, `unknown-session`, `malformed-name` — and a dropped socket and its
reconnect have **no browser test**. They are protocol-suite facts today; what a browser would add is
that the page renders each without blanking, which nothing yet asserts.

Not tested here, because another gate already proves it:

- what a topic contains, and every derivation in it → the protocol tests
- diff parsing, token spans, line numbering → `tests/unit/test_view_diffdoc.py`
- row HTML from hunks → `tests/web/diffrows.test.js` under node
- anything the terminal client also does → its own tests

The rule: if an assertion would hold with no browser, it belongs in a cheaper suite.

## CI

`.github/workflows/ci.yml`. The `tests` job runs everything but `tests/webui`; the `web-ui` job is a
matrix over chromium and firefox, `fail-fast: false` so one browser's failure does not hide the
other's. Failure artifacts upload on red.

Browser jobs are the slowest thing in the repo — if the matrix stops being tolerable, shard before
dropping a browser. A Firefox-only regression found a day later costs more than the minutes saved.
