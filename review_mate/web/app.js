"use strict";
// review-mate diff browser — a thin surface. Presents session state, captures highlights; never
// reasons. Source of truth is the bridge-server; we re-fetch the snapshot on every WS event.

let SID = null;
let state = null;
let currentFile = null;
const collapsedDirs = new Set();
let splitMode = localStorage.getItem("rm-split") === "1";
const draftBuffers = {};   // highlight_id -> in-progress review-comment text (survives re-render)
let focusedDraft = null;   // highlight_id of the focused draft textarea, to restore after render
let showAll = localStorage.getItem("rm-showall") === "1";
const scopeViews = {};               // scope name -> the view the server folded, whole
let viewSocket = null;
let wantedScopes = [];               // what this page subscribes to, re-sent on reconnect
let refreshing = false;              // a hub.refresh is in flight
let markHubReady = null;
const hubReady = new Promise((resolve) => { markHubReady = resolve; });
const blobWanted = new Set();        // paths whose whole-file content this page is showing
const expandedGaps = {};             // path -> Map of gap-start -> {top, bot, all} lines unfolded
const mdRendered = new Set();        // .md paths currently shown rendered (vs raw diff)
let viewingPath = null;              // a non-diff file currently shown (plain view)
let annotationFilter = "all";              // index filter: all | context | comment | posted
let annotationQuery = "";                  // index text search (file + comment + question)
let annotationSearchFocused = false;       // restore search focus after a WS-driven re-render
let selected = null;                 // {kind:"hl"|"insight"|"mr"|"thread", id} shown in the detail overlay
let detailTab = null;                // "claude" | "host" for the open subject; null picks the default
let detailMax = false;               // the panel given the whole window, for reading a long one
let detailReading = false;           // and held to a measure within it, when the lines get long
const msgDraft = {};                 // chat scope -> in-progress message (survives re-render)
let msgFocused = null;               // scope of the focused composer, to restore after render
const MR_KEY = "__mr__";             // draftBuffers/focus key for the (anchorless) MR-level comment
let approveToggle = false;           // "Approve MR" checkbox on the submit bar
let threadFilter = "unresolved";     // discussions filter: unresolved | all
const threadReplyBuf = {};           // thread_id -> in-progress reply text (survives re-render)
let threadReplyFocused = null;       // thread_id of the focused reply textarea, to restore after render
const askBuf = {};                   // highlight_id -> in-progress "ask Claude" question text
let askFocused = null;               // highlight_id of the focused ask-context input, to restore after render
const suggBuf = {};                  // draft key -> in-progress suggested-change text
const suggOpen = {};                 // draft key -> whether the suggestion editor is open
const noteEdit = {};                 // note_id -> in-progress edit text (null/absent = not editing)
// the review bar is the server's: what is prepared, whether this moved on, who has approved
let commitsMode = false;             // per-commit review: the diff pane shows one commit at a time
let currentCommit = null;            // sha of the commit being reviewed
let sinceLast = false;               // showing the rebase-aware "since last review" interdiff

const $ = (id) => document.getElementById(id);
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// minimal, safe markdown (escape first, then a small subset) — Claude writes markdown
function mdInline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function md(src) {
  const lines = esc(src || "").split("\n");
  let html = "", inList = false, inCode = false, code = "";
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
  for (const ln of lines) {
    if (/^\s*```/.test(ln)) {
      if (!inCode) { inCode = true; code = ""; }
      else { html += `<pre class="md"><code>${code}</code></pre>`; inCode = false; }
      continue;
    }
    if (inCode) { code += (code ? "\n" : "") + ln; continue; }
    const m = ln.match(/^\s*[-*]\s+(.*)/);
    if (m) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${mdInline(m[1])}</li>`; continue; }
    closeList();
    if (ln.trim() === "") continue;
    html += `<div>${mdInline(ln)}</div>`;
  }
  closeList();
  if (inCode) html += `<pre class="md"><code>${code}</code></pre>`;
  return html;
}

// Esc steps out of full view rather than closing the panel: the subject is still open behind it,
// and losing it to a stray keypress costs more than the extra press.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !detailMax) return;
  if (/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
  detailMax = false;
  renderDetail();
});

// --- boot -------------------------------------------------------------------

async function boot() {
  wireToolbar();
  startAgentWatch();   // the header light runs everywhere, queue page included
  const params = new URLSearchParams(location.search);
  SID = params.get("s");
  if (!SID) connectViews(["hub"]);   // the landing page and the ?ref= resolver read the hub scope
  // ?ref=<project!iid> — what a queue entry links to, so it can be middle-clicked into its own tab.
  // Resolving it here (rather than on click) is what makes the entry a real link instead of a button.
  if (!SID && params.get("ref")) return openRef(params.get("ref"));
  if (!SID) return showLanding();
  $("sid").textContent = SID.slice(0, 8);
  connectViews(diffScopes());   // the change is read through the view protocol
  await load();          // paint fast from stored state
  connectWS();           // the session's own event stream, for highlights, cards and threads
  syncFromHost();        // then bring the session up to the live head, so an update the hub flagged
                         // actually surfaces here (banner + "Since last review"), not just on the hub
}

// one-time re-sync when opening a review: pulls the current head + diff + discussions from the host,
// so a branch that advanced since you last looked (the hub's "git update") engages the since-last
// feature in-session instead of the stored, stale head hiding it. Background — the page already painted.
async function syncFromHost() {
  // the same re-sync the ↻ button runs, on the way in. Quiet on failure: the stored view is still
  // worth reading, and the button is there to try again
  if ((await cmd("session.resync", { session: SID })).ok) await load();
}

// the new-side content of a highlighted line range, pulled from the diff hunks (for suggestion pre-fill)
function newSideLines(path, lo, hi) {
  const file = activeFiles().find((f) => f.path === path);
  if (!file) return "";
  const out = [];
  (file.hunks || []).forEach((h) => {
    let newLine = 0;
    (h.diff || "").split("\n").forEach((raw) => {
      if (raw.startsWith("@@")) { const m = raw.match(/\+(\d+)/); if (m) newLine = parseInt(m[1], 10); return; }
      if (raw === "") return;
      if (raw[0] === "-") return;                       // deleted lines aren't on the new side
      if (newLine >= lo && newLine <= hi) out.push(raw.slice(1));
      newLine += 1;
    });
  });
  return out.join("\n");
}

function setStatus(msg) { $("status").textContent = msg || ""; }

// --- is Claude working, or is nothing listening? -----------------------------
// The server answers this now. `chat:<sid>` publishes the join of presence with what the review is
// still owed, because neither half means anything on its own: a watcher parked in wait() is idle by
// definition, and an ask nobody is listening for is not a slow answer. The rule — including why an
// agent's own question back is not an ask — lives in review_mate/view/asks.py. The browser renders
// the word it is given.

// every file in the repository at this change's sha — subscribed only while the browser is open,
// so a reviewer who never opens it never pays for the read
function treeView() {
  return scopeViews[`tree:${SID}`] || null;
}

function repoPaths() {
  const view = treeView();
  return view && view.state === "ready" ? view.paths : [];
}

// the commits this change is made of, subscribed only while reviewing one at a time
function commitsView() {
  return scopeViews[`commits:${SID}`] || null;
}

function commitRows() {
  const view = commitsView();
  return view && view.state === "ready" ? view.commits : [];
}

// what Claude has asked to read, and what was decided
function accessView() {
  const view = scopeViews[`access:${SID}`];
  return view && view.state === "ready" ? view : null;
}

function pendingAccess() {
  const view = accessView();
  return view ? view.requests.filter((r) => r.status === "pending") : [];
}

// Every request, decided ones included. What the reviewer answered is worth seeing after they
// answered it — an agent re-asking for what was refused reads very differently from a first ask,
// and an approval that produced nothing is something only they can chase.
function accessRequests() {
  const view = accessView();
  return view ? view.requests : [];
}

// What became of an approval, in the reviewer's terms. The distinction that carries the weight is
// the one between an approval being worked on and an approval nothing is working on: the scope
// leaves the grant absent for the second, and this must not paper over it.
function grantLine(r) {
  if (r.status === "denied") return { text: "denied", cls: "no" };
  if (r.status !== "approved") return null;
  const g = r.grant;
  if (!g) return { text: "approved — nothing is fetching it", cls: "warn" };
  if (g.state === "materializing") return { text: "fetching the repository…", cls: "work" };
  if (g.state === "ready") return { text: g.path || "ready", cls: "ok", path: true };
  return { text: g.error || "could not be fetched", cls: "no" };
}

// the discussions on the merge request, as the host last reported them
function threadsView() {
  const view = scopeViews[`threads:${SID}`];
  return view && view.state === "ready" ? view : null;
}

function allThreads() {
  const view = threadsView();
  return view ? view.threads : [];
}

function threadById(id) {
  return allThreads().find((t) => t.id === id) || null;
}

// what this review has prepared to send, and what it would take to send it
function reviewView() {
  const view = scopeViews[`review:${SID}`];
  return view && view.state === "ready" ? view : null;
}

// `checked` is not the same as "nobody has approved": until the host has been asked, the bar must
// not claim either way, which is why this returns null rather than an empty answer
function reviewApproval() {
  const view = reviewView();
  return view && view.approval && view.approval.checked ? view.approval : null;
}

function reviewVersion() {
  const view = reviewView();
  return view ? view.version : null;
}

// this session's chats with the agent, and the agent state they add up to
function chatIndex() {
  return scopeViews[`chat:${SID}`] || null;
}

const AGENT_OFF = { state: "off", stale: false, since: null, asks: [] };

// A review page reads its session's own join. The landing page has no session, so the hub answers
// the narrower question it owns — is anyone listening at all — and each row carries its own count.
function agentState() {
  if (SID) {
    const view = chatIndex();
    return view && view.state === "ready" && view.agent ? view.agent : AGENT_OFF;
  }
  const hub = scopeViews.hub;
  const attached = !!(hub && hub.agent && hub.agent.attached);
  return { ...AGENT_OFF, state: attached ? "watching" : "off", attached };
}

function elapsed(iso) {
  const t = Date.parse(iso || "");
  if (!t) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

const AGENT_TITLE = {
  working: "Claude is watching and has work outstanding",
  stalled: "you asked Claude something, but no agent is watching — start one with the review-mate skill",
  watching: "Claude is watching this review",
  off: "no agent is watching — start one with the review-mate skill",
};
const AGENT_STALE_TITLE = "an agent is watching but hasn't picked this up — it may never have " +
  "received the request (a server restart drops in-flight notifications). Re-send it, or ask the " +
  "agent to reconcile from durable state.";

// the header light: colour + pulse for the state, and a word only when it needs the reviewer's eye
function renderAgentLight() {
  const el = $("agent");
  if (!el) return;
  const st = agentState();
  el.className = "agent " + st.state + (st.stale ? " stale" : "");
  el.title = st.stale ? AGENT_STALE_TITLE : AGENT_TITLE[st.state];
  el.querySelector(".alab").textContent =
    st.stale ? `Claude · ${elapsed(st.since)} · no progress` :
    st.state === "working" ? `Claude · ${elapsed(st.since)}` :
    st.state === "stalled" ? "no agent watching" : "";
}

// the inline "still waiting" line, next to whatever the reviewer asked (a chat turn, a highlight).
// data-since lets the ticker age it in place, without re-rendering the panel under their cursor.
// `mini` is the terse form for the highlight index, where there's room for a few words.
function agentWaitLine(since, mini) {
  const el = document.createElement("div");
  el.dataset.since = since || "";
  el.innerHTML = `<span class="spin"></span><span class="awtext"></span>`;
  if (mini) el.dataset.mini = "1";
  paintWaitLine(el);
  return el;
}

// Staleness is the session's reading, not this line's: the threshold that decides it has one owner
// on the server, and a second copy here to age each line separately would be that owner again.
function paintWaitLine(el) {
  const st = agentState();
  const stalled = st.state === "stalled";
  el.className = "agentwait " + (stalled ? "stalled" : st.stale ? "stale" : "working")
               + (el.dataset.mini ? " mini" : "");
  const age = elapsed(el.dataset.since);
  el.querySelector(".awtext").textContent = el.dataset.mini
    ? (stalled ? `not picked up · ${age}` : st.stale ? `no progress · ${age}` : `Claude working · ${age}`)
    : (stalled ? `no agent is watching — waiting ${age}, nothing has picked this up`
       : st.stale ? `waiting ${age} with an agent attached — it may never have received this; re-send it`
                  : `Claude is working on it… ${age}`);
}

// One ticker, and it only ages the labels: the state itself arrives on the stream.
function startAgentWatch() {
  const tick = () => {
    renderAgentLight();
    document.querySelectorAll(".agentwait").forEach(paintWaitLine);
  };
  tick();
  setInterval(tick, 1000);
}

// --- the hub scope ----------------------------------------------------------
// The landing page renders the `hub` view document and derives nothing from it: the per-review
// verdict, the counts and the row order are all the server's. The terminal client reads the same
// scope, so the two can never disagree about what a review's state is.

function connectViews(scopes) {
  wantedScopes = scopes.slice();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  viewSocket = new WebSocket(`${proto}://${location.host}/api/stream`);
  // a reconnect re-subscribes to everything wanted and is sent each scope's current view, so
  // there is no local state to reconcile
  viewSocket.onopen = () => viewSocket.send(JSON.stringify({ action: "subscribe", scopes: wantedScopes }));
  viewSocket.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === "scope") {
      scopeViews[msg.scope] = msg.view;   // whole-scope replacement — nothing to merge
      renderAgentLight();                 // the state rides a scope, so it repaints with one
      if (msg.scope === "hub") {
        markHubReady();
        if (!SID) showLanding();
      } else if (SID && state) {
        // a full render, not just the diff: the mode's file list drives the tree and decides
        // which file is selected, and a frame can arrive before either has caught up
        render();
      }
    } else if (msg.type === "error") {
      setStatus("✕ " + (msg.reason || "stream error"));
    }
  };
  viewSocket.onclose = () => { viewSocket = null; setTimeout(() => connectViews(wantedScopes), 1000); };
}

// subscribe to scopes this page now needs, and drop the ones it no longer shows
function watchScopes(scopes) {
  const fresh = scopes.filter((s) => !wantedScopes.includes(s));
  const stale = wantedScopes.filter((s) => !scopes.includes(s) && s !== "hub");
  if (!fresh.length && !stale.length) return;
  wantedScopes = wantedScopes.filter((s) => !stale.includes(s)).concat(fresh);
  stale.forEach((s) => delete scopeViews[s]);
  if (viewSocket && viewSocket.readyState === WebSocket.OPEN) {
    if (stale.length) viewSocket.send(JSON.stringify({ action: "unsubscribe", scopes: stale }));
    if (fresh.length) viewSocket.send(JSON.stringify({ action: "subscribe", scopes: fresh }));
  }
}

// which version of the change is being read. It lives in the scope name, so switching is a
// subscription rather than a fetch — and there is no second place for it to be recorded.
// which commit is being read. The list arrives on its own scope, so this derives rather than
// waits: the moment the commits are known the mode names one, and the file scope follows in the
// same render instead of a frame later.
function currentCommitSha() {
  const rows = commitRows();
  if (currentCommit && rows.some((c) => c.sha === currentCommit)) return currentCommit;
  return rows.length ? rows[0].sha : null;
}

function diffMode() {
  const sha = commitsMode ? currentCommitSha() : null;
  if (sha) return `commit@${sha}`;
  return sinceLast ? "since" : "full";
}

// the scopes the review page reads: the file list, the file being shown, any whole file it needs,
// the annotations, the chat index that carries the agent's state, the review it is preparing, and the
// discussions already on the merge request
function diffScopes() {
  const mode = diffMode();
  const listing = `diff:${SID}:${mode}`;
  const scopes = [listing, `annotations:${SID}`, `chat:${SID}`, `review:${SID}`, `threads:${SID}`,
                  `access:${SID}`];
  if (currentFile) scopes.push(`${listing}:${currentFile}`);
  if (showAll) scopes.push(`tree:${SID}`);        // the file browser, only while it is open
  if (commitsMode) scopes.push(`commits:${SID}`); // and the commit list, only while reviewing one
  if (selected) scopes.push(chatScope(selected));   // only the chat on screen
  blobWanted.forEach((path) => scopes.push(`blob:${SID}:${mode}:${path}`));
  return scopes;
}

// --- one subject, two channels ----------------------------------------------
// A chat's subject is whatever the detail panel opens, addressed exactly as the protocol
// addresses it (review_mate/view/chat.py). `mr` is the review itself: its Claude channel is the
// chat anchored to nothing, and its review channel is the MR-level note everyone sees.
const SUBJECT_KIND = { hl: "highlight", insight: "insight", thread: "thread" };

function subjectAnchor(sel) {
  if (!sel || sel.kind === "mr") return null;
  return { kind: SUBJECT_KIND[sel.kind], id: sel.id };
}

function chatScope(sel) {
  const a = subjectAnchor(sel);
  return a ? `chat:${SID}:${a.kind}:${a.id}` : `chat:${SID}:review`;
}

function conversationMessages(sel) {
  const view = scopeViews[chatScope(sel)];
  return view && view.state === "ready" ? view.messages : [];
}

// this subject's row in the chat index — counts and who spoke last, without opening it
function conversationRow(sel) {
  const index = chatIndex();
  if (!index || index.state !== "ready") return null;
  const a = subjectAnchor(sel);
  const kind = a ? a.kind : "review";
  const id = a ? a.id : "";
  return (index.chats || []).find((c) => c.kind === kind && c.id === id) || null;
}

// Which side the chat is waiting on. `owed` is the server's and means an answer is actually
// outstanding. The other direction has no server fact behind it by design — view/asks.py declines to
// read Claude's last word as a question, because nothing in a message body distinguishes one — so it
// is read here from who spoke last and means only that: Claude spoke, your turn if you want it.
function owedSide(sel) {
  const row = conversationRow(sel);
  if (!row || !row.count) return null;
  if (row.owed) return "claude";
  return row.last_role === "assistant" ? "you" : null;
}

function owedMarker(sel) {
  const side = owedSide(sel);
  if (!side) return null;
  const el = document.createElement("span");
  el.className = "owed " + side;
  el.textContent = side === "claude" ? "Claude ▸" : "you ▸";
  el.title = side === "claude" ? "you spoke last — Claude owes an answer"
                               : "Claude spoke last — your turn, if you want it";
  return el;
}

// the view behind the current mode's file list, or null until it arrives
function listingView() {
  return scopeViews[`diff:${SID}:${diffMode()}`] || null;
}

// the hunks for a path, as the server built them — null while the scope has not arrived
function scopeHunks(path) {
  const view = scopeViews[`diff:${SID}:${diffMode()}:${path}`];
  return view && view.state === "ready" ? view.hunks : null;
}

// what this review has asked about, as the server folded it: numbering, state, cards, host context
function annotationsView() {
  return scopeViews[`annotations:${SID}`] || null;
}

function annotationHighlights() {
  const view = annotationsView();
  return view && view.state === "ready" ? view.highlights : [];
}

// the MR-wide pass: whether one is running, whether it is about code that has moved, and whether
// asking now would say anything new. All three are the server's — see view/annotations.py
function reviewPass() {
  const view = annotationsView();
  return (view && view.state === "ready" && view.review_pass) || null;
}

function annotationInsights() {
  const view = annotationsView();
  return view && view.state === "ready" ? view.insights : [];
}

// The lens: read the change by what matters rather than by the order Claude happened to find it in.
// Unlabelled sorts last, not lowest — nobody classified it, which is not the same as deciding it is
// unimportant, and burying it under the lows would make that decision silently.
const CRITICALITY_RANK = { high: 0, medium: 1, low: 2 };
let insightTheme = "";          // "" = every theme

function sortedInsights() {
  const rows = annotationInsights().filter((c) => !insightTheme
                                    || (c.label && c.label.theme === insightTheme));
  return rows.slice().sort((a, b) => {
    const ra = a.label ? CRITICALITY_RANK[a.label.criticality] : 3;
    const rb = b.label ? CRITICALITY_RANK[b.label.criticality] : 3;
    return ra - rb;
  });
}

function insightThemes() {
  const seen = [];
  annotationInsights().forEach((c) => {
    if (c.label && !seen.includes(c.label.theme)) seen.push(c.label.theme);
  });
  return seen.sort();
}

function annotationHighlight(id) {
  return annotationHighlights().find((h) => h.id === id) || null;
}

// a whole file at the MR head: [{n, text, tokens}], or null until its scope arrives
function blobLines(path) {
  const view = scopeViews[`blob:${SID}:${diffMode()}:${path}`];
  return view && view.state === "ready" ? view.lines : null;
}

function blobText(path) {
  const lines = blobLines(path);
  return lines === null ? undefined : lines.map((l) => l.text).join("\n");
}

// ask for a file's content — the scope arrives on the stream and the page re-renders
function wantBlob(path) {
  if (blobWanted.has(path)) return;
  blobWanted.add(path);
  watchScopes(diffScopes());
}

// the single write path — every landing-page action is one named command. Reports the status
// rather than a bare failure, because a rejected reference (400) is a prompt for suggestions
// while anything else is a real error to surface.
async function cmd(name, args) {
  let r, data;
  try {
    r = await fetch("/api/cmd", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ cmd: name, args: args || {} }),
    });
    data = await r.json().catch(() => ({}));
  } catch (e) {
    return { ok: false, status: 0, reason: String(e) };
  }
  if (!r.ok || !data.ok) {
    return { ok: false, status: r.status, reason: data.reason || `${name} failed (${r.status})` };
  }
  return { ok: true, status: r.status, ...data };
}

// the queue page doubles as the session hub: resume or close an in-flight review
// the per-review state (server-derived) → a chip label + a row class for colour
const REVIEW_STATE = {
  merged:      { label: "✓ merged",           cls: "st-merged" },
  closed:      { label: "closed",             cls: "st-closed" },
  git_update:  { label: "↑ git update",       cls: "st-git" },
  discussions: { label: "open discussions",   cls: "st-disc" },
  in_progress: { label: "in progress",        cls: "st-prog" },
  reviewed:    { label: "reviewed",           cls: "st-done" },
  new:         { label: "not started",        cls: "st-new" },
};

async function checkReviewStates() {
  if (refreshing) return;
  refreshing = true;
  setStatus("checking your open reviews…");
  showLanding();                      // paint the "checking…" affordance before the fan-out
  const res = await cmd("hub.refresh");
  if (!res.ok) setStatus("✕ " + res.reason);
  refreshing = false;
  setStatus("");
  showLanding();
}

function agoText(ms) {
  const m = Math.round((Date.now() - ms) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m}m ago` : `${Math.round(m / 60)}h ago`;
}

function renderOpenSessions(land, sessions) {
  if (!sessions.length) return;
  const hd = document.createElement("div");
  hd.className = "hubhdr";
  hd.appendChild(h2("Open reviews"));
  hd.appendChild(btn(refreshing ? "checking…" : "↻ check for updates", "btn ghost", checkReviewStates));
  const checkedAt = scopeViews["hub"] && scopeViews["hub"].host_checked_at ? Date.parse(scopeViews["hub"].host_checked_at) : 0;
  if (checkedAt) {
    const note = document.createElement("span"); note.className = "hubago";
    note.textContent = `checked ${agoText(checkedAt)}`;
    hd.appendChild(note);
  }
  land.appendChild(hd);
  const list = document.createElement("div");
  list.style.margin = "0 0 26px";
  // no sort here: rows arrive in the order the server decided, so every client shows the same one
  sessions.forEach((s) => {
    const mr = s.mr || {};
    const loc = mr.label ? esc(mr.label) : "(no MR loaded)";
    const bits = [];
    if (s.highlights) bits.push(`${s.highlights} highlight${s.highlights > 1 ? "s" : ""}`);
    if (s.cards) bits.push(`${s.cards} card${s.cards > 1 ? "s" : ""}`);
    if (s.pending) bits.push(`${s.pending} draft${s.pending > 1 ? "s" : ""}`);
    if (s.posted) bits.push(`${s.posted} posted`);
    // the chip reports host-derived state, so it appears once a check has priced this review in
    const meta = s.host_checked ? REVIEW_STATE[s.state] : null;
    const label = !meta ? "" :
      (s.state === "discussions" && s.unresolved)
        ? `${s.unresolved} open discussion${s.unresolved > 1 ? "s" : ""}`
        : meta.label;
    const row = document.createElement("div");
    row.className = "sitem" + (meta ? " " + meta.cls : "");
    // the title is a real link to the review (stretched over the card, see .rowlink) and the
    // project!iid a real link to the MR on the host — both middle-clickable into their own tab
    row.innerHTML =
      `<button class="x" title="close review">×</button>` +
      `<div class="t">${meta ? `<span class="ststate">${esc(label)}</span> ` : ""}` +
      `<a class="rowlink" href="?s=${encodeURIComponent(s.id)}">${esc(mr.title || "(untitled review)")}</a></div>` +
      `<div class="m">${hostLink(mr.url, loc)}${bits.length ? " · " + bits.join(" · ") : ""}</div>`;
    row.querySelector(".x").onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      closeSession(s.id, s.pending);
    };
    list.appendChild(row);
  });
  land.appendChild(list);
}

async function closeSession(id, pending) {
  if (pending && !confirm(`This review has ${pending} unsubmitted comment(s). Close it anyway?`)) return;
  const res = await cmd("session.close", { id });   // the hub republishes itself; nothing to re-fetch
  if (!res.ok) setStatus("✕ " + res.reason);
}

// --- links: every navigable thing on this page is a real link ----------------
// The queue page is a list of destinations, so each one is an <a> the browser owns: ctrl/middle-click
// and "open in new tab" work, and a reviewer can fan several reviews out into tabs. Rows keep their
// whole-card click target via .rowlink's stretched overlay; buttons on the row sit above it.

// project!iid as a link to the MR on the host — the same outward idiom as the header's .mrlink.
// `label` is already-escaped HTML; falls back to plain text for an entry with no URL.
function hostLink(url, label) {
  return url ? `<a class="extlink" href="${esc(url)}" target="_blank" rel="noopener">${label} ↗</a>` : label;
}

// the session already reviewing this MR, if any — so a ?ref= link (or a stale Track button) resumes
// that review instead of opening a second one for the same MR
async function findTracking(ref) {
  await hubReady;   // a cold ?ref= tab lands here before the first scope has arrived
  const sessions = (scopeViews["hub"] && scopeViews["hub"].sessions) || [];
  return sessions.find((s) => s.mr && `${s.mr.project}!${s.mr.iid}` === ref) || null;
}

// "Track": flag a queue entry for review without leaving the queue. It starts the review session, so
// the MR moves up into "Open reviews" — where check-for-updates then watches it — and the reviewer
// carries on triaging the rest of the queue.
async function trackRef(ref, button) {
  button.disabled = true; button.textContent = "tracking…";
  setStatus("tracking " + ref + "…");
  if (await findTracking(ref)) { setStatus(ref + " is already in your open reviews"); showLanding(); return; }
  const res = await cmd("session.open", { ref });
  if (!res.ok) {
    setStatus("✕ " + res.reason);
    button.disabled = false; button.textContent = "Track";
    return;
  }
  setStatus("tracking " + ref + " — it's in your open reviews");
  // the entry moves from the queue into "Open reviews" when the republished scope arrives
}

// the ?ref= landing: open the review a queue link points at, resuming the existing session when the
// MR is already tracked, so following the link twice never duplicates a review
async function openRef(ref) {
  setStatus("opening " + ref + "…");
  const land = document.createElement("div");   // the tab lands here cold — say what it's doing
  land.className = "land";
  land.appendChild(h2("Opening " + ref));
  land.appendChild(empty("resolving the merge request…"));
  const d = $("diff"); d.innerHTML = ""; d.appendChild(land);
  const hit = await findTracking(ref);
  if (hit) return location.replace(`?s=${encodeURIComponent(hit.id)}`);
  const opened = await cmd("session.open", { ref });
  if (!opened.ok) { setStatus("✕ " + opened.reason); return showLanding(); }
  location.replace(`?s=${encodeURIComponent(opened.session)}`);   // replace: no ?ref= left in history
}

// one MR entry in a picker list — the review queue, search results, Claude's candidates. All three
// offer Track: wherever you come across an MR, flagging it for later is the cheaper action than
// opening it. The row links to the review; the project!iid links out to the MR on the host.
function mrItem(it) {
  const ref = `${it.project}!${it.iid}`;
  const row = document.createElement("div");
  row.className = "qitem";
  row.innerHTML =
    `<div class="t"><a class="rowlink" href="?ref=${encodeURIComponent(ref)}">${esc(it.title)}</a></div>` +
    `<div class="m">${hostLink(it.url, `${esc(it.project)} !${it.iid}`)}</div>`;
  const b = btn("Track", "btn track", null);
  b.title = "flag this MR for review — adds it to your open reviews without opening it";
  b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); trackRef(ref, b); };
  row.appendChild(b);
  return row;
}

async function showLanding() {
  $("mr").textContent = "—";
  $("files").innerHTML = ""; $("ann").innerHTML = "";
  const land = document.createElement("div");
  land.className = "land";
  const d = $("diff"); d.innerHTML = ""; d.appendChild(land);

  if (!scopeViews["hub"]) { land.appendChild(empty("connecting to the review server…")); return; }
  // open reviews are the local half of the scope and are already here; the queue half carries its
  // own loading state, so a slow host delays the queue block and nothing else
  const active = scopeViews["hub"].sessions || [];
  renderOpenSessions(land, active);

  const head = document.createElement("div");
  head.className = "queuehdr";      // its own name: two headings on this page, and only one queue
  head.innerHTML = `<h2>Pick a merge request</h2>`;
  land.appendChild(head);
  const queueBox = document.createElement("div");
  land.appendChild(queueBox);
  renderQueue(queueBox, scopeViews["hub"], active);
}

function renderQueue(box, view, openSessions) {
  box.innerHTML = "";
  const queueState = view.queue_state || "idle";
  if (queueState === "idle" || queueState === "loading") {
    box.appendChild(empty("loading your review queue…"));
    return;
  }
  if (queueState === "error") {
    box.appendChild(empty("your review queue is unavailable — " + (view.queue_error || "host read failed")));
    return;
  }
  // drop MRs already open as reviews — they're listed under "Open reviews" above, not the queue
  const open = new Set((openSessions || []).filter((s) => s.mr).map((s) => `${s.mr.project}!${s.mr.iid}`));
  let items = (view.queue || []).filter((it) => !open.has(`${it.project}!${it.iid}`));
  if (!items.length) {
    box.appendChild(empty("your review queue is empty here — paste an MR reference in the top bar (URL or group/proj!iid)."));
    return;
  }
  const filter = document.createElement("input");
  filter.className = "qfilter";
  filter.placeholder = "filter the queue…";
  const list = document.createElement("div");
  const matches = (it, q) => !q || `${it.title} ${it.project} !${it.iid}`.toLowerCase().includes(q);
  const renderList = () => {
    const q = filter.value.trim().toLowerCase();
    list.innerHTML = "";
    const shown = items.filter((it) => matches(it, q));
    if (!shown.length) { list.appendChild(empty("no queue entries match")); return; }
    shown.forEach((it) => list.appendChild(mrItem(it)));
  };
  filter.oninput = renderList;
  box.appendChild(filter);
  box.appendChild(list);
  renderList();
}

async function loadRef(ref) {
  ref = (ref || "").trim();
  if (!ref) {   // no input → don't create an empty, MR-less session; nudge toward a ref or the list
    setStatus("enter an MR URL or group/proj!iid — or pick one from the list below");
    $("ref").focus();
    return;
  }
  const btn = $("load");
  btn.disabled = true; btn.textContent = "Loading…"; setStatus("resolving " + ref + "…");
  try {
    const res = await cmd("session.open", { ref });
    if (!res.ok) {
      setStatus("");
      // 400 = the reference didn't parse → fall back to flexible search suggestions.
      // Anything else (e.g. 502, a GitLab/auth failure) is a real error — surface it
      // instead of masking it as "no match".
      if (res.status === 400 && ref) renderSuggestions(ref);
      else setStatus("✕ " + res.reason);
      return;
    }
    location.search = `?s=${res.session}`;  // reload cleanly into the loaded session
  } finally {
    btn.disabled = false; btn.textContent = "Load";
  }
}

async function load() {
  state = await fetch(`/api/sessions/${SID}`).then((r) => r.json());
  if (!currentFile && state.files.length) currentFile = state.files[0].path;   // the session's own list seeds it
  render();
}

function toggleSinceLast() {
  sinceLast = !sinceLast;
  viewingPath = null;   // both views are diff views — drop any non-diff repo file being shown
  if (sinceLast) { commitsMode = false; $("t-commits").classList.toggle("on", false); }  // one diff mode at a time
  currentFile = null;   // the mode has its own file list; pick its first
  render();             // the new mode's scope is subscribed on render and arrives on the stream
}

async function markReviewed() {
  setStatus("marking reviewed…");
  const result = await cmd("review.mark_reviewed", { session: SID });
  setStatus(result.ok ? "marked reviewed up to the current version" : "✕ " + result.reason);
}

let wsTimer = null;
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // state.seq is the event log's offset, not a scope's seq — this stream is the one you resume
  const ws = new WebSocket(`${proto}://${location.host}/api/sessions/${SID}/stream?since=${state.seq}`);
  ws.onmessage = () => { clearTimeout(wsTimer); wsTimer = setTimeout(load, 60); };
  ws.onclose = () => setTimeout(connectWS, 1000);
}

async function post(command) {
  await fetch(`/api/sessions/${SID}/commands`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify(command),
  });
}

// --- toolbar ----------------------------------------------------------------

// dark is the bare :root, light is an attribute override — same resolution order as the no-flash
// script in index.html, which sets the attribute before first paint but can't touch the button glyph.
const lightScheme = matchMedia("(prefers-color-scheme: light)");
function applyTheme() {
  const stored = localStorage.getItem("rm-theme");
  const light = stored ? stored === "light" : lightScheme.matches;
  document.documentElement.dataset.theme = light ? "light" : "";
  $("t-theme").textContent = light ? "☀" : "☾";
}
lightScheme.addEventListener("change", () => {   // follow the OS until the reviewer pins a theme
  if (!localStorage.getItem("rm-theme")) applyTheme();
});

function wireToolbar() {
  $("t-left").onclick = () => $("shell").classList.toggle("hl");
  $("t-right").onclick = () => $("shell").classList.toggle("hr");
  applyTheme();
  $("t-theme").onclick = () => {
    const light = document.documentElement.dataset.theme !== "light";
    localStorage.setItem("rm-theme", light ? "light" : "dark");
    applyTheme();
  };
  $("t-split").classList.toggle("on", splitMode);
  $("t-split").onclick = () => {
    splitMode = !splitMode;
    localStorage.setItem("rm-split", splitMode ? "1" : "0");
    $("t-split").classList.toggle("on", splitMode);
    renderDiff();
  };
  $("t-commits").onclick = toggleCommits;
  $("load").onclick = () => { const v = $("ref").value.trim(); loadRef(v); };
  $("ref").addEventListener("keydown", (e) => { if (e.key === "Enter") $("load").click(); });
  // live suggestions while typing — only on the landing page, so we never hijack a loaded diff
  $("ref").addEventListener("input", (e) => {
    if (SID) return;
    const v = e.target.value.trim();
    clearTimeout(suggestTimer);
    if (v.length < 2) { showLanding(); return; }
    suggestTimer = setTimeout(() => { if (!SID && $("ref").value.trim() === v) renderSuggestions(v); }, 250);
  });
}

let suggestTimer = null;
async function renderSuggestions(query) {
  const d = $("diff");
  d.innerHTML = "";
  const land = document.createElement("div");
  land.className = "land";
  land.innerHTML = `<h2>Search results</h2><p>GitLab matches for “${esc(query)}”</p>`;
  // GitLab search (code-first, D20) is the default; results render here first
  const list = document.createElement("div");
  list.className = "searchresults";     // its own name: the answer panel below renders the same rows
  list.appendChild(empty("searching GitLab…"));
  land.appendChild(list);
  // escape hatch (D20): a fuzzy description routed to the agent, always offered — the host search
  // can return plenty of matches and still miss the one the reviewer meant
  const fallback = document.createElement("div");
  fallback.className = "askrow";
  const claudePanel = document.createElement("div");
  claudePanel.className = "claudepanel";
  land.appendChild(fallback);
  land.appendChild(claudePanel);
  d.appendChild(land);
  if (!SID) { $("files").innerHTML = ""; $("ann").innerHTML = ""; }
  let data = null;
  try { data = await fetch(`/api/search?q=${encodeURIComponent(query)}`).then((r) => r.json()); }
  catch (e) { data = { error: String(e) }; }
  if ($("ref").value.trim() !== query) return;  // box moved on while we fetched
  list.innerHTML = "";
  if (data && data.error) {   // surface a real host failure instead of masking it as "no matches"
    const auth = /401|403|unauthor|forbidden/i.test(data.error);
    const err = document.createElement("div");
    err.className = "searcherr";
    err.textContent = auth
      ? "⚠ GitLab authentication failed. Run `glab auth login` (or let glab refresh), then search again — the server reloads credentials automatically, no restart needed."
      : "⚠ GitLab error: " + data.error;
    list.appendChild(err);
    renderAskRow(fallback, claudePanel, query, "failed");
    return;
  }
  const items = Array.isArray(data) ? data : [];
  items.forEach((it) => list.appendChild(mrItem(it)));
  if (!items.length) list.appendChild(empty("no GitLab matches — try another term, or paste a full MR URL"));
  renderAskRow(fallback, claudePanel, query, items.length ? "hits" : "empty");
}

// The way to reach Claude about finding an MR, offered whatever the host search did. A search that
// returned ten matches and none of them the right one is the commonest case of all, and a search
// that errored is the one where the agent is the only route left.
//
// The box is not the search box. What the host search wants is a term; what Claude wants is a
// description, and making the reviewer overwrite one with the other would re-run the host search
// and throw away the answer they are reading.
function renderAskRow(row, panel, query, outcome) {
  row.innerHTML = "";
  row.appendChild(document.createTextNode({
    hits: "Not the one? Describe it instead: ",
    empty: "Looking for it by description? ",
    failed: "GitLab search is unavailable — Claude may still find it: ",
  }[outcome]));
  const box = document.createElement("input");
  box.className = "askbox";
  box.id = "askbox";
  box.value = query;
  box.placeholder = "the MR that reworked the retry backoff";
  const ask = () => {
    const described = box.value.trim();
    if (described) askClaude(described, panel);
  };
  box.onkeydown = (e) => { if (e.key === "Enter") ask(); };
  row.appendChild(box);
  row.appendChild(btn("✦ Ask Claude to find it", "btn ghost", ask));
}

// route a fuzzy query to Claude's lookup channel; render its answer + loadable candidates
async function askClaude(query, panel) {
  // What counts as "the reviewer moved on" is the search box changing, not this description — they
  // can rewrite the description as often as they like without abandoning the search it belongs to.
  const searching = $("ref").value.trim();
  panel.innerHTML = "";
  // the lookup channel has no session to hang a wait line on — say it plainly instead. Only once
  // the hub has arrived: an unknown watcher must not read as an absent one.
  panel.appendChild(empty(scopeViews.hub && !agentState().attached
    ? "asking Claude… — but no agent is watching, so this will go unanswered"
    : "asking Claude…"));
  let id = null;
  try {
    const r = await fetch("/api/lookup", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ query }),
    });
    if (!r.ok) { panel.innerHTML = ""; panel.appendChild(empty("lookup unavailable")); return; }
    id = (await r.json()).id;
  } catch (e) { panel.innerHTML = ""; panel.appendChild(empty("lookup failed")); return; }
  for (let attempt = 0; attempt < 3; attempt++) {
    if ($("ref").value.trim() !== searching) return;  // reviewer moved on
    let req = null;
    try { req = await fetch(`/api/lookup/${id}`).then((r) => r.json()); } catch (e) {}
    if (req && req.status === "answered") { renderClaudeAnswer(req, panel); return; }
  }
  panel.innerHTML = "";
  panel.appendChild(empty("Claude didn't respond — is the agent attached and watching for lookups?"));
}

function renderClaudeAnswer(req, panel) {
  panel.innerHTML = "";
  const ans = document.createElement("div");
  ans.className = "claudeans";
  ans.innerHTML = `<div class="byclaude">Claude suggests <span class="prov">· may draw on non-GitLab sources; provenance noted inline</span></div><div class="md">${md(req.answer || "")}</div>`;
  panel.appendChild(ans);
  (req.candidates || []).forEach((it) => panel.appendChild(mrItem(it)));
}

// Whether this review is of a branch on this machine rather than a merge request on a forge. It
// changes what several things are called: there is nothing to link to, nobody else to discuss it
// with, and "the merge request" is the wrong name for what the findings are about.
function local() {
  return !!(state.mr && state.mr.host === "local");
}

function render() {
  if (state.mr) {
    const m = state.mr;
    // the server names the change — a branch has no `!iid` to show, and building one here is how
    // a session that is not a merge request ends up claiming to be merge request zero
    const path = esc(m.label || "");
    const link = m.url && !local() ? `<a class="mrlink" href="${esc(m.url)}" target="_blank" rel="noopener">${path} ↗</a>` : path;
    $("mr").innerHTML = `${link} — ${esc(m.title)}`;
  } else {
    $("mr").textContent = "(no MR loaded)";
  }
  // keep the selected file valid for the active set (full vs since-last) so tree + diff agree
  if (!viewingPath) {
    const files = activeFiles();
    if (files.length && !files.some((f) => f.path === currentFile)) currentFile = files[0].path;
  }
  renderTree();
  renderDiff();
  renderAnnotations();
}

// --- file tree (nested, foldable) -------------------------------------------

function buildTree(entries) {
  const root = { dirs: {}, files: [] };
  entries.forEach((e) => {
    const parts = e.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      node.dirs[parts[i]] = node.dirs[parts[i]] || { dirs: {}, files: [] };
      node = node.dirs[parts[i]];
    }
    node.files.push({ name: parts[parts.length - 1], entry: e });
  });
  return root;
}

function renderTree() {
  const el = $("files");
  el.innerHTML = "";
  const inCommits = commitsMode && currentCommitSha();
  const inSince = sinceLast;
  if (inCommits) {   // the tree lists the current commit's files
    const hdr = document.createElement("label");
    hdr.className = "treehdr"; hdr.textContent = "files in this commit";
    el.appendChild(hdr);
  } else if (inSince) {   // the tree lists the since-last delta's files; "show all repo files" doesn't apply
    const hdr = document.createElement("label");
    hdr.className = "treehdr"; hdr.textContent = "changed since your last review";
    el.appendChild(hdr);
  } else {
    const hdr = document.createElement("label");
    hdr.className = "treehdr";
    hdr.innerHTML = `<input type="checkbox" ${showAll ? "checked" : ""}> show all repo files`;
    hdr.querySelector("input").onchange = (e) => {
      showAll = e.target.checked;
      localStorage.setItem("rm-showall", showAll ? "1" : "0");
      // opening the browser is a subscription, and closing it drops one — the repository listing
      // is read while it is being looked at and not otherwise
      watchScopes(diffScopes());
      renderTree();
    };
    el.appendChild(hdr);
  }

  const files = activeFiles();
  const diffPaths = new Set(files.map((f) => f.path));
  const entries = files.map((f) => ({ path: f.path, old_path: f.old_path,
                                     change_type: f.change_type, diff: true }));
  if (!inSince && !inCommits && showAll) {
    repoPaths().forEach((p) => { if (!diffPaths.has(p)) entries.push({ path: p, diff: false }); });
  }
  if (!entries.length) {
    el.appendChild(empty(inCommits ? "this commit changed no files" : inSince ? "no changes since your last review" : "no files"));
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "tree";
  renderNode(buildTree(entries), "", 0, wrap);
  el.appendChild(wrap);
}

function renderNode(node, path, depth, out) {
  Object.keys(node.dirs).sort().forEach((name) => {
    const dpath = path ? `${path}/${name}` : name;
    const closed = collapsedDirs.has(dpath);
    const row = document.createElement("div");
    row.className = "node dir" + (closed ? " closed" : "");
    row.style.paddingLeft = `${10 + depth * 14}px`;
    row.innerHTML = `<span class="chev">▾</span>📁 ${esc(name)}`;
    row.onclick = () => { closed ? collapsedDirs.delete(dpath) : collapsedDirs.add(dpath); renderTree(); };
    out.appendChild(row);
    const kids = document.createElement("div");
    kids.className = "children" + (closed ? " closed" : "");
    renderNode(node.dirs[name], dpath, depth + 1, kids);
    out.appendChild(kids);
  });
  node.files.sort((a, b) => a.name.localeCompare(b.name)).forEach(({ name, entry }) => {
    const row = document.createElement("div");
    row.className = "node file" + (entry.path === currentFile ? " on" : "") + (entry.diff ? "" : " nodiff");
    row.style.paddingLeft = `${10 + depth * 14 + 14}px`;
    const c = entry.diff ? ((entry.change_type || "")[0] || "~") : "·";
    // the tree nests by directory, so a rename can only diverge in the leaf here; a move between
    // directories shows under its new parent and is spelled out in the tooltip
    const moved = entry.old_path && entry.old_path !== entry.path;
    const oldName = moved ? entry.old_path.split("/").pop() : null;
    const shown = oldName && oldName !== name ? `{${oldName},${name}}` : name;
    row.innerHTML = `<span class="ct ${entry.change_type || ""}">${c}</span>${esc(shown)}`;
    if (moved) row.title = `renamed: ${entry.old_path} → ${entry.path}`;
    row.onclick = () => selectFile(entry);
    out.appendChild(row);
  });
}

function selectFile(entry) {
  currentFile = entry.path;
  if (entry.diff) { viewingPath = null; render(); }
  else {
    viewingPath = entry.path;
    wantBlob(entry.path);
    render();
  }
}

// --- diff -------------------------------------------------------------------

function highlightLines(path) {
  const set = new Set();
  annotationHighlights().filter((h) => h.file === path).forEach((h) => {
    for (let l = h.start; l <= h.end; l++) set.add(l);
  });
  return set;
}

// new-side lines carrying a host discussion (GitLab thread), for a distinct diff overlay
function threadLines(path) {
  const set = new Set();
  allThreads().forEach((t) => {
    if (t.anchor && t.anchor.file === path && t.anchor.line != null) set.add(t.anchor.line);
  });
  return set;
}

// after a diff table is built, mark rows whose new-side line has a host discussion (tr.thl)
function overlayThreadAnchors(table, path) {
  threadLines(path).forEach((n) => {
    const cell = table.querySelector(`td.code[data-line="${n}"]`);
    if (cell && cell.closest("tr")) cell.closest("tr").classList.add("thl");
  });
}

// navigate the (full) diff to a file:line — used to jump from a discussion to its code
function jumpToCode(file, line) {
  if (commitsMode) { commitsMode = false; $("t-commits").classList.toggle("on", false); }
  sinceLast = false; viewingPath = null;   // the anchor is in head coords — show the full diff
  currentFile = file;
  render();
  setTimeout(() => revealLine(file, line), 0);
}

// Land on a new-side line of the file now shown, unfolding to reach it if need be. A discussion can
// be anchored to unchanged context that sits between hunks — GitLab lets you comment on expanded
// lines — and that line has no row until its gap is opened. Failing silently there reads as a broken
// link, so unfold the gap that contains it and retry; if it still can't be reached, say so.
async function revealLine(path, line) {
  const land = () => {
    const cell = line != null && $("diff").querySelector(`td.code[data-line="${line}"]`);
    if (!cell) return false;
    cell.scrollIntoView({ block: "center", behavior: "smooth" });
    const row = cell.closest("tr");
    if (row) { row.classList.add("flash"); setTimeout(() => row.classList.remove("flash"), 1600); }
    return true;
  };
  if (land()) return;
  const file = activeFiles().find((f) => f.path === path);
  const gap = file ? gapContaining(file, line) : null;
  if (gap !== null) {
    await expandGap(path, gap, "all");   // fetches the blob if needed, then re-renders
    if (land()) return;
  }
  setStatus(`could not reach ${path}:${line} in the diff` + (splitMode ? " — try unified view" : ""));
}

// Which folded gap holds a new-side line, keyed the way renderUnifiedUnfoldable lays them out: the
// span before each hunk, then the tail after the last. null when the line is inside a hunk (already
// rendered, or a deletion with no new-side row) — nothing to unfold.
function gapContaining(file, line) {
  if (line == null) return null;
  const hunks = scopeHunks(file.path) || [];
  let cursor = 1;
  for (const h of hunks) {
    if (line < h.new_start) return line >= cursor ? cursor : null;
    if (line < h.new_start + h.new_count) return null;    // inside this hunk
    cursor = h.new_start + h.new_count;
  }
  return line >= cursor ? cursor : null;                   // past the last hunk — the trailing gap
}

function highlightExact(path, lo, hi) {
  return annotationHighlights().find((h) => h.file === path && h.start === lo && h.end === hi);
}

// the per-file diffs currently in play: the since-last delta when that mode is on and parsed, else
// the full MR diff. Lets the file tree and the diff pane share one path for both views.
function activeFiles() {
  const view = listingView();
  if (!view || view.state !== "ready") return [];
  // `diff` is the tree's "this file has changes" flag; the scope calls the same thing has_diff
  return view.files.map((f) => Object.assign({}, f, { diff: f.has_diff }));
}

function renderDiff() {
  if (SID) watchScopes(diffScopes());   // the open file decides what this page watches
  const el = $("diff");
  el.innerHTML = "";
  if (commitsMode) { renderCommitView(el); return; }
  if (sinceLast) { renderSinceLast(el); return; }
  if (viewingPath) { renderFileView(el, viewingPath); return; }
  renderFileDiff(el, activeFiles(), "  ·  click a line, or drag to select a block", true);
}

// --- per-commit review ------------------------------------------------------

async function toggleCommits() {
  commitsMode = !commitsMode;
  $("t-commits").classList.toggle("on", commitsMode);
  if (commitsMode) {
    sinceLast = false; viewingPath = null;   // one diff mode at a time; both are diff views
  }
  currentFile = null;
  render();
}

function selectCommit(sha) {
  currentCommit = sha; currentFile = null;   // reset to the new commit's first file
  render();   // a different commit is a different scope — subscribed on render, arrives on the stream
}

function stepCommit(delta) {
  const rows = commitRows();
  if (!rows.length) return;
  const i = rows.findIndex((c) => c.sha === currentCommitSha());
  const j = Math.min(rows.length - 1, Math.max(0, (i < 0 ? 0 : i) + delta));
  if (rows[j]) selectCommit(rows[j].sha);
}

function renderCommitView(el) {
  const listed = commitsView();
  const rows = commitRows();
  if (!listed || listed.state === "idle" || listed.state === "loading") {
    el.appendChild(empty("loading commits…")); return;
  }
  if (listed.state === "unavailable") {
    el.appendChild(empty("this host cannot list commits")); return;
  }
  if (listed.state === "error") {
    el.appendChild(empty("✕ " + (listed.error || "commits failed"))); return;
  }
  if (!rows.length) { el.appendChild(empty("no commits on this MR")); return; }
  const i = Math.max(0, rows.findIndex((c) => c.sha === currentCommitSha()));
  const c = rows[i];
  // pair with the reviewed watermark: commits at/before it (in oldest→newest order) are reviewed,
  // the rest are new since your last review. -1 when there's no watermark or it isn't in this list.
  const wm = (reviewVersion() || {}).watermark;
  const wmIndex = wm ? rows.findIndex((x) => x.sha === wm) : -1;
  const reviewed = (k) => wmIndex >= 0 && k <= wmIndex;

  const bar = document.createElement("div");
  bar.className = "commitbar";
  const prev = btn("◀", "btn ghost", () => stepCommit(-1)); if (i <= 0) prev.disabled = true;
  const next = btn("▶", "btn ghost", () => stepCommit(1)); if (i >= rows.length - 1) next.disabled = true;
  const pos = document.createElement("span"); pos.className = "cpos"; pos.textContent = `commit ${i + 1}/${rows.length}`;
  const sel = document.createElement("select"); sel.className = "csel";
  rows.forEach((x, k) => {
    const o = document.createElement("option");
    const mark = wmIndex < 0 ? "" : (reviewed(k) ? "✓ " : "○ ");
    o.value = x.sha; o.textContent = `${mark}${k + 1}. ${(x.short_id || x.sha.slice(0, 8))} — ${x.title}`;
    if (x.sha === c.sha) o.selected = true;
    sel.appendChild(o);
  });
  sel.onchange = () => selectCommit(sel.value);
  bar.appendChild(prev); bar.appendChild(pos); bar.appendChild(next); bar.appendChild(sel);
  if (wmIndex >= 0) {
    const chip = document.createElement("span");
    chip.className = "chip " + (reviewed(i) ? "posted" : "comment");
    chip.textContent = reviewed(i) ? "✓ reviewed" : "new since review";
    bar.appendChild(chip);
  }
  el.appendChild(bar);

  const msg = document.createElement("div");
  msg.className = "commitmsg";
  const body = (c.message || c.title || "").trim();
  msg.innerHTML = `<div class="ct">${esc(c.title || "")}</div>` +
    (body && body !== (c.title || "").trim() ? `<div class="cb">${esc(body)}</div>` : "");
  el.appendChild(msg);

  const view = listingView();
  if (!view || view.state === "loading") { el.appendChild(empty("loading commit…")); return; }
  if (view.state === "unavailable") { el.appendChild(empty("unavailable on this host")); return; }
  if (view.state === "error") { el.appendChild(empty("couldn't load: " + (view.error || ""))); return; }
  const files = activeFiles();
  if (!files.length) { el.appendChild(empty("this commit changed no files")); return; }
  // The tip commit's new-side lines ARE the MR head's, so highlighting there is coordinate-correct —
  // make it interactive (highlight → card, drafts). Earlier commits stay read-only: their line numbers
  // are at that commit, so a highlight/comment would anchor to the wrong line at head.
  const short = c.short_id || c.sha.slice(0, 8);
  const isTip = !!(state.mr && c.sha === state.mr.sha);
  renderFileDiff(el, files,
    isTip ? `  ·  in ${short} (latest — click/drag to highlight)`
          : `  ·  in ${short} · older commit (read-only; use the full diff to comment)`,
    isTip);
}

// A renamed file's header, as a brace divergence over the parts of the path that actually moved:
// test/a/file.py → test/b/file.py reads `test/{a,b}/file.py`, old side first. Segments shared at
// either end stay outside the braces, so the eye lands on what changed rather than re-reading the
// whole path twice. A move that shares nothing collapses to `{old,new}`; one that only deepens or
// flattens leaves a side empty (`a/{b/c,}/f.py`).
function divergedPath(oldPath, newPath) {
  const a = oldPath.split("/"), b = newPath.split("/");
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p += 1;
  let s = 0;
  while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s += 1;
  const head = p ? a.slice(0, p).join("/") + "/" : "";
  const tail = s ? "/" + a.slice(a.length - s).join("/") : "";
  return `${head}{${a.slice(p, a.length - s).join("/")},${b.slice(p, b.length - s).join("/")}}${tail}`;
}

// what the diff header calls this file: the brace form for a rename, the plain path otherwise
function fileLabel(file) {
  return file.old_path && file.old_path !== file.path
    ? divergedPath(file.old_path, file.path) : file.path;
}

// render one file's diff (the current selection) from a file set — shared by the full diff and the
// per-file since-last view. interactive=false → read-only (no highlight overlay, no line selection,
// no unfold), used for a since-last diff whose new side sits on coordinates that don't match the head
// blob (a stale session, before refresh) and so can't anchor highlights or reveal head context.
function renderFileDiff(el, files, suffix, interactive) {
  let file = files.find((f) => f.path === currentFile);
  if (!file && files.length) { currentFile = files[0].path; file = files[0]; }
  if (!file) { el.innerHTML = '<div class="empty" style="padding:16px">select a file</div>'; return; }
  const isMd = interactive && /\.(md|markdown)$/i.test(file.path);   // full diff only (rendered = head)
  const name = document.createElement("div");
  name.className = "fname";
  const label = document.createElement("span"); label.textContent = fileLabel(file) + suffix;
  // the braces are compact but don't say which side is which — the tooltip spells the move out
  if (file.old_path && file.old_path !== file.path) {
    label.className = "renamed";
    label.title = `renamed: ${file.old_path} → ${file.path}`;
  }
  name.appendChild(label);
  if (isMd) name.appendChild(btn(mdRendered.has(file.path) ? "◱ show diff" : "◱ rendered",
                                 "btn ghost fnbtn", () => toggleMd(file.path)));
  el.appendChild(name);
  if (isMd && mdRendered.has(file.path)) { renderMarkdownDoc(el, file.path); return; }
  const hl = interactive ? highlightLines(file.path) : new Set();
  const hunks = scopeHunks(file.path);
  if (hunks === null) { el.appendChild(empty("loading " + file.path + "…")); return; }
  const table = document.createElement("table");
  table.className = "hunk";
  if (!splitMode && interactive) {
    // unified: render with "unfold" bands revealing the context between hunks (full diff, and any
    // head-aligned since-last diff — its new side is the head blob the bands reveal from)
    renderUnifiedUnfoldable(table, hunks, file.path, hl);
  } else {
    table.innerHTML = (splitMode ? splitRowsHtml : unifiedRowsHtml)(hunks, hl);
  }
  if (interactive) wireSelection(table, file.path);
  el.appendChild(table);
  if (interactive) overlayThreadAnchors(table, file.path);   // mark host-discussion lines (head coords)
}

// a rendered Markdown view of a doc's current version, toggled from the diff (the raw diff stays a
// click away). Scroll-sync to the changed hunk is a future step — this gives the reading view.
function toggleMd(path) {
  if (mdRendered.has(path)) mdRendered.delete(path); else mdRendered.add(path);
  if (mdRendered.has(path)) wantBlob(path);
  renderDiff();
}

function renderMarkdownDoc(el, path) {
  const content = blobText(path);
  if (content === undefined) { el.appendChild(empty("loading " + path + "…")); return; }
  const box = document.createElement("div");
  box.className = "mdview md";
  box.innerHTML = md(content);
  el.appendChild(box);
}

function renderUnifiedUnfoldable(table, hunks, path, hl) {
  const lines = blobLines(path);
  const exp = expandedGaps[path] || new Map();
  let cursor = 1;   // next not-yet-shown new-side line number
  hunks.forEach((h) => {
    renderGap(table, path, cursor, h.new_start - 1, lines, exp, hl);
    // the hunk's own rows are built from the scope — sides, numbers and spans are all fields
    const block = document.createElement("tbody");
    block.innerHTML = unifiedRowsHtml([h], hl);
    while (block.firstChild) table.appendChild(block.firstChild);
    cursor = h.new_start + h.new_count;
  });
  if (lines) {
    renderGap(table, path, cursor, lines.length, lines, exp, hl);   // trailing gap — exact, length known
  } else {
    // file length isn't known until the blob is fetched, so a single-hunk file that stops before EOF
    // couldn't reveal its tail. Offer a band that fetches on click; the re-render then shows the exact tail.
    const tr = document.createElement("tr");
    tr.className = "expand";
    const cell = document.createElement("td");
    cell.className = "code exp";
    const s = document.createElement("span");
    s.className = "exlink"; s.textContent = "⋯ show rest of file";
    s.onclick = () => expandGap(path, cursor, "all");
    cell.appendChild(s);
    tr.innerHTML = `<td class="ln">⋯</td>`;
    tr.appendChild(cell);
    table.appendChild(tr);
  }
}

const UNFOLD_CHUNK = 20;   // lines revealed per incremental unfold step

// a gap of new-side lines [from..to]: revealed context rows at the edges (grown incrementally) and,
// for whatever is still collapsed in the middle, a band offering ▼/▲ N-more and "show all".
function renderGap(table, path, from, to, lines, exp, hl) {
  if (to < from) return;
  const size = to - from + 1;
  const g = exp.get(from) || { top: 0, bot: 0, all: false };
  const at = (n) => (lines && lines[n - 1] !== undefined ? lines[n - 1] : { text: "", tokens: [] });
  // A revealed line comes from the blob scope already lexed. The server saw the whole file, so a
  // docstring that opens above a collapsed run and closes inside it is coloured correctly here —
  // which is what the old carry-state was approximating without ever being able to see those lines.
  const ctxRow = (n) => {
    const line = at(n);
    const tr = document.createElement("tr");
    tr.className = "line ctx" + (hl.has(n) ? " hl" : "");
    const code = " " + tokenSpans(line.text, line.tokens);   // align with the +/-/space column
    tr.innerHTML = `<td class="ln">${n}</td><td class="code" data-line="${n}">${code}</td>`;
    table.appendChild(tr);
  };
  if ((g.all || g.top + g.bot >= size) && lines) { for (let n = from; n <= to; n++) ctxRow(n); return; }
  const topN = Math.min(g.top, size);
  const botN = Math.min(g.bot, size - topN);
  if (lines) for (let n = from; n < from + topN; n++) ctxRow(n);          // revealed near the previous hunk
  const mFrom = from + topN, mTo = to - botN, mSize = mTo - mFrom + 1;    // still-collapsed middle
  if (mSize > 0) {
    const tr = document.createElement("tr"); tr.className = "expand";
    const ln = document.createElement("td"); ln.className = "ln"; ln.textContent = "⋯";
    const cell = document.createElement("td"); cell.className = "code exp";
    const link = (txt, kind) => { const s = document.createElement("span"); s.className = "exlink"; s.textContent = txt; s.onclick = () => expandGap(path, from, kind); return s; };
    if (mSize <= UNFOLD_CHUNK) {
      cell.appendChild(link(`⋯ show ${mSize} line${mSize > 1 ? "s" : ""}`, "all"));
    } else {
      cell.appendChild(link(`▼ ${UNFOLD_CHUNK}`, "top"));       // reveal downward from the top of the gap
      cell.append(" · "); cell.appendChild(link(`⋯ all ${mSize}`, "all"));
      cell.append(" · "); cell.appendChild(link(`▲ ${UNFOLD_CHUNK}`, "bot"));  // reveal upward from the bottom
    }
    tr.appendChild(ln); tr.appendChild(cell); table.appendChild(tr);
  }
  if (lines) for (let n = to - botN + 1; n <= to; n++) ctxRow(n);          // revealed near the next hunk
}

function expandGap(path, from, kind) {
  const m = expandedGaps[path] = expandedGaps[path] || new Map();
  const g = m.get(from) || { top: 0, bot: 0, all: false };
  if (kind === "all") g.all = true;
  else if (kind === "top") g.top += UNFOLD_CHUNK;
  else if (kind === "bot") g.bot += UNFOLD_CHUNK;
  m.set(from, g);
  wantBlob(path);   // revealing needs the file — the scope arrives and re-renders
  renderDiff();
}

// "since last review" — a normal diff of the author's net changes, rendered per-file like the full
// diff (falls back to the flat range-diff only when a conflicting replay forced that mode)
function renderSinceLast(el) {
  const view = listingView();
  const note = (text) => { const box = document.createElement("div"); box.className = "empty";
                           box.style.padding = "12px 16px"; box.textContent = text;
                           el.appendChild(box); };
  if (!view || view.state === "loading") return note("computing the diff…");
  if (view.state === "unavailable") return note("unavailable on this host");
  if (view.state === "error") return note("couldn't compute: " + (view.error || "unknown error"));
  const files = activeFiles();
  if (!files.length) {
    return note("No author changes since your last review (a rebase brought no new work).");
  }
  if (view.clean === false) {
    const warn = document.createElement("div"); warn.className = "sincenote";
    warn.textContent = "⚠ the replay conflicted — this diff may include target-branch changes";
    el.appendChild(warn);
  }
  // fully interactive (highlight, comment, unfold) when head-aligned — the diff's new side is then
  // the head blob, so its line numbers anchor exactly like the full diff. A stale session (head
  // moved past the session) is read-only until a refresh re-syncs the head.
  renderFileDiff(el, files, "  ·  since your last review", view.head_aligned !== false);
}

function renderFileView(el, path) {
  const name = document.createElement("div");
  name.className = "fname";
  name.textContent = path + "  ·  related file (not in the diff) · click or drag to ask for context";
  el.appendChild(name);
  const lines = blobLines(path);
  if (lines === null) { el.appendChild(empty("loading " + path + "…")); return; }
  const hl = highlightLines(path);
  const table = document.createElement("table");
  table.className = "hunk";
  lines.forEach((line) => {
    const tr = document.createElement("tr");
    tr.className = "line ctx" + (hl.has(line.n) ? " hl" : "");
    tr.innerHTML = `<td class="ln">${line.n}</td>`
                 + `<td class="code" data-line="${line.n}">${tokenSpans(line.text, line.tokens)}</td>`;
    table.appendChild(tr);
  });
  wireSelection(table, path);
  el.appendChild(table);
}
// click a line = toggle its highlight (dedupe + discard-by-reclick); drag = select a block
// The line a selection started on, kept outside the table it started in. A frame arriving between
// the press and the release rebuilds the diff, and a start held on the old table would go with it —
// the reviewer's drag silently doing nothing. Which scopes republish decides how often that
// happens, so it must not be what decides whether a selection works.
let dragStart = null;

function wireSelection(table, path) {
  const lineOf = (target) => {
    let el = target;
    while (el && el !== table) { if (el.dataset && el.dataset.line) return parseInt(el.dataset.line, 10); el = el.parentElement; }
    return null;
  };
  table.addEventListener("mousedown", (e) => { const ln = lineOf(e.target); if (ln != null) { dragStart = ln; e.preventDefault(); } });
  table.addEventListener("mouseup", (e) => {
    if (dragStart == null) return;
    const end = lineOf(e.target);
    commitSelection(path, dragStart, end == null ? dragStart : end);
    dragStart = null;
  });
}

function commitSelection(path, a, b) {
  const lo = Math.min(a, b), hi = Math.max(a, b);
  const existing = highlightExact(path, lo, hi);
  if (existing) post({ type: "remove_highlight", highlight_id: existing.id });   // re-select = discard
  else post({ type: "add_highlight", file: path, side: "new", line_range: { start: lo, end: hi } });
}

function firstLine(s) {
  const ln = (s || "").split("\n").find((l) => l.trim()) || "";
  return ln.length > 80 ? ln.slice(0, 79) + "…" : ln;
}

function annotationMatch(hl) {
  if (annotationFilter !== "all" && hl.comment_state !== annotationFilter) return false;
  if (annotationQuery) {
    const d = state.drafts.find((x) => x.highlight_id === hl.id);
    const buf = (hl.id in draftBuffers) ? draftBuffers[hl.id] : (d ? d.body : "");
    const hay = `${hl.file} ${hl.question || ""} ${buf}`.toLowerCase();
    if (!hay.includes(annotationQuery.toLowerCase())) return false;
  }
  return true;
}

// The annotations have two zones. The pinned one holds what the reviewer wants within reach whatever they
// are reading — the MR-level comment, and the insights Claude raised about the change as a whole —
// and it is capped, so its own growth cannot bury what sits under it. Everything else scrolls, in
// the order it already had: the per-line index, then the discussions, then the access requests,
// each under its own heading. The pin is about what stays reachable, not a home for every row that
// happens to be MR-wide.
function renderAnnotations() {
  const el = $("ann");
  el.innerHTML = "";

  renderVersionBanner(el);      // "updated since your last review" (diff-versions)
  renderReviewBar(el);          // submit + counts

  el.appendChild(renderMrZone());

  const list = document.createElement("div");
  list.className = "annlist";
  el.appendChild(list);

  list.appendChild(annotationSplit("Per line"));
  renderAnnotationTools(list);        // filter chips + text search
  const hlist = document.createElement("div");
  hlist.id = "hlist";
  list.appendChild(hlist);
  renderHlist();

  renderThreads(list);          // existing MR discussions — reply / resolve / refresh

  const requests = accessRequests();
  list.appendChild(h3("Access requests"));
  if (!requests.length) list.appendChild(empty("none"));
  requests.forEach((r) => {
    const box = document.createElement("div");
    box.className = "req" + (r.status === "pending" ? "" : " decided");
    box.innerHTML = `<div class="repo">${esc(r.repo)}</div><div class="why">${esc(r.reason)}</div>`;
    if (r.status === "pending") {
      box.appendChild(btn("Approve", "btn ok", () => post({ type: "decide_access", request_id: r.id, approve: true })));
      box.appendChild(btn("Deny", "btn no", () => post({ type: "decide_access", request_id: r.id, approve: false })));
    } else {
      const state = grantLine(r);
      const line = document.createElement("div");
      line.className = "grant " + state.cls + (state.path ? " path" : "");
      line.textContent = state.text;
      if (state.path) line.title = "Claude can read this checkout";
      box.appendChild(line);
    }
    list.appendChild(box);
  });

  renderDetail();

  if (annotationSearchFocused) {  // a WS-driven re-render shouldn't steal the search box you're typing in
    const s = $("annsearch");
    if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); }
  }
}

// what the whole change owns: the MR-level review comment, then the insights Claude raised itself.
// The comment is always there to be written, so it sits outside the scroller; the insights take the
// cap and scroll within it, however many arrive.
function renderMrZone() {
  const zone = document.createElement("div");
  zone.className = "annpin";
  const insights = annotationInsights();
  const whole = local() ? "The branch" : "The merge request";
  zone.appendChild(h3(insights.length ? `${whole} · ${insights.length} insights` : whole));
  zone.appendChild(reviewPassRow());
  renderMrRow(zone);
  const themes = insightThemes();
  if (themes.length > 1) zone.appendChild(themeFilter(themes));
  const box = document.createElement("div");
  box.className = "anninsights";
  sortedInsights().forEach((c) => box.appendChild(insightRow(c)));
  zone.appendChild(box);
  return zone;
}

// Narrow the findings to one kind. Only offered once there is more than one kind to choose between
// — a filter with a single option is a control that cannot do anything.
function themeFilter(themes) {
  const wrap = document.createElement("div");
  wrap.className = "themes";
  const chip = (value, text) => {
    const b = btn(text, "btn chipbtn" + (insightTheme === value ? " on" : ""),
                  () => { insightTheme = value; renderAnnotations(); });
    return b;
  };
  wrap.appendChild(chip("", "all"));
  themes.forEach((t) => wrap.appendChild(chip(t, t)));
  return wrap;
}

// Asking Claude for a pass over the whole change. The control is disabled rather than hidden while
// a pass already covers what is on screen — a control that vanishes reads as broken — and a pass
// the change has moved past says so rather than leaving a spinner that quietly stopped.
function reviewPassRow() {
  const wrap = document.createElement("div");
  wrap.className = "passrow";
  const state = reviewPass();
  const running = state && state.requested && !state.stale;
  const b = btn("✦ Review this change", "btn" + (running ? "" : " primary"),
                () => post({ type: "request_insights" }));
  b.disabled = !state || !state.available;
  b.title = running ? "Claude is reviewing this change"
          : b.disabled ? "already reviewed — ask again once the change moves"
          : "ask Claude for a pass over the whole change, alongside your own";
  wrap.appendChild(b);
  if (running) wrap.appendChild(agentWaitLine(state.at, true));
  else if (state && state.stale) {
    const note = document.createElement("span");
    note.className = "passnote"; note.textContent = "that pass was about an earlier version";
    wrap.appendChild(note);
  }
  return wrap;
}

function annotationSplit(label) {
  const el = document.createElement("div");
  el.className = "annsplit";
  el.textContent = label;
  return el;
}

function renderAnnotationTools(el) {
  if (!annotationHighlights().length) return;
  const wrap = document.createElement("div");
  wrap.className = "anntools";
  const seg = document.createElement("div"); seg.className = "seg"; seg.id = "annseg";
  wrap.appendChild(seg);
  const inp = document.createElement("input");
  inp.id = "annsearch"; inp.placeholder = "filter…"; inp.value = annotationQuery;
  inp.oninput = (e) => { annotationQuery = e.target.value; renderHlist(); };  // list-only → input keeps focus
  inp.onfocus = () => { annotationSearchFocused = true; };
  inp.onblur = () => { annotationSearchFocused = false; };
  wrap.appendChild(inp);
  el.appendChild(wrap);
  fillSeg();
}

function fillSeg() {
  const seg = $("annseg"); if (!seg) return;
  seg.innerHTML = "";
  const rows = annotationHighlights();
  const counts = { all: rows.length, context: 0, comment: 0, posted: 0 };
  rows.forEach((hl) => { counts[hl.comment_state] += 1; });
  [["all", "All"], ["context", "Cards"], ["comment", "Comments"], ["posted", "Posted"]].forEach(([k, label]) => {
    seg.appendChild(btn(`${label} ${counts[k]}`, "btn" + (annotationFilter === k ? " on" : ""),
      () => { annotationFilter = k; fillSeg(); renderHlist(); }));
  });
}

function renderHlist() {
  const list = $("hlist"); if (!list) return;
  list.innerHTML = "";
  const rows = annotationHighlights();
  if (!rows.length) { list.appendChild(empty("highlight a line to ask for context")); return; }
  let shown = 0;
  // #N is the server's, fixed when the highlight was made — a position here would move on removal
  rows.forEach((hl) => { if (annotationMatch(hl)) { list.appendChild(hlRow(hl, hl.n)); shown += 1; } });
  if (!shown) list.appendChild(empty("no highlights match this filter"));
}

// What closing the panel means, wherever it is noticed. Two paths notice it and they cannot share
// a call: the reviewer's × re-renders from the top, while a subject that vanished under the panel
// is discovered *inside* a render and must not start another. So they share this instead.
// `detailReading` is deliberately left alone — the width someone prefers to read at outlives the
// chat they were reading.
function clearSubject() {
  selected = null;
  detailTab = null;
  detailMax = false;
}

// Opening a subject is a subscription change, and so is closing one: the panel holds the
// chat it has open and no other, the way the diff holds one file.
function openSubject(sel) {
  if (sel) { selected = sel; detailTab = null; } else { clearSubject(); }
  if (SID) watchScopes(diffScopes());
  renderAnnotations();
}

// Whether a subject's code moving is a warning or a result.
//
// `stale` alone means "made on an earlier version, its lines may have moved" — the right reading
// when the head moved for reasons nobody here caused. It is the wrong reading when the agent moved
// it *because* this was what the reviewer asked for, which is the ordinary case while reviewing a
// branch before it leaves the machine. The addressing record is what tells the two apart, so a
// client reads the pair and never staleness alone.
function staleChip(row) {
  const done = row.addressed;
  if (done) {
    const at = done.sha ? done.sha.slice(0, 7) : "";
    const why = done.summary ? `${done.summary} (${at})` : `changed at ${at}`;
    return `<span class="chip fixed" title="${esc(why)}">✓ addressed</span>`;
  }
  return row.stale
    ? `<span class="chip stale" title="made on an earlier version — its lines may have moved">older ver</span>`
    : "";
}

function hlRow(hl, n) {
  const st = hl.comment_state;
  const card = hl.card;
  const d = state.drafts.find((x) => x.highlight_id === hl.id);
  const buf = (hl.id in draftBuffers) ? draftBuffers[hl.id] : (d ? d.body : "");
  const loc = `${hl.file}:${hl.start}${hl.end !== hl.start ? "-" + hl.end : ""}`;
  const chipLabel = { context: "context", comment: "comment", posted: "✓ posted" }[st];
  const prev = buf ? firstLine(buf)
             : st === "context" ? (card ? "context ready" : hl.context_requested ? "waiting for context…" : "")
             : firstLine(hl.question || "");
  const active = selected && selected.kind === "hl" && selected.id === hl.id;
  const row = document.createElement("div");
  row.className = "hrow" + (active ? " active" : "") + (hl.author === "agent" ? " agent" : "");
  row.innerHTML =
    `<button class="x" title="discard">×</button>` +
    `<div class="top"><span class="num">#${n}</span>` +
    `<span class="chip ${st}">${chipLabel}</span>` +
    staleChip(hl) +
    `<span class="loc">${esc(loc)}</span></div>` +
    `<div class="prev">${esc(prev)}</div>`;
  // the index is the always-visible surface, so an escalation still waiting shows a live cue here
  // too — otherwise the reviewer has to open the panel to learn whether anything is happening
  if (!buf && st === "context" && !card && hl.context_requested) {
    const p = row.querySelector(".prev");
    p.textContent = "";
    p.appendChild(agentWaitLine(hl.context_requested_at || hl.created_at, true));
  }
  const mark = owedMarker({ kind: "hl", id: hl.id });
  if (mark) row.querySelector(".top").appendChild(mark);
  row.onclick = () => openSubject({ kind: "hl", id: hl.id });
  row.querySelector(".x").onclick = (e) => { e.stopPropagation(); post({ type: "remove_highlight", highlight_id: hl.id }); };
  return row;
}

function insightRow(c) {
  const active = selected && selected.kind === "insight" && selected.id === c.id;
  const row = document.createElement("div");
  row.className = "hrow agent" + (active ? " active" : "");
  const l = c.label;
  // a corrected label reads differently from one nobody questioned — it is the reviewer's word now
  const mine = l && l.by === "browser";
  const label = l
    ? `<span class="chip crit ${esc(l.criticality)}" title="${mine ? "you set this" : "Claude's assessment"}">` +
      `${esc(l.theme)} · ${esc(l.criticality)}${mine ? " ✓" : ""}</span>`
    : "";
  row.innerHTML =
    `<button class="x" title="dismiss">×</button>` +
    `<div class="top"><span class="chip insight">MR-level</span>${label}${staleChip(c)}</div>` +
    (l && l.about ? `<div class="about">${esc(l.about)}</div>` : "") +
    `<div class="prev">${esc(firstLine(c.body))}</div>`;
  const mark = owedMarker({ kind: "insight", id: c.id });
  if (mark) row.querySelector(".top").appendChild(mark);
  row.onclick = () => openSubject({ kind: "insight", id: c.id });
  row.querySelector(".x").onclick = (e) => { e.stopPropagation(); post({ type: "remove_card", card_id: c.id }); };
  return row;
}

// the reviewer's MR-level comment — a single pinned row that opens the same detail editor
function renderMrRow(el) {
  const d = mrDraft();
  const buf = (MR_KEY in draftBuffers) ? draftBuffers[MR_KEY] : (d ? d.body : "");
  const posted = d && d.status === "posted";
  const active = selected && selected.kind === "mr";
  const chip = posted ? `<span class="chip posted">✓ posted</span>`
             : d ? `<span class="chip comment">comment</span>`
             : `<span class="chip">MR-level</span>`;
  const prev = buf ? firstLine(buf) : "⊕ write a review summary";
  const row = document.createElement("div");
  row.className = "hrow mr" + (active ? " active" : "");
  row.innerHTML =
    (d && !posted ? `<button class="x" title="discard">×</button>` : "") +
    `<div class="top">${chip}<span class="loc">whole MR</span></div>` +
    `<div class="prev">${esc(prev)}</div>`;
  const mark = owedMarker({ kind: "mr" });
  if (mark) row.querySelector(".top").appendChild(mark);
  row.onclick = () => openSubject({ kind: "mr" });
  if (d && !posted) row.querySelector(".x").onclick = (e) => {
    e.stopPropagation(); delete draftBuffers[MR_KEY]; post({ type: "remove_draft", highlight_id: null });
  };
  el.appendChild(row);
}

function mrDraft() { return state.drafts.find((d) => !d.highlight_id); }  // the MR-level comment, if any

// The non-blocking detail overlay: one subject, and the two channels it is discussed in. They are
// never one list — the Claude channel is a session command that only the reviewer sees, the review
// channel is written back to the host for everyone — so composing them as tabs is what keeps an
// internal message from becoming a posted one by landing in the wrong box.
function renderDetail() {
  const el = $("detail");
  const close = () => openSubject(null);
  const subject = detailSubject();
  if (!subject) {
    if (selected) { clearSubject(); if (SID) watchScopes(diffScopes()); }
    el.hidden = true; el.innerHTML = "";
    return;
  }
  el.hidden = false; el.innerHTML = "";
  el.classList.toggle("max", detailMax);
  el.classList.toggle("reading", detailReading);
  const tab = detailTab || defaultDetailTab(subject);
  const body = document.createElement("div");
  body.className = "dbody";
  el.appendChild(body);
  body.appendChild(detailHead(subject, close));
  body.appendChild(detailTabs(subject, tab));
  body.appendChild(tab === "host" ? hostChannel(subject) : claudeChannel(subject));
  restoreDetailFocus(el, subject, tab);
}

// resolve the selection against live state: a row can vanish under the panel — a removed highlight,
// a thread the host dropped on re-sync — and the panel closes rather than render a ghost
function detailSubject() {
  if (!selected) return null;
  if (selected.kind === "mr") return { kind: "mr" };
  if (selected.kind === "insight") {
    const card = annotationInsights().find((x) => x.id === selected.id);
    return card ? { kind: "insight", card } : null;
  }
  if (selected.kind === "thread") {
    const thread = threadById(selected.id);
    return thread ? { kind: "thread", thread } : null;
  }
  const hl = annotationHighlight(selected.id);
  return hl ? { kind: "hl", hl } : null;
}

// A thread came from the MR, so it opens where it already lives — unless something has been asked
// about it here. Everything else opens on Claude, which is where its card is.
function defaultDetailTab(subject) {
  return subject.kind === "thread" && !conversationMessages(selected).length ? "host" : "claude";
}

function detailHead(subject, close) {
  const head = document.createElement("div");
  head.className = "dhead";
  if (subject.kind === "mr") {
    head.innerHTML = `<span class="chip">whole MR</span><span class="dlabel">the change itself</span>`;
  } else if (subject.kind === "insight") {
    head.innerHTML = `<span class="byclaude">Claude's insight</span>`;
  } else if (subject.kind === "thread") {
    const t = subject.thread;
    const loc = t.anchor && t.anchor.file
      ? `${t.anchor.file}${t.anchor.line ? ":" + t.anchor.line : ""}` : "whole MR";
    head.innerHTML = (t.resolved ? `<span class="chip posted">✓ resolved</span>`
                                 : `<span class="chip comment">open</span>`) +
      `<span class="dlabel">${esc(loc)}</span>`;
    // the location jumps to the code here as it does on the row this was opened from — the panel is
    // where the reviewer reads the thread, so it's where they reach for the link
    if (t.anchor && t.anchor.file) {
      const locEl = head.querySelector(".dlabel");
      locEl.classList.add("jumpcode"); locEl.title = "jump to this line in the diff";
      locEl.onclick = () => jumpToCode(t.anchor.file, t.anchor.line);
    }
  } else {
    const hl = subject.hl;
    const loc = `${hl.file}:${hl.start}${hl.end !== hl.start ? "-" + hl.end : ""}`;
    head.innerHTML = `<span class="num">#${hl.n}</span>` +
      (hl.author === "agent" ? `<span class="byclaude">Claude flagged</span>` : "") +
      `<span class="loc" title="jump to code">${esc(loc)}</span>`;
    head.querySelector(".loc").onclick = () => goToHighlight(hl);
  }
  // A long chat is the reason to ask for the whole window, so full view opens edge to edge
  // and the measure is the opt-in — the other way round reads as a panel that refused to grow.
  if (detailMax) {
    head.appendChild(btn(detailReading ? "↔ Full width" : "↔ Reading width", "dmax",
                         () => { detailReading = !detailReading; renderDetail(); }));
  }
  head.appendChild(btn(detailMax ? "⤡ Fit" : "⤢ Full view", "dmax", () => {
    detailMax = !detailMax;
    renderDetail();
  }));
  head.appendChild(btn("×", "dclose", close));
  return head;
}

function detailTabs(subject, active) {
  const wrap = document.createElement("div");
  wrap.className = "tabs";
  wrap.setAttribute("role", "tablist");
  const counts = { claude: conversationMessages(selected).length, host: hostCount(subject) };
  [["claude", "Claude", ""], ["host", "Review", " host"]].forEach(([key, label, extra]) => {
    const t = document.createElement("button");
    t.type = "button";
    t.className = "tab" + extra + (active === key ? " on" : "");
    t.setAttribute("role", "tab");
    t.setAttribute("aria-selected", active === key ? "true" : "false");
    t.textContent = label;
    if (counts[key]) {
      const n = document.createElement("span");
      n.className = "cnt"; n.textContent = counts[key];
      t.appendChild(n);
    }
    t.onclick = () => { detailTab = key; renderDetail(); };
    wrap.appendChild(t);
  });
  return wrap;
}

// the reviewer's own draft for this subject, if it takes one at all
function subjectDraft(subject) {
  if (subject.kind === "mr") return mrDraft();
  if (subject.kind === "hl") return state.drafts.find((d) => d.highlight_id === subject.hl.id);
  return null;
}

// the thread this subject has on the MR — its own, or the one its posted comment became
function hostThread(subject) {
  if (subject.kind === "thread") return subject.thread;
  const draft = subjectDraft(subject);
  if (!(draft && draft.status === "posted" && draft.thread_id)) return null;
  return threadById(draft.thread_id);
}

function hostCount(subject) {
  const thread = hostThread(subject);
  return thread ? (thread.comments || []).length : 0;
}

// --- the Claude channel: the card, and the chat beneath it -----------------

function claudeChannel(subject) {
  const frag = document.createDocumentFragment();
  if (subject.kind === "hl") {
    const hl = subject.hl;
    const posted = hl.comment_state === "posted";
    if (hl.question) {
      const q = document.createElement("div");
      q.className = "q"; q.textContent = hl.question;
      frag.appendChild(q);
    }
    // the deterministic host context — shown by default, no agent (D21). Both it and the card drop
    // once the comment is posted: they were context for writing it, and it is written.
    if (!posted) {
      frag.appendChild(hostContextBlock(hl));
      if (hl.card) {
        frag.appendChild(cardBlock(hl.card));
      } else if (hl.context_requested) {
        frag.appendChild(agentWaitLine(hl.context_requested_at || hl.created_at));
      } else {
        frag.appendChild(askContextControl(hl));
      }
    }
  } else if (subject.kind === "insight") {
    frag.appendChild(cardBlock(subject.card));
  }
  frag.appendChild(conversationBlock(subject));
  return frag;
}

// Doubting something that was said, and asking Claude to verify it. The claim travels as a note
// because a message is not a subject the protocol knows — the doubt is recorded against the
// subject the claim was made about, which is also where the answer will appear.
function doubtControl(claim, label) {
  return btn(label || "double-check", "btn ghost", () => post({
    type: "request_check", subject: subjectAnchor(selected), note: claim || "",
  }));
}

// Whether Claude owes this chat a verification. Server-side fact, same list the agent
// works from, so what is shown waiting and what is actually owed cannot disagree.
function beingChecked(sel) {
  const view = scopeViews[chatScope(sel)];
  return !!(view && view.state === "ready" && view.checking);
}

// An answer Claude gave, with the means to doubt it. The control sits on the claim rather than in
// the header because that is what is being doubted — and Claude's own words are the ones a reviewer
// most often wants a second pass over.
function cardBlock(card) {
  const wrap = document.createElement("div");
  const c = document.createElement("div");
  c.className = "card md"; c.innerHTML = md(card.body);
  wrap.appendChild(c);
  const acts = document.createElement("div"); acts.className = "noteacts";
  acts.appendChild(doubtControl(card.body, "double-check this"));
  wrap.appendChild(acts);
  wrap.appendChild(labelControl(card));
  return wrap;
}

const THEMES = ["bug", "security", "performance", "test", "docs", "style", "naming", "complexity"];
const CRITICALITIES = ["low", "medium", "high"];

// Disagreeing with how Claude classified a finding, without losing the finding. A label that reads
// `bug · high` on something that is a naming preference costs the reviewer attention every time
// they scan the list, and dismissing the card to be rid of the label throws away the content too.
// What the reviewer has picked but the server has not confirmed yet, per card. A label is two
// choices sent as one command, so changing the second reads the first off the page — and a frame
// arriving in between rebuilds these controls from the stored label, quietly putting the first
// choice back to what it was. Held here for the same reason draft prose is: a re-render must not
// discard what someone has just said.
const labelChoice = {};

function labelControl(card) {
  const row = document.createElement("div");
  row.className = "labelrow";
  const l = card.label || {};
  const pending = labelChoice[card.id] || {};
  if (pending.theme === l.theme && pending.criticality === l.criticality) {
    delete labelChoice[card.id];               // the server caught up; stop second-guessing it
  }
  const shown = { ...l, ...(labelChoice[card.id] || {}) };
  const pick = (name, values, current) => {
    const sel = document.createElement("select");
    sel.className = "labelpick"; sel.setAttribute("aria-label", name);
    if (!current) sel.appendChild(new Option("—", ""));
    values.forEach((v) => sel.appendChild(new Option(v, v, false, v === current)));
    return sel;
  };
  const theme = pick("theme", THEMES, shown.theme);
  const crit = pick("criticality", CRITICALITIES, shown.criticality);
  const send = () => {
    labelChoice[card.id] = { theme: theme.value, criticality: crit.value };
    if (!theme.value || !crit.value) return;   // half a label is not one
    post({ type: "label_card", card_id: card.id,
           label: { theme: theme.value, criticality: crit.value, about: l.about || "" } });
  };
  theme.onchange = send;
  crit.onchange = send;
  row.appendChild(theme);
  row.appendChild(crit);
  if (l.by === "browser") {
    const note = document.createElement("span");
    note.className = "labelnote"; note.textContent = "your label";
    row.appendChild(note);
  }
  return row;
}

// one subject's chat with Claude: the messages, and the box that adds to them
function conversationBlock(subject) {
  const scope = chatScope(selected);
  const wrap = document.createElement("div");
  wrap.className = "conv";

  const head = document.createElement("div");
  head.className = "chathdr";
  const label = document.createElement("div");
  label.className = "convhdr";
  label.textContent = subject.kind === "mr" ? "about the change as a whole" : "about this";
  head.appendChild(label);
  const messages = conversationMessages(selected);
  if (messages.length) {
    head.appendChild(btn("clear", "btn ghost", () => {
      if (confirm("Clear this chat?")) post({ type: "clear_chat", anchor: subjectAnchor(selected) });
    }));
  }
  wrap.appendChild(head);

  const msgs = document.createElement("div");
  msgs.className = "msgs";
  if (!messages.length) msgs.appendChild(empty("nothing asked here yet"));
  messages.forEach((m) => {
    const d = document.createElement("div");
    d.className = "msg " + (m.role === "user" ? "user" : "agent");
    d.innerHTML = `<div class="who">${esc(m.role)}</div><div class="md">${md(m.body)}</div>`;
    if (subject.kind !== "mr") {     // a doubt needs a subject to be recorded against
      const acts = document.createElement("div"); acts.className = "noteacts";
      acts.appendChild(doubtControl(m.body));
      d.appendChild(acts);
    }
    msgs.appendChild(d);
  });
  // your turn is still unanswered — say whether it's being worked on or nothing picked it up
  const last = messages[messages.length - 1];
  if (beingChecked(selected)) {
    const line = document.createElement("div");
    line.className = "checkwait";
    line.textContent = "Claude is double-checking this";
    msgs.appendChild(line);
  } else if (last && last.role === "user") msgs.appendChild(agentWaitLine(last.created_at));
  wrap.appendChild(msgs);

  const box = document.createElement("div");
  box.className = "chatbox";
  const inp = document.createElement("input");
  inp.placeholder = subject.kind === "mr"
    ? "message Claude about the change — reference a card by #N"
    : "ask Claude about this";
  inp.value = msgDraft[scope] || "";
  inp.oninput = (e) => { msgDraft[scope] = e.target.value; };
  inp.onfocus = () => { msgFocused = scope; };
  inp.onblur = () => { if (msgFocused === scope) msgFocused = null; };
  const send = () => {
    const body = inp.value.trim();
    if (!body) return;
    post({ type: "post_message", body, anchor: subjectAnchor(selected) });
    delete msgDraft[scope]; inp.value = "";
  };
  inp.onkeydown = (e) => { if (e.key === "Enter") send(); };
  box.appendChild(inp);
  box.appendChild(btn("Send", "btn", send));
  wrap.appendChild(box);

  const note = document.createElement("div");
  note.className = "channelnote";
  note.textContent = "Only you see this. Nothing here reaches the MR.";
  wrap.appendChild(note);

  msgs.scrollTop = msgs.scrollHeight;
  return wrap;
}

// --- the review channel: what everyone on the merge request sees -------------

function hostChannel(subject) {
  const frag = document.createDocumentFragment();
  const thread = hostThread(subject);
  if (thread) {
    if (subject.kind !== "thread") {
      const lbl = document.createElement("div");
      lbl.className = "yourthread"; lbl.textContent = "your comment";
      frag.appendChild(lbl);
    }
    frag.appendChild(threadConversationBlock(thread));
    return frag;
  }
  if (subject.kind === "insight") {
    frag.appendChild(empty("an insight is Claude's, not the MR's — nothing here is posted"));
    return frag;
  }
  const key = subject.kind === "mr" ? MR_KEY : subject.hl.id;
  frag.appendChild(draftEditor(key, subject.kind === "mr" ? null : subject.hl.id,
                               subjectDraft(subject)));
  const note = document.createElement("div");
  note.className = "channelnote host";
  note.textContent = "Everyone on the merge request sees this once you submit.";
  frag.appendChild(note);
  return frag;
}

// restore focus across a WS-driven re-render; never steal it
function restoreDetailFocus(el, subject, tab) {
  if (tab === "claude") {
    if (msgFocused === chatScope(selected)) {
      const inp = el.querySelector(".chatbox input");
      if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
    }
    if (subject.kind === "hl" && askFocused === subject.hl.id) {
      const ai = el.querySelector("input.askinp");
      if (ai) { ai.focus(); ai.setSelectionRange(ai.value.length, ai.value.length); }
    }
    return;
  }
  const key = subject.kind === "mr" ? MR_KEY : subject.kind === "hl" ? subject.hl.id : null;
  if (key !== null && focusedDraft === key) {
    const ta = el.querySelector("textarea.draftbox");
    if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
  }
}

// a reviewer's review-comment draft (their words; the card is never posted). `anchor` is the
// highlight id, or null for the MR-level comment; `key` keys the local buffer + focus tracking.
function draftEditor(key, anchor, draft) {
  const wrap = document.createElement("div");
  wrap.className = "draft";
  if (draft && draft.status === "posted") {
    wrap.innerHTML = `<div class="posted">✓ posted${
      draft.url ? ` · <a href="${esc(draft.url)}" target="_blank" rel="noopener">view</a>` : ""}</div>`;
    return wrap;
  }
  const ta = document.createElement("textarea");
  ta.className = "draftbox";
  ta.placeholder = anchor === null
    ? "write an MR-level review comment — a summary posted as a general note on the MR"
    : "prepare a review comment — your words (Claude's card is context, not posted)";
  ta.value = (key in draftBuffers) ? draftBuffers[key] : (draft ? draft.body : "");
  ta.oninput = (e) => { draftBuffers[key] = e.target.value; };
  ta.onfocus = () => { focusedDraft = key; };
  ta.onblur = () => { if (focusedDraft === key) focusedDraft = null; };
  wrap.appendChild(ta);

  // an optional suggested change (line-anchored only) — coexists with the prose above
  const canSuggest = anchor !== null && (!state.mr || (state.mr.capabilities || {}).suggestions !== false);
  const sugActive = canSuggest && (suggOpen[key] || (draft && draft.suggestion != null));
  let sta = null;
  if (sugActive) {
    const hl = annotationHighlight(anchor);
    const seed = (draft && draft.suggestion != null) ? draft.suggestion
               : (hl ? newSideLines(hl.file, hl.start, hl.end) : "");
    if (!(key in suggBuf)) suggBuf[key] = seed;
    const lbl = document.createElement("div"); lbl.className = "suglbl"; lbl.textContent = "suggested change — edit the lines";
    sta = document.createElement("textarea");
    sta.className = "draftbox suggbox"; sta.spellcheck = false;
    sta.value = suggBuf[key];
    sta.oninput = (e) => { suggBuf[key] = e.target.value; };
    wrap.appendChild(lbl); wrap.appendChild(sta);
  }

  const row = document.createElement("div");
  row.className = "draftbtns";
  row.appendChild(btn(draft ? "Update" : "Save", "btn", () => {
    const body = ta.value.trim();
    const suggestion = sugActive ? (suggBuf[key] != null ? suggBuf[key] : "") : null;
    if (!body && !(suggestion && suggestion.trim())) return;   // need prose or a suggestion
    post({ type: "save_draft", highlight_id: anchor, body, suggestion: suggestion });
    delete draftBuffers[key]; delete suggBuf[key]; suggOpen[key] = false;
  }));
  if (canSuggest) row.appendChild(btn(sugActive ? "Drop suggestion" : "＋ Suggest a change", "btn ghost", () => {
    if (sugActive) { suggOpen[key] = false; delete suggBuf[key]; }
    else { suggOpen[key] = true; }
    renderAnnotations();
  }));
  if (draft) row.appendChild(btn("Remove", "btn ghost", () => {
    post({ type: "remove_draft", highlight_id: anchor });
    delete draftBuffers[key]; delete suggBuf[key]; suggOpen[key] = false;
  }));
  wrap.appendChild(row);
  return wrap;
}

// existing MR discussions (host threads) — list, filter, and open a discussion in the overlay
function renderThreads(el) {
  // a posted draft is already shown inline under its highlight ("your comment"); don't also list
  // its thread here, or the reviewer's own comments double up once refresh re-mirrors them
  const ownPosted = new Set((state.drafts || [])
    .filter((d) => d.status === "posted" && d.thread_id)
    .map((d) => d.thread_id));
  const threads = allThreads().filter((t) => !ownPosted.has(t.id));
  const head = document.createElement("div");
  head.className = "chathdr";
  head.appendChild(h3("Discussions"));
  head.appendChild(btn("↻ refresh", "btn ghost", refreshThreads));
  el.appendChild(head);
  if (!threads.length) {
    el.appendChild(empty(local() ? "nobody else is reading this yet"
                                 : "no discussions on this MR"));
    return;
  }

  const seg = document.createElement("div");
  // named apart from the index's own filter: both sit in the same scroller and read alike
  seg.className = "seg threadseg";
  const unresolved = threads.filter((t) => !t.resolved).length;
  [["unresolved", `Unresolved ${unresolved}`], ["all", `All ${threads.length}`]].forEach(([k, label]) => {
    seg.appendChild(btn(label, "btn" + (threadFilter === k ? " on" : ""),
      () => { threadFilter = k; renderAnnotations(); }));
  });
  el.appendChild(seg);

  const shown = threadFilter === "all" ? threads : threads.filter((t) => !t.resolved);
  if (!shown.length) { el.appendChild(empty("nothing unresolved — all threads addressed")); return; }
  shown.forEach((t) => el.appendChild(threadRow(t)));
}

function threadRow(t) {
  const active = selected && selected.kind === "thread" && selected.id === t.id;
  const loc = t.anchor && t.anchor.file
    ? `${t.anchor.file}${t.anchor.line ? ":" + t.anchor.line : ""}` : "whole MR";
  const first = t.comments && t.comments.length ? t.comments[0] : null;
  const prev = first ? `${first.author}: ${firstLine(first.body)}` : "(empty)";
  const chip = t.resolved ? `<span class="chip posted">✓ resolved</span>`
             : `<span class="chip comment">open</span>`;
  const row = document.createElement("div");
  row.className = "hrow" + (active ? " active" : "") + (t.resolved ? " resolved" : "");
  row.innerHTML =
    `<div class="top">${chip}<span class="loc">${esc(loc)}</span>` +
    (t.comments && t.comments.length > 1 ? `<span class="num">${t.comments.length}</span>` : "") +
    `</div><div class="prev">${esc(prev)}</div>`;
  const mark = owedMarker({ kind: "thread", id: t.id });
  const top = row.querySelector(".top");
  if (mark && top) top.appendChild(mark);
  row.onclick = () => openSubject({ kind: "thread", id: t.id });
  if (t.anchor && t.anchor.file) {   // the location links to the code — jump the diff there + flash
    const locEl = row.querySelector(".loc");
    locEl.classList.add("jumpcode"); locEl.title = "jump to this line in the diff";
    locEl.onclick = (e) => { e.stopPropagation(); jumpToCode(t.anchor.file, t.anchor.line); };
  }
  const m = matchingHighlight(t);   // also overlaps one of your highlights → offer a jump to it
  if (m) {
    const jump = btn(`→ #${m.n}`, "btn ghost jump", (e) => {
      e.stopPropagation(); openSubject({ kind: "hl", id: m.hl.id });
    });
    row.querySelector(".top").appendChild(jump);
  }
  return row;
}

function matchingHighlight(t) {
  if (!t.anchor || !t.anchor.file) return null;
  const line = t.anchor.line;
  const hl = annotationHighlights().find((h) => h.file === t.anchor.file && line != null &&
    line >= h.start && line <= h.end);
  return hl ? { hl, n: hl.n } : null;
}

// Every thread verb is the same command shape and the same report. None of them reloads the
// session afterwards: the server republishes the discussions it changed, so the panel repaints
// from the scope rather than from whatever this guessed the host would say.
async function threadCmd(name, args, okMsg) {
  setStatus("…");
  const result = await cmd(name, { session: SID, ...args });
  setStatus(result.ok ? (okMsg || "") : "✕ " + result.reason);
  return result.ok;
}

async function refreshThreads() {
  // a full re-read of the change, not just its discussions — an updated head is what lets the
  // "Since last review" banner appear at all. The session fetch still carries the file list, so
  // that one is reloaded here until it has a scope of its own.
  if (await threadCmd("session.resync", {}, "re-synced with host")) await load();
}

async function replyThread(tid) {
  const body = (threadReplyBuf[tid] || "").trim();
  if (!body) return;
  if (await threadCmd("thread.reply", { thread: tid, body }, "reply posted")) {
    delete threadReplyBuf[tid]; renderAnnotations();
  }
}

async function resolveThread(tid, resolved) {
  await threadCmd("thread.resolve", { thread: tid, resolved },
                  resolved ? "resolved" : "reopened");
}

async function submitNoteEdit(tid, nid) {
  const body = (noteEdit[nid] || "").trim();
  if (!body) return;
  if (await threadCmd("thread.edit_note", { thread: tid, note: nid, body }, "edited")) {
    delete noteEdit[nid]; renderAnnotations();
  }
}

async function deleteNote(tid, nid) {
  if (!confirm("Delete this comment?")) return;
  await threadCmd("thread.delete_note", { thread: tid, note: nid }, "deleted");
}

// the discussion for a thread — notes (edit/delete on your own) + reply + resolve.
// Reused by the thread detail overlay and inline on a highlight whose comment became this thread.
function threadConversationBlock(t) {
  const canThreads = !state.mr || (state.mr.capabilities || {}).threads !== false;
  const wrap = document.createElement("div");
  const conv = document.createElement("div");
  conv.className = "msgs";
  (t.comments || []).forEach((c) => {
    const d = document.createElement("div");
    d.className = "msg agent";
    if (noteEdit[c.id] !== undefined) {           // this note is being edited in place
      const ta = document.createElement("textarea");
      ta.className = "draftbox"; ta.value = noteEdit[c.id];
      ta.oninput = (e) => { noteEdit[c.id] = e.target.value; };
      const row = document.createElement("div"); row.className = "draftbtns";
      row.appendChild(btn("Save", "btn", () => submitNoteEdit(t.id, c.id)));
      row.appendChild(btn("Cancel", "btn ghost", () => { delete noteEdit[c.id]; renderAnnotations(); }));
      d.innerHTML = `<div class="who">${esc(c.author)}</div>`;
      d.appendChild(ta); d.appendChild(row);
    } else {
      d.innerHTML = `<div class="who">${esc(c.author)}</div><div class="md">${md(c.body)}</div>`;
      if (canThreads && c.mine) {    // your own note → edit / delete
        const acts = document.createElement("div"); acts.className = "noteacts";
        acts.appendChild(btn("edit", "btn ghost", () => { noteEdit[c.id] = c.body; renderAnnotations(); }));
        acts.appendChild(btn("delete", "btn ghost", () => deleteNote(t.id, c.id)));
        d.appendChild(acts);
      }
    }
    conv.appendChild(d);
  });
  wrap.appendChild(conv);

  if (canThreads) {
    const rwrap = document.createElement("div");
    rwrap.className = "draft";
    const ta = document.createElement("textarea");
    ta.className = "draftbox"; ta.placeholder = "reply to this thread…";
    ta.value = threadReplyBuf[t.id] || "";
    ta.oninput = (e) => { threadReplyBuf[t.id] = e.target.value; };
    ta.onfocus = () => { threadReplyFocused = t.id; };
    ta.onblur = () => { if (threadReplyFocused === t.id) threadReplyFocused = null; };
    const row = document.createElement("div");
    row.className = "draftbtns";
    row.appendChild(btn("Reply", "btn", () => replyThread(t.id)));
    if (t.anchor)  // only diff-anchored discussions are resolvable
      row.appendChild(btn(t.resolved ? "Reopen" : "Resolve", "btn ghost",
        () => resolveThread(t.id, !t.resolved)));
    rwrap.appendChild(ta); rwrap.appendChild(row);
    wrap.appendChild(rwrap);
    if (threadReplyFocused === t.id) setTimeout(() => {
      const el2 = rwrap.querySelector("textarea");
      if (el2) { el2.focus(); el2.setSelectionRange(el2.value.length, el2.value.length); }
    }, 0);
  }
  return wrap;
}

function renderVersionBanner(el) {
  const version = reviewVersion();
  if (!version) return;
  const cap = state.mr && (state.mr.capabilities || {}).diff_versions === true;
  if (!cap) return;
  if (version.behind) {
    // the MR advanced past the reviewed watermark — offer the interdiff + advance the watermark
    const bar = document.createElement("div");
    bar.className = "verbanner";
    const lbl = document.createElement("span"); lbl.className = "vblabel";
    lbl.textContent = "Updated since your last review";
    bar.appendChild(lbl);
    const controls = document.createElement("div"); controls.className = "vbctl";
    const toggle = btn(sinceLast ? "Full diff" : "Since last review",
                       "btn ghost" + (sinceLast ? " on" : ""), toggleSinceLast);
    controls.appendChild(toggle);
    controls.appendChild(btn("Mark reviewed", "btn ghost", markReviewed));
    bar.appendChild(controls);
    el.appendChild(bar);
  } else if (!version.watermark) {
    // no baseline yet → let the reviewer set one, so incremental review can engage on later pushes
    // (this is the only entry point to the *first* watermark; submitting a review also sets it)
    const bar = document.createElement("div");
    bar.className = "verbanner baseline";
    const lbl = document.createElement("span"); lbl.className = "vblabel";
    lbl.textContent = "Set a baseline for incremental review";
    bar.appendChild(lbl);
    const controls = document.createElement("div"); controls.className = "vbctl";
    const b = btn("Mark reviewed up to here", "btn ghost", markReviewed);
    b.title = "record the current version as reviewed — then \"Since last review\" shows only later changes";
    controls.appendChild(b);
    bar.appendChild(controls);
    el.appendChild(bar);
  }
  // else: caught up (watermark == head) — nothing to show
}

function renderReviewBar(el) {
  const review = reviewView();
  if (!review) return;                      // nothing to say until the scope arrives
  const approval = reviewApproval();
  const { pending, posted } = review;
  const canApprove = (review.approval || {}).available;
  // the bar is worth showing when there is something to submit or an approval to give
  if (!pending && !posted && !canApprove) return;

  const alreadyApproved = !!(approval && approval.you_approved);
  const bar = document.createElement("div");
  bar.className = "reviewbar";
  const lbl = document.createElement("span");
  lbl.textContent = `Your review · ${pending} pending${posted ? ` · ${posted} posted` : ""}`;
  bar.appendChild(lbl);
  if (alreadyApproved) {   // your prior review approved this MR — a right-aligned status marker
    const ap = document.createElement("span");
    ap.className = "chip posted you-approved"; ap.textContent = "✓ you approved";
    bar.appendChild(ap);
  } else if (canApprove) {   // offer to approve only while you haven't (re-approving is a no-op)
    const tog = document.createElement("label");
    tog.className = "approve-tog";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = approveToggle;
    cb.onchange = (e) => { approveToggle = e.target.checked; };
    tog.appendChild(cb);
    tog.appendChild(document.createTextNode(" Approve MR"));
    bar.appendChild(tog);
  }
  // a submit button only when there's an action to take: drafts to post, an approve toggled on, or an
  // approval still available. Once approved with nothing pending, the bar is pure status (no dead button).
  if (pending || approveToggle || (canApprove && !alreadyApproved)) {
    const label = pending ? "Submit review" : (approveToggle ? "Approve MR" : "Submit review");
    const b = btn(label, "btn primary", submitReview);
    if (!pending && !approveToggle) b.disabled = true;
    bar.appendChild(b);
  }
  el.appendChild(bar);
}

async function submitReview() {
  setStatus(approveToggle ? "submitting review…" : "posting review…");
  const data = await cmd("review.submit", { session: SID, approve: approveToggle });
  if (!data.ok) { setStatus("✕ " + data.reason); return; }
  const failed = (data.results || []).filter((x) => !x.ok);
  const parts = [];
  if (data.total) parts.push(failed.length ? `posted ${data.posted}/${data.total} — ${failed.length} failed`
                                           : `posted ${data.posted} comment${data.posted === 1 ? "" : "s"}`);
  if (data.approved) parts.push("approved");
  else if (data.approve_error) parts.push("approve failed: " + data.approve_error);
  setStatus(parts.join(" · ") || "nothing to submit");
  approveToggle = false;
}

// escalate a highlight to the agent (D21) — separate from the review-comment box (D14):
// an optional "what do you want to know?" plus the explicit request button.
function askContextControl(hl) {
  const wrap = document.createElement("div");
  wrap.className = "askctx";
  const inp = document.createElement("input");
  inp.className = "askinp";
  inp.placeholder = "ask Claude something specific (optional)";
  inp.value = askBuf[hl.id] || "";
  inp.oninput = (e) => { askBuf[hl.id] = e.target.value; };
  inp.onfocus = () => { askFocused = hl.id; };
  inp.onblur = () => { if (askFocused === hl.id) askFocused = null; };
  inp.onkeydown = (e) => { if (e.key === "Enter") requestContext(hl); };
  wrap.appendChild(inp);
  wrap.appendChild(btn("✦ Ask Claude for context", "btn", () => requestContext(hl)));
  return wrap;
}

function requestContext(hl) {
  post({ type: "request_context", highlight_id: hl.id, question: (askBuf[hl.id] || "").trim() || null });
  delete askBuf[hl.id];
}

function hostContextBlock(hl) {
  const c = hl.context || {};
  const box = document.createElement("div");
  box.className = "hostctx";
  if (c.state === "loading" || c.state === "idle") { box.appendChild(empty("looking up context…")); return box; }
  if (c.state === "unavailable") { box.appendChild(empty("this host has no last-touch")); return box; }
  if (c.state === "error") { box.appendChild(empty("context unavailable: " + (c.error || ""))); return box; }
  const blame = (c.blame || []);
  const issues = (c.linked_issues || []);
  if (!blame.length && !issues.length) { box.appendChild(empty("no last-touch or linked issue")); return box; }
  if (blame.length) {
    const b = blame[0];
    const row = document.createElement("div");
    row.className = "ctxrow";
    row.innerHTML = `<span class="k">last touch</span> ${esc(b.author || "?")}` +
      (b.date ? ` · ${esc((b.date || "").slice(0, 10))}` : "") +
      (b.commit ? ` · <code>${esc(b.commit)}</code>` : "") +
      (b.summary ? `<div class="s">${esc(b.summary)}</div>` : "");
    box.appendChild(row);
  }
  issues.forEach((i) => {
    const row = document.createElement("div");
    row.className = "ctxrow";
    row.innerHTML = `<span class="k">closes</span> <a href="${esc(i.url)}" target="_blank" rel="noopener">#${i.iid} ${esc(i.title)}</a>`;
    box.appendChild(row);
  });
  return box;
}

function goToHighlight(hl) {
  if (currentFile !== hl.file) { currentFile = hl.file; render(); }
  const cell = $("diff").querySelector(`td.code[data-line="${hl.start}"]`);
  if (cell) cell.scrollIntoView({ block: "center", behavior: "smooth" });
}

function h2(t) { const e = document.createElement("h2"); e.textContent = t; return e; }
function h3(t) { const e = document.createElement("h3"); e.textContent = t; return e; }
function empty(t) { const e = document.createElement("div"); e.className = "empty"; e.textContent = t; return e; }
function btn(t, cls, fn) { const b = document.createElement("button"); b.className = cls; b.textContent = t; b.onclick = fn; return b; }

boot();
