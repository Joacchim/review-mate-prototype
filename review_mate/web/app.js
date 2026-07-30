"use strict";
// review-mate diff browser — a thin surface. Presents session state, captures highlights; never
// reasons. Source of truth is the bridge-server; we re-fetch the snapshot on every WS event.

let SID = null;
let state = null;
let currentFile = null;
const collapsedDirs = new Set();
let splitMode = localStorage.getItem("rm-split") === "1";
let chatDraft = "";
let chatFocused = false;
const draftBuffers = {};   // highlight_id -> in-progress review-comment text (survives re-render)
let focusedDraft = null;   // highlight_id of the focused draft textarea, to restore after render
let repoTree = null;                 // all repo paths (lazy-loaded when "show all" is on)
let showAll = localStorage.getItem("rm-showall") === "1";
let sessionStates = null;            // sid -> {state, mr_state, behind, unresolved, …} from the hub check
let sessionStatesAt = 0;             // when the states were last computed (ms), for the "checked … ago" note
let checkingStates = false;          // a hub "check for updates" fan-out is in flight
try {   // hydrate the last hub check so it survives a reload of the queue page
  const _st = JSON.parse(localStorage.getItem("rm-review-states") || "null");
  if (_st && _st.states) { sessionStates = _st.states; sessionStatesAt = _st.at || 0; }
} catch (e) { /* ignore a corrupt cache */ }
const fileContents = {};             // path -> content (cache for non-diff file views + unfold)
const expandedGaps = {};             // path -> Map of gap-start -> {top, bot, all} lines unfolded
const mdRendered = new Set();        // .md paths currently shown rendered (vs raw diff)
let viewingPath = null;              // a non-diff file currently shown (plain view)
let railFilter = "all";              // index filter: all | context | comment | posted
let railQuery = "";                  // index text search (file + comment + question)
let railSearchFocused = false;       // restore search focus after a WS-driven re-render
let selected = null;                 // {kind:"hl"|"insight"|"mr"|"thread", id} shown in the detail overlay
const MR_KEY = "__mr__";             // draftBuffers/focus key for the (anchorless) MR-level comment
let approveToggle = false;           // "Approve MR" checkbox on the submit bar
let threadFilter = "unresolved";     // discussions filter: unresolved | all
const threadReplyBuf = {};           // thread_id -> in-progress reply text (survives re-render)
let threadReplyFocused = null;       // thread_id of the focused reply textarea, to restore after render
const cheapCtx = {};                 // highlight_id -> {blame, linked_issues} | "loading" (D21 cheap tier)
const askBuf = {};                   // highlight_id -> in-progress "ask Claude" question text
let askFocused = null;               // highlight_id of the focused ask-context input, to restore after render
let me = null;                       // the reviewer's own host username (to mark "your" notes)
const suggBuf = {};                  // draft key -> in-progress suggested-change text
const suggOpen = {};                 // draft key -> whether the suggestion editor is open
const noteEdit = {};                 // note_id -> in-progress edit text (null/absent = not editing)
let reviewStatus = null;             // {behind, watermark, head} — diff-versions awareness
let approvalStatus = null;           // {available, you_approved, approved_by} — your approval state
let commitsMode = false;             // per-commit review: the diff pane shows one commit at a time
let commitList = null;               // [{sha, short_id, title, message, …}] oldest→newest, or null
let currentCommit = null;            // sha of the commit being reviewed
const commitFiles = {};              // sha -> that commit's files (shaped like state.files)
let sinceLast = false;               // showing the rebase-aware "since last review" interdiff
let sinceLastData = null;            // fetched {available, empty, mode, files|interdiff, error}
let sinceLastHead = null;            // the head sinceLastData was computed for (invalidate when it moves)
let sinceLastPrefetching = false;    // a background warm of the interdiff is in flight
let agentWatch = null;               // {attached, parked, last_seen} — is an agent on the activity stream?

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

// --- lightweight, self-contained syntax highlighting ------------------------
// A conservative per-line tokenizer: strings, line comments (style by file type), numbers, and a
// union keyword set. Escapes all text (XSS-safe); unknown file types are left plain. Per-line, so
// multi-line strings/comments only colour to end of line — an accepted trade for zero dependencies.
const SYNTAX_KW = new Set(("if else elif for while do switch case break continue return function " +
  "func def class struct enum interface type const let var val fn import from export default void " +
  "int float double bool boolean string char new delete try catch finally throw throws raise with " +
  "as in is not and or async await yield lambda pass None True False null nil true false undefined " +
  "this self super extends implements package namespace using module require public private " +
  "protected static").split(" "));

function langOf(path) {
  const ext = (path.split(".").pop() || "").toLowerCase();
  if (["js", "jsx", "ts", "tsx", "go", "rs", "c", "h", "cc", "cpp", "hpp", "java", "cs", "php", "swift", "kt", "scala"].includes(ext)) return { line: "//" };
  if (["py", "rb", "sh", "bash", "zsh", "yml", "yaml", "toml", "pl", "r"].includes(ext)) return { line: "#" };
  if (["sql", "lua", "hs"].includes(ext)) return { line: "--" };
  if (["css", "scss", "less", "json"].includes(ext)) return { line: null };
  return null;   // unknown → no highlighting (stay plain, never mis-colour)
}

function highlightCode(code, lang) {
  if (!lang) return esc(code);
  const cmtPat = lang.line ? lang.line.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ".*$" : "(?!)";
  const re = new RegExp("(" + cmtPat + ")|(\"(?:\\\\.|[^\"\\\\])*\"?|'(?:\\\\.|[^'\\\\])*'?|`(?:\\\\.|[^`\\\\])*`?)|(\\b\\d[\\w.]*)|([A-Za-z_$][\\w$]*)", "gm");
  let last = 0, m, out = "";
  while ((m = re.exec(code))) {
    if (m.index > last) out += esc(code.slice(last, m.index));
    if (m[1]) out += `<span class="tok-cmt">${esc(m[1])}</span>`;
    else if (m[2]) out += `<span class="tok-str">${esc(m[2])}</span>`;
    else if (m[3]) out += `<span class="tok-num">${esc(m[3])}</span>`;
    else if (m[4]) out += SYNTAX_KW.has(m[4]) ? `<span class="tok-kw">${esc(m[4])}</span>` : esc(m[4]);
    last = re.lastIndex;
    if (m.index === re.lastIndex) re.lastIndex++;   // guard against a zero-width match looping
  }
  out += esc(code.slice(last));
  return out;
}

// a diff content line: keep its +/-/space marker plain, highlight the code after it
function hlLine(raw, lang) { return esc(raw[0] || "") + highlightCode(raw.slice(1), lang); }

// --- boot -------------------------------------------------------------------

async function boot() {
  wireToolbar();
  startAgentWatch();   // the header light runs everywhere, queue page included
  const params = new URLSearchParams(location.search);
  SID = params.get("s");
  // ?ref=<project!iid> — what a queue entry links to, so it can be middle-clicked into its own tab.
  // Resolving it here (rather than on click) is what makes the entry a real link instead of a button.
  if (!SID && params.get("ref")) return openRef(params.get("ref"));
  if (!SID) return showLanding();
  $("sid").textContent = SID.slice(0, 8);
  try { me = (await fetch("/api/me").then((r) => r.json())).username; } catch (e) { me = null; }
  await load();          // paint fast from stored state
  connectWS();
  syncFromHost();        // then bring the session up to the live head, so an update the hub flagged
                         // actually surfaces here (banner + "Since last review"), not just on the hub
}

// one-time re-sync when opening a review: pulls the current head + diff + discussions from the host,
// so a branch that advanced since you last looked (the hub's "git update") engages the since-last
// feature in-session instead of the stored, stale head hiding it. Background — the page already painted.
async function syncFromHost() {
  try {
    const r = await fetch(`/api/sessions/${SID}/refresh-threads`, {
      method: "POST", headers: { "content-type": "application/json" }, body: "{}",
    });
    if (r.ok) await load();
  } catch (e) { /* keep the stored view; the ↻ button re-syncs on demand */ }
}

// the new-side content of a highlighted line range, pulled from the diff hunks (for suggestion pre-fill)
function newSideLines(path, lo, hi) {
  const file = state.files.find((f) => f.path === path);
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
// A request to the agent (a chat message, a context escalation) can take a while, and the reviewer
// has no way to tell a slow answer from a lost one. Two facts settle it, and the UI shows both
// continuously: what they're waiting on (derived here from the session snapshot — the server never
// tracks who owes what) and whether an agent is watching at all (/api/agent-status).

// everything the reviewer is currently waiting on Claude for, oldest first
function outstandingAsks() {
  if (!state) return [];
  const out = [];
  const last = state.messages && state.messages[state.messages.length - 1];
  if (last && last.role === "user") out.push({ kind: "message", since: last.created_at });
  (state.highlights || []).forEach((h) => {
    if (!h.context_requested) return;
    if ((state.cards || []).some((c) => c.highlight_id === h.id)) return;   // already answered
    out.push({ kind: "context", id: h.id, since: h.context_requested_at || h.created_at });
  });
  return out.sort((a, b) => (a.since || "").localeCompare(b.since || ""));
}

// working: something outstanding and an agent is watching. stalled: outstanding and nothing is —
// the case that used to be indistinguishable from "slow". watching/off: nothing outstanding.
function agentState() {
  const asks = outstandingAsks();
  const attached = !!(agentWatch && agentWatch.attached);
  if (asks.length) return { cls: attached ? "working" : "stalled", since: asks[0].since, asks };
  return { cls: attached ? "watching" : "off", since: null, asks };
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

// the header light: colour + pulse for the state, and a word only when it needs the reviewer's eye
function renderAgentLight() {
  const el = $("agent");
  if (!el) return;
  const st = agentState();
  el.className = "agent " + st.cls;
  el.title = AGENT_TITLE[st.cls];
  el.querySelector(".alab").textContent =
    st.cls === "working" ? `Claude · ${elapsed(st.since)}` :
    st.cls === "stalled" ? "no agent watching" : "";
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

function paintWaitLine(el) {
  const stalled = agentState().cls === "stalled";
  el.className = "agentwait " + (stalled ? "stalled" : "working") + (el.dataset.mini ? " mini" : "");
  const age = elapsed(el.dataset.since);
  el.querySelector(".awtext").textContent = el.dataset.mini
    ? (stalled ? `not picked up · ${age}` : `Claude working · ${age}`)
    : (stalled ? `no agent is watching — waiting ${age}, nothing has picked this up`
               : `Claude is working on it… ${age}`);
}

async function pollAgentStatus() {
  try { agentWatch = await fetch("/api/agent-status").then((r) => r.json()); }
  catch (e) { agentWatch = null; }
}

// One ticker drives both: the elapsed times refresh every second, the watcher fetch rides along —
// often while an answer is outstanding, rarely when none is (it's a cheap in-process read, but
// there's nothing to learn while the reviewer isn't waiting on anything).
function startAgentWatch() {
  let ticks = 0;
  const tick = async () => {
    const period = outstandingAsks().length ? 2 : 15;
    if (ticks % period === 0) await pollAgentStatus();
    ticks += 1;
    renderAgentLight();
    document.querySelectorAll(".agentwait").forEach(paintWaitLine);
  };
  tick();
  setInterval(tick, 1000);
}

// the queue page doubles as the session hub: resume or close an in-flight review
// the per-review state (from a hub "check for updates") → a chip label + a row class for colour
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
  if (checkingStates) return;
  checkingStates = true;
  setStatus("checking your open reviews…");
  try {
    sessionStates = await fetch("/api/sessions/status").then((r) => r.json());
    sessionStatesAt = Date.now();
    localStorage.setItem("rm-review-states", JSON.stringify({ at: sessionStatesAt, states: sessionStates }));
  } catch (e) { setStatus("✕ " + e); }
  finally { checkingStates = false; setStatus(""); showLanding(); }   // re-render with the states
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
  hd.appendChild(btn(checkingStates ? "checking…" : "↻ check for updates", "btn ghost", checkReviewStates));
  if (sessionStatesAt) {
    const note = document.createElement("span"); note.className = "hubago";
    note.textContent = `checked ${agoText(sessionStatesAt)}`;
    hd.appendChild(note);
  }
  land.appendChild(hd);
  const list = document.createElement("div");
  list.style.margin = "0 0 26px";
  sessions
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))
    .forEach((s) => {
      const loc = s.project ? `${esc(s.project)} !${s.iid}` : "(no MR loaded)";
      const bits = [];
      if (s.highlights) bits.push(`${s.highlights} highlight${s.highlights > 1 ? "s" : ""}`);
      if (s.cards) bits.push(`${s.cards} card${s.cards > 1 ? "s" : ""}`);
      if (s.drafts_pending) bits.push(`${s.drafts_pending} draft${s.drafts_pending > 1 ? "s" : ""}`);
      if (s.drafts_posted) bits.push(`${s.drafts_posted} posted`);
      const st = sessionStates && sessionStates[s.id];
      const meta = st && REVIEW_STATE[st.state] || null;
      if (meta && st.state === "discussions" && st.unresolved) meta.label = `${st.unresolved} open discussion${st.unresolved > 1 ? "s" : ""}`;
      const row = document.createElement("div");
      row.className = "sitem" + (meta ? " " + meta.cls : "");
      // the title is a real link to the review (stretched over the card, see .rowlink) and the
      // project!iid a real link to the MR on the host — both middle-clickable into their own tab
      row.innerHTML =
        `<button class="x" title="close review">×</button>` +
        `<div class="t">${meta ? `<span class="ststate">${esc(meta.label)}</span> ` : ""}` +
        `<a class="rowlink" href="?s=${encodeURIComponent(s.id)}">${esc(s.title || "(untitled review)")}</a></div>` +
        `<div class="m">${hostLink(s.url, loc)}${bits.length ? " · " + bits.join(" · ") : ""}</div>`;
      row.querySelector(".x").onclick = (e) => {
        e.preventDefault(); e.stopPropagation();
        closeSession(s.id, s.drafts_pending);
      };
      list.appendChild(row);
    });
  land.appendChild(list);
}

async function closeSession(id, pending) {
  if (pending && !confirm(`This review has ${pending} unsubmitted comment(s). Close it anyway?`)) return;
  try { await fetch(`/api/sessions/${id}`, { method: "DELETE" }); } catch (e) {}
  showLanding();  // refresh the hub
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
  let sessions = [];
  try { sessions = await fetch("/api/sessions").then((r) => r.json()); } catch (e) {}
  if (!Array.isArray(sessions)) return null;
  return sessions.find((s) => s.status === "active" && `${s.project}!${s.iid}` === ref) || null;
}

// "Track": flag a queue entry for review without leaving the queue. It starts the review session, so
// the MR moves up into "Open reviews" — where check-for-updates then watches it — and the reviewer
// carries on triaging the rest of the queue.
async function trackRef(ref, button) {
  button.disabled = true; button.textContent = "tracking…";
  setStatus("tracking " + ref + "…");
  const fail = (msg) => { setStatus("✕ " + msg); button.disabled = false; button.textContent = "Track"; };
  try {
    if (await findTracking(ref)) { setStatus(ref + " is already in your open reviews"); showLanding(); return; }
    const r = await fetch("/api/sessions", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ ref }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return fail(data.error || `track failed (${r.status})`);
    setStatus("tracking " + ref + " — it's in your open reviews");
    showLanding();   // the entry now belongs to "Open reviews", not the queue — re-render both
  } catch (e) { fail(String(e)); }
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
  try {
    const r = await fetch("/api/sessions", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ ref }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { setStatus("✕ " + (data.error || `load failed (${r.status})`)); return showLanding(); }
    location.replace(`?s=${encodeURIComponent(data.id)}`);   // replace: don't leave ?ref= in history
  } catch (e) { setStatus("✕ " + e); showLanding(); }
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
  $("files").innerHTML = ""; $("rail").innerHTML = "";
  const land = document.createElement("div");
  land.className = "land";
  const d = $("diff"); d.innerHTML = ""; d.appendChild(land);

  // open sessions first — local and fast; never block them behind the (possibly slow) host queue
  let sessions = [];
  try { sessions = await fetch("/api/sessions").then((r) => r.json()); } catch (e) {}
  if (!Array.isArray(sessions)) sessions = [];
  const active = sessions.filter((s) => s.status === "active");
  renderOpenSessions(land, active);

  const head = document.createElement("div");
  head.innerHTML = `<h2>Pick a merge request</h2>`;
  land.appendChild(head);
  const queueBox = document.createElement("div");
  queueBox.appendChild(empty("loading your review queue…"));
  land.appendChild(queueBox);

  let items = [];
  try { items = await fetch("/api/queue").then((r) => r.json()); } catch (e) {}
  if (items && items.error) items = [];
  renderQueue(queueBox, items, active);
}

function renderQueue(box, items, openSessions) {
  box.innerHTML = "";
  // drop MRs already open as reviews — they're listed under "Open reviews" above, not the queue
  const open = new Set((openSessions || []).map((s) => `${s.project}!${s.iid}`));
  items = items.filter((it) => !open.has(`${it.project}!${it.iid}`));
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
    const r = await fetch("/api/sessions", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ ref }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      setStatus("");
      // 400 = the reference didn't parse → fall back to flexible search suggestions.
      // Anything else (e.g. 502, a GitLab/auth failure) is a real error — surface it
      // instead of masking it as "no match".
      if (r.status === 400 && ref) renderSuggestions(ref);
      else setStatus("✕ " + (data.error || `load failed (${r.status})`));
      return;
    }
    location.search = `?s=${data.id}`;  // reload cleanly into the loaded session
  } catch (e) {
    setStatus("✕ " + e);
  } finally {
    btn.disabled = false; btn.textContent = "Load";
  }
}

async function load() {
  state = await fetch(`/api/sessions/${SID}`).then((r) => r.json());
  if (!currentFile && state.files.length) currentFile = state.files[0].path;
  try { reviewStatus = await fetch(`/api/sessions/${SID}/review-status`).then((r) => r.json()); }
  catch (e) { reviewStatus = null; }
  try { approvalStatus = await fetch(`/api/sessions/${SID}/approval-status`).then((r) => r.json()); }
  catch (e) { approvalStatus = null; }
  const head = reviewStatus ? reviewStatus.head : null;
  if (head !== sinceLastHead) { sinceLastData = null; sinceLastHead = head; }   // invalidate only when the head moved
  // warm the interdiff in the background once the MR is behind, so the first toggle is instant
  if (reviewStatus && reviewStatus.behind && sinceLastData === null && !sinceLastPrefetching) {
    sinceLastPrefetching = true;
    fetch(`/api/sessions/${SID}/since-last`).then((r) => r.json())
      .then((d) => { sinceLastData = d; })
      .catch(() => {})
      .finally(() => { sinceLastPrefetching = false; if (sinceLast) render(); });
  }
  render();
}

async function toggleSinceLast() {
  sinceLast = !sinceLast;
  viewingPath = null;   // both views are diff views — drop any non-diff repo file being shown
  if (sinceLast) { commitsMode = false; $("t-commits").classList.toggle("on", false); }  // one diff mode at a time
  render();   // swap to the panel now — it shows "computing…" while the (cold: clone + fetch) work runs
  // usually the background warm (in load) already has it; only fetch here if it didn't, and never
  // race that in-flight warm — its .finally re-renders when it lands
  if (sinceLast && sinceLastData === null && !sinceLastPrefetching) {
    try { sinceLastData = await fetch(`/api/sessions/${SID}/since-last`).then((r) => r.json()); }
    catch (e) { sinceLastData = { error: String(e) }; }
    render();
  }
}

// a highlight made against an earlier MR head — its lines may have moved since (diff-versions)
function isStale(hl) {
  return !!(hl.created_sha && state.mr && state.mr.sha && hl.created_sha !== state.mr.sha);
}

async function markReviewed() {
  setStatus("marking reviewed…");
  try {
    await fetch(`/api/sessions/${SID}/mark-reviewed`, { method: "POST" });
    setStatus("marked reviewed up to the current version");
    await load();
  } catch (e) { setStatus("✕ " + e); }
}

let wsTimer = null;
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
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

function wireToolbar() {
  $("t-left").onclick = () => $("shell").classList.toggle("hl");
  $("t-right").onclick = () => $("shell").classList.toggle("hr");
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
  list.appendChild(empty("searching GitLab…"));
  land.appendChild(list);
  // escape hatch (D20): only if the direct search misses, route the fuzzy query to the agent
  const fallback = document.createElement("div");
  fallback.className = "askrow";
  const claudePanel = document.createElement("div");
  claudePanel.className = "claudepanel";
  land.appendChild(fallback);
  land.appendChild(claudePanel);
  d.appendChild(land);
  if (!SID) { $("files").innerHTML = ""; $("rail").innerHTML = ""; }
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
    return;
  }
  const items = Array.isArray(data) ? data : [];
  items.forEach((it) => list.appendChild(mrItem(it)));
  if (!items.length) list.appendChild(empty("no GitLab matches — try another term, or paste a full MR URL"));
  // the agent is a fallback, not the default: prompt it more strongly when direct search came up empty
  fallback.appendChild(document.createTextNode(items.length ? "Not the one? " : "Looking for it by description? "));
  fallback.appendChild(btn("✦ Ask Claude to find it", "btn ghost", () => askClaude(query, claudePanel)));
}

// route a fuzzy query to Claude's lookup channel; render its answer + loadable candidates
async function askClaude(query, panel) {
  panel.innerHTML = "";
  // the lookup channel has no session to hang a wait line on — say it plainly instead
  panel.appendChild(empty(agentWatch && !agentWatch.attached
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
    if ($("ref").value.trim() !== query) return;  // reviewer moved on
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

function render() {
  if (state.mr) {
    const m = state.mr;
    // the project!iid path links back to the MR on the host, so reviewers can jump to the source
    const path = `${esc(m.project)} !${m.iid}`;
    const link = m.url ? `<a class="mrlink" href="${esc(m.url)}" target="_blank" rel="noopener">${path} ↗</a>` : path;
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
  renderRail();
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

async function loadRepoTree() {
  try {
    const t = await fetch(`/api/sessions/${SID}/repo-tree`).then((r) => r.json());
    repoTree = Array.isArray(t) ? t : [];
  } catch (e) { repoTree = []; }
}

function renderTree() {
  const el = $("files");
  el.innerHTML = "";
  const inCommits = commitsMode && commitList && currentCommit;
  const inSince = sinceFilesReady();
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
    hdr.querySelector("input").onchange = async (e) => {
      showAll = e.target.checked;
      localStorage.setItem("rm-showall", showAll ? "1" : "0");
      if (showAll && repoTree === null) { hdr.lastChild.textContent = " loading repo…"; await loadRepoTree(); }
      renderTree();
    };
    el.appendChild(hdr);
  }

  const files = activeFiles();
  const diffPaths = new Set(files.map((f) => f.path));
  const entries = files.map((f) => ({ path: f.path, old_path: f.old_path,
                                     change_type: f.change_type, diff: true }));
  if (!inSince && !inCommits && showAll && repoTree) {
    repoTree.forEach((p) => { if (!diffPaths.has(p)) entries.push({ path: p, diff: false }); });
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
    viewingPath = entry.path; render();
    if (!(entry.path in fileContents)) fetchFile(entry.path);
  }
}

async function fetchFile(path) {
  try {
    const d = await fetch(`/api/sessions/${SID}/file?path=${encodeURIComponent(path)}`).then((r) => r.json());
    fileContents[path] = (d && typeof d.content === "string") ? d.content : `(could not load: ${d.error || "error"})`;
  } catch (e) { fileContents[path] = "(failed to load)"; }
  if (viewingPath === path) renderDiff();
}

// --- diff -------------------------------------------------------------------

function highlightLines(path) {
  const set = new Set();
  state.highlights.filter((h) => h.file === path).forEach((h) => {
    for (let l = h.line_range.start; l <= h.line_range.end; l++) set.add(l);
  });
  return set;
}

// new-side lines carrying a host discussion (GitLab thread), for a distinct diff overlay
function threadLines(path) {
  const set = new Set();
  (state.threads || []).forEach((t) => {
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
  const hunks = parseHunks((file.hunks || []).map((h) => h.diff || "").join("\n"));
  let cursor = 1;
  for (const h of hunks) {
    if (line < h.newStart) return line >= cursor ? cursor : null;
    if (line < h.newStart + h.newCount) return null;      // inside this hunk
    cursor = h.newStart + h.newCount;
  }
  return line >= cursor ? cursor : null;                   // past the last hunk — the trailing gap
}

function highlightExact(path, lo, hi) {
  return state.highlights.find((h) => h.file === path && h.line_range.start === lo && h.line_range.end === hi);
}

// the per-file diffs currently in play: the since-last delta when that mode is on and parsed, else
// the full MR diff. Lets the file tree and the diff pane share one path for both views.
function sinceFilesReady() {
  return sinceLast && sinceLastData && sinceLastData.mode === "diff" && Array.isArray(sinceLastData.files);
}
function activeFiles() {
  if (commitsMode && currentCommit && commitFiles[currentCommit]) return commitFiles[currentCommit];
  if (sinceFilesReady()) return sinceLastData.files;
  return state.files;
}

function renderDiff() {
  const el = $("diff");
  el.innerHTML = "";
  if (commitsMode) { renderCommitView(el); return; }
  if (sinceLast) { renderSinceLast(el); return; }
  if (viewingPath) { renderFileView(el, viewingPath); return; }
  renderFileDiff(el, state.files, "  ·  click a line, or drag to select a block", true);
}

// --- per-commit review ------------------------------------------------------

async function toggleCommits() {
  commitsMode = !commitsMode;
  $("t-commits").classList.toggle("on", commitsMode);
  if (commitsMode) {
    sinceLast = false; viewingPath = null;   // one diff mode at a time; both are diff views
    if (commitList === null) {
      render();   // show "loading commits…" while the list is fetched
      try {
        const d = await fetch(`/api/sessions/${SID}/commits`).then((r) => r.json());
        commitList = Array.isArray(d.commits) ? d.commits : [];
      } catch (e) { commitList = []; }
      if (commitList.length && !currentCommit) currentCommit = commitList[0].sha;
    }
    if (currentCommit && !commitFiles[currentCommit]) await loadCommit(currentCommit);
  }
  render();
}

async function loadCommit(sha) {
  try {
    const d = await fetch(`/api/sessions/${SID}/commit/${encodeURIComponent(sha)}`).then((r) => r.json());
    commitFiles[sha] = Array.isArray(d.files) ? d.files : [];
  } catch (e) { commitFiles[sha] = []; }
}

async function selectCommit(sha) {
  currentCommit = sha; currentFile = null;   // reset to the new commit's first file
  if (!commitFiles[sha]) { render(); await loadCommit(sha); }
  render();
}

function stepCommit(delta) {
  if (!commitList || !commitList.length) return;
  const i = commitList.findIndex((c) => c.sha === currentCommit);
  const j = Math.min(commitList.length - 1, Math.max(0, (i < 0 ? 0 : i) + delta));
  if (commitList[j]) selectCommit(commitList[j].sha);
}

function renderCommitView(el) {
  if (commitList === null) { el.appendChild(empty("loading commits…")); return; }
  if (!commitList.length) { el.appendChild(empty("no commits on this MR")); return; }
  const i = Math.max(0, commitList.findIndex((c) => c.sha === currentCommit));
  const c = commitList[i];
  // pair with the reviewed watermark: commits at/before it (in oldest→newest order) are reviewed,
  // the rest are new since your last review. -1 when there's no watermark or it isn't in this list.
  const wm = reviewStatus && reviewStatus.watermark;
  const wmIndex = wm ? commitList.findIndex((x) => x.sha === wm) : -1;
  const reviewed = (k) => wmIndex >= 0 && k <= wmIndex;

  const bar = document.createElement("div");
  bar.className = "commitbar";
  const prev = btn("◀", "btn ghost", () => stepCommit(-1)); if (i <= 0) prev.disabled = true;
  const next = btn("▶", "btn ghost", () => stepCommit(1)); if (i >= commitList.length - 1) next.disabled = true;
  const pos = document.createElement("span"); pos.className = "cpos"; pos.textContent = `commit ${i + 1}/${commitList.length}`;
  const sel = document.createElement("select"); sel.className = "csel";
  commitList.forEach((x, k) => {
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

  const files = commitFiles[c.sha];
  if (!files) { el.appendChild(empty("loading commit…")); return; }
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
  const table = document.createElement("table");
  table.className = "hunk";
  if (!splitMode && interactive) {
    // unified: render with "unfold" bands revealing the context between hunks (full diff, and any
    // head-aligned since-last diff — its new side is the head blob the bands reveal from)
    const fileDiff = (file.hunks || []).map((h) => h.diff || "").join("\n");
    renderUnifiedUnfoldable(table, fileDiff, file.path, hl);
  } else {
    const lang = langOf(file.path);
    (file.hunks || []).forEach((h) => {
      (splitMode ? splitRows : unifiedRows)(h.diff || "", hl, table, lang);
    });
  }
  if (interactive) wireSelection(table, file.path);
  el.appendChild(table);
  if (interactive) overlayThreadAnchors(table, file.path);   // mark host-discussion lines (head coords)
}

// a rendered Markdown view of a doc's current version, toggled from the diff (the raw diff stays a
// click away). Scroll-sync to the changed hunk is a future step — this gives the reading view.
async function toggleMd(path) {
  if (mdRendered.has(path)) mdRendered.delete(path); else mdRendered.add(path);
  if (mdRendered.has(path) && fileContents[path] === undefined) {
    try {
      const d = await fetch(`/api/sessions/${SID}/file?path=${encodeURIComponent(path)}`).then((r) => r.json());
      fileContents[path] = (d && typeof d.content === "string") ? d.content : "";
    } catch (e) { fileContents[path] = ""; }
  }
  renderDiff();
}

function renderMarkdownDoc(el, path) {
  const content = fileContents[path];
  if (content === undefined) { el.appendChild(empty("loading " + path + "…")); return; }
  const box = document.createElement("div");
  box.className = "mdview md";
  box.innerHTML = md(content);
  el.appendChild(box);
}

// parse a unified file diff into hunks carrying their new-side start/count (from the @@ header)
function parseHunks(fileDiff) {
  const hunks = [];
  let cur = null;
  (fileDiff || "").split("\n").forEach((raw) => {
    if (raw.startsWith("@@")) {
      const mn = raw.match(/\+(\d+)(?:,(\d+))?/);
      const newStart = mn ? parseInt(mn[1], 10) : 1;
      const newCount = mn && mn[2] !== undefined ? parseInt(mn[2], 10) : 1;
      cur = { newStart, newCount, lines: [] };
      hunks.push(cur);
    } else if (cur && raw !== "") {
      cur.lines.push(raw);
    }
  });
  return hunks;
}

// unified diff with GitHub-style unfold: gaps between hunks show a "⋯ show N lines" band that
// reveals the real context (fetched from the file blob) on click. Gap sizes come from the @@ line
// numbers, so the bands render before any fetch; only the revealed text needs the full file.
function renderUnifiedUnfoldable(table, fileDiff, path, hl) {
  const hunks = parseHunks(fileDiff);
  const lines = fileContents[path] !== undefined ? fileContents[path].split("\n") : null;
  const exp = expandedGaps[path] || new Map();
  const lang = langOf(path);
  let cursor = 1;   // next not-yet-shown new-side line number
  hunks.forEach((h) => {
    renderGap(table, path, cursor, h.newStart - 1, lines, exp, hl, lang);
    let newLine = h.newStart;
    h.lines.forEach((raw) => {
      const kind = raw[0] === "+" ? "add" : raw[0] === "-" ? "del" : "ctx";
      const tr = document.createElement("tr");
      tr.className = "line " + kind + (kind !== "del" && hl.has(newLine) ? " hl" : "");
      const code = `<td class="code"${kind !== "del" ? ` data-line="${newLine}"` : ""}>${hlLine(raw, lang)}</td>`;
      tr.innerHTML = `<td class="ln">${kind === "del" ? "" : newLine}</td>${code}`;
      if (kind !== "del") newLine += 1;
      table.appendChild(tr);
    });
    cursor = h.newStart + h.newCount;
  });
  if (lines) {
    renderGap(table, path, cursor, lines.length, lines, exp, hl, lang);   // trailing gap — exact, length known
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
function renderGap(table, path, from, to, lines, exp, hl, lang) {
  if (to < from) return;
  const size = to - from + 1;
  const g = exp.get(from) || { top: 0, bot: 0, all: false };
  const ctxRow = (n) => {
    const text = lines && lines[n - 1] !== undefined ? lines[n - 1] : "";
    const tr = document.createElement("tr");
    tr.className = "line ctx" + (hl.has(n) ? " hl" : "");
    tr.innerHTML = `<td class="ln">${n}</td><td class="code" data-line="${n}">${esc(" ") + highlightCode(text, lang)}</td>`;
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

async function expandGap(path, from, kind) {
  const m = expandedGaps[path] = expandedGaps[path] || new Map();
  const g = m.get(from) || { top: 0, bot: 0, all: false };
  if (kind === "all") g.all = true;
  else if (kind === "top") g.top += UNFOLD_CHUNK;
  else if (kind === "bot") g.bot += UNFOLD_CHUNK;
  m.set(from, g);
  if (fileContents[path] === undefined) {   // reveal needs the full blob — fetch once, then re-render
    try {
      const d = await fetch(`/api/sessions/${SID}/file?path=${encodeURIComponent(path)}`).then((r) => r.json());
      fileContents[path] = (d && typeof d.content === "string") ? d.content : "";
    } catch (e) { fileContents[path] = ""; }
  }
  renderDiff();
}

// "since last review" — a normal diff of the author's net changes, rendered per-file like the full
// diff (falls back to the flat range-diff only when a conflicting replay forced that mode)
function renderSinceLast(el) {
  const d = sinceLastData;
  if (d && !d.error && d.available !== false && !d.empty) {
    if (d.mode === "rangediff") {
      const name = document.createElement("div");
      name.className = "fname"; name.textContent = "Changes since your last review · rebase noise excluded";
      el.appendChild(name);
      el.appendChild(rangeDiffView(d.interdiff));
      return;
    }
    if (d.note) { const n = document.createElement("div"); n.className = "sincenote"; n.textContent = "⚠ " + d.note; el.appendChild(n); }
    // fully interactive (highlight, comment, unfold) when head-aligned — the diff's new side is then
    // the head blob, so its line numbers anchor exactly like the full diff. A stale session (head
    // moved past the session) is read-only until a refresh re-syncs the head.
    renderFileDiff(el, d.files || [], "  ·  since your last review", d.head_aligned !== false);
    return;
  }
  const box = document.createElement("div");
  box.style.padding = "12px 16px";
  if (!d) { box.className = "empty"; box.textContent = "computing the diff…"; }
  else if (d.available === false) { box.className = "empty"; box.textContent = "unavailable on this host"; }
  else if (d.error) { box.className = "empty"; box.textContent = "couldn't compute: " + d.error; }
  else { box.className = "empty"; box.textContent = d.note || "No author changes since your last review (a rebase brought no new work)."; }
  el.appendChild(box);
}

// git range-diff is a diff-of-diffs — dense as raw text. Render it as a proper dual-column split.
// The format is deterministic: cols 0-3 are indent, col 4 is the OUTER marker (was the line in the
// reviewed patch / is it in the current one: ' '=both, '-'=old-only, '+'=new-only, '@'=section),
// col 5+ is the INNER patch line with its own +/-. Two gutters show old|new presence, the row is
// tinted by the since-review delta, and the code cell keeps the patch-level +/- coloring.
function rangeDiffView(text) {
  const wrap = document.createElement("div");
  wrap.className = "rdiff";
  const commitRe = /^\s*(?:\d+|-):\s+\S+\s+([=!<>])\s+(?:\d+|-):\s+\S+/;
  const opClass = { "!": "rd-cmod", ">": "rd-cnew", "<": "rd-cdrop", "=": "rd-csame" };
  const legend = document.createElement("div");
  legend.className = "rdlegend";
  legend.textContent = "old = in the version you reviewed · new = in the current version · tinted row = changed since your review";
  wrap.appendChild(legend);

  let table = null;
  const header = (cls, txt) => {
    table = null;
    const h = document.createElement("div");
    h.className = "rdln " + cls;
    h.textContent = txt;
    wrap.appendChild(h);
  };
  const cell = (cls, txt) => { const c = document.createElement("td"); c.className = cls; c.textContent = txt; return c; };

  (text || "").split("\n").forEach((line) => {
    const cm = line.match(commitRe);
    if (cm) { header("rdcommit " + (opClass[cm[1]] || "rd-csame"), line); return; }
    if (/^\s{0,4}@@ /.test(line) && line[4] === "@") { header("rd-file", line.replace(/^\s+/, "")); return; }

    const outer = line[4] || " ";                 // present-in-old / present-in-new marker
    const inner = line.slice(5);                  // the underlying patch line (keeps its own +/-)
    const im = inner[0];                          // patch-level add / del / hunk
    if (!table) { table = document.createElement("table"); table.className = "rdtable"; wrap.appendChild(table); }
    const tr = document.createElement("tr");
    tr.className = outer === "+" ? "rd-add" : outer === "-" ? "rd-del" : "rd-ctx";
    const oldPresent = outer === " " || outer === "-";
    const newPresent = outer === " " || outer === "+";
    tr.appendChild(cell("rg" + (outer === "-" ? " g-del" : ""), outer === "-" ? "−" : oldPresent ? "·" : ""));
    tr.appendChild(cell("rg" + (outer === "+" ? " g-add" : ""), outer === "+" ? "+" : newPresent ? "·" : ""));
    tr.appendChild(cell("rc" + (im === "+" ? " i-add" : im === "-" ? " i-del" : im === "@" ? " i-hunk" : ""), inner));
    table.appendChild(tr);
  });
  return wrap;
}

function renderFileView(el, path) {
  const name = document.createElement("div");
  name.className = "fname";
  name.textContent = path + "  ·  related file (not in the diff) · click or drag to ask for context";
  el.appendChild(name);
  const content = fileContents[path];
  if (content === undefined) { el.appendChild(empty("loading " + path + "…")); return; }
  const hl = highlightLines(path);
  const lang = langOf(path);
  const table = document.createElement("table");
  table.className = "hunk";
  content.split("\n").forEach((line, i) => {
    const n = i + 1;
    const tr = document.createElement("tr");
    tr.className = "line ctx" + (hl.has(n) ? " hl" : "");
    tr.innerHTML = `<td class="ln">${n}</td><td class="code" data-line="${n}">${highlightCode(line, lang)}</td>`;
    table.appendChild(tr);
  });
  wireSelection(table, path);
  el.appendChild(table);
}

// click a line = toggle its highlight (dedupe + discard-by-reclick); drag = select a block
function wireSelection(table, path) {
  let dragStart = null;
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

function unifiedRows(diff, hl, table, lang) {
  let newLine = 0;
  diff.split("\n").forEach((raw) => {
    if (raw === "") return;
    const tr = document.createElement("tr");
    if (raw.startsWith("@@")) {
      tr.className = "hh";
      const m = raw.match(/\+(\d+)/); if (m) newLine = parseInt(m[1], 10);
      tr.innerHTML = `<td class="ln"></td><td class="code">${esc(raw)}</td>`;
      table.appendChild(tr); return;
    }
    const kind = raw[0] === "+" ? "add" : raw[0] === "-" ? "del" : "ctx";
    tr.className = "line " + kind + (kind !== "del" && hl.has(newLine) ? " hl" : "");
    const code = `<td class="code"${kind !== "del" ? ` data-line="${newLine}"` : ""}>${hlLine(raw, lang)}</td>`;
    tr.innerHTML = `<td class="ln">${kind === "del" ? "" : newLine}</td>${code}`;
    if (kind !== "del") newLine += 1;
    table.appendChild(tr);
  });
}

function splitRows(diff, hl, table, lang) {
  let oldLine = 0, newLine = 0;
  let pendDel = [], pendAdd = [];
  const flush = () => {
    const n = Math.max(pendDel.length, pendAdd.length);
    for (let i = 0; i < n; i++) {
      const d = pendDel[i], a = pendAdd[i];
      const tr = document.createElement("tr"); tr.className = "line";
      appendCell(tr, d ? "del" : "gap", d ? d.ln : "", d ? d.text : "", null, false, lang);
      appendCell(tr, a ? "add" : "gap", a ? a.ln : "", a ? a.text : "", a ? a.ln : null, a && hl.has(a.ln), lang);
      table.appendChild(tr);
    }
    pendDel = []; pendAdd = [];
  };
  diff.split("\n").forEach((raw) => {
    if (raw === "") return;
    if (raw.startsWith("@@")) {
      flush();
      const mo = raw.match(/-(\d+)/), mn = raw.match(/\+(\d+)/);
      if (mo) oldLine = parseInt(mo[1], 10);
      if (mn) newLine = parseInt(mn[1], 10);
      const tr = document.createElement("tr"); tr.className = "hh";
      tr.innerHTML = `<td class="ln"></td><td class="code">${esc(raw)}</td><td class="ln"></td><td class="code">${esc(raw)}</td>`;
      table.appendChild(tr); return;
    }
    if (raw[0] === "-") { pendDel.push({ ln: oldLine, text: raw.slice(1) }); oldLine += 1; }
    else if (raw[0] === "+") { pendAdd.push({ ln: newLine, text: raw.slice(1) }); newLine += 1; }
    else {
      flush();
      const tr = document.createElement("tr"); tr.className = "line";
      const txt = raw.slice(1);
      appendCell(tr, "ctx", oldLine, txt, null, false, lang);
      appendCell(tr, "ctx", newLine, txt, newLine, hl.has(newLine), lang);
      table.appendChild(tr); oldLine += 1; newLine += 1;
    }
  });
  flush();
}

function appendCell(tr, kind, ln, text, dataLine, isHl, lang) {
  const tdLn = document.createElement("td");
  tdLn.className = "ln" + (kind === "gap" ? " gap" : kind === "del" ? " delln" : kind === "add" ? " addln" : "");
  tdLn.textContent = ln === "" ? "" : ln;
  const tdCode = document.createElement("td");
  tdCode.className = "code" + (kind === "gap" ? " gap" : kind === "del" ? " delc" : kind === "add" ? " addc" : "")
                   + (isHl ? " hlc" : "");
  if (kind === "gap") tdCode.textContent = "";
  else tdCode.innerHTML = highlightCode(text || "", lang);
  if (dataLine != null) tdCode.dataset.line = dataLine;  // new-side, selectable
  tr.appendChild(tdLn); tr.appendChild(tdCode);
}

// --- rail: a compact, filterable index; the card+editor open in a side overlay ----

// a highlight's place in the reviewer's taxonomy: context-only "card" → drafted "comment" → posted
function commentState(hl) {
  const d = state.drafts.find((x) => x.highlight_id === hl.id);
  if (!d) return "context";
  return d.status === "posted" ? "posted" : "comment";
}

function firstLine(s) {
  const ln = (s || "").split("\n").find((l) => l.trim()) || "";
  return ln.length > 80 ? ln.slice(0, 79) + "…" : ln;
}

function railMatch(hl) {
  if (railFilter !== "all" && commentState(hl) !== railFilter) return false;
  if (railQuery) {
    const d = state.drafts.find((x) => x.highlight_id === hl.id);
    const buf = (hl.id in draftBuffers) ? draftBuffers[hl.id] : (d ? d.body : "");
    const hay = `${hl.file} ${hl.question || ""} ${buf}`.toLowerCase();
    if (!hay.includes(railQuery.toLowerCase())) return false;
  }
  return true;
}

function renderRail() {
  const el = $("rail");
  el.innerHTML = "";

  renderVersionBanner(el);      // "updated since your last review" (diff-versions)
  renderReviewBar(el);          // sticky submit + counts
  renderMrRow(el);              // the MR-level review comment, pinned above the index
  renderRailTools(el);          // filter chips + text search

  el.appendChild(h3("Highlights & cards"));
  const list = document.createElement("div");
  list.id = "hlist";
  el.appendChild(list);
  renderHlist();

  // MR-level insights Claude raised on its own (cards anchored to no highlight)
  const insights = state.cards.filter((c) => !c.highlight_id);
  if (insights.length) {
    el.appendChild(h3("Claude's insights"));
    insights.forEach((c) => el.appendChild(insightRow(c)));
  }

  renderThreads(el);            // existing MR discussions — reply / resolve / refresh

  const pending = state.access_requests.filter((r) => r.status === "pending");
  el.appendChild(h3("Access requests"));
  if (!pending.length) el.appendChild(empty("none"));
  pending.forEach((r) => {
    const box = document.createElement("div");
    box.className = "req";
    box.innerHTML = `<div class="repo">${esc(r.repo)}</div><div class="why">${esc(r.reason)}</div>`;
    box.appendChild(btn("Approve", "btn ok", () => post({ type: "decide_access", request_id: r.id, approve: true })));
    box.appendChild(btn("Deny", "btn no", () => post({ type: "decide_access", request_id: r.id, approve: false })));
    el.appendChild(box);
  });

  renderChat(el);
  renderDetail();

  if (railSearchFocused) {  // a WS-driven re-render shouldn't steal the search box you're typing in
    const s = $("railsearch");
    if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); }
  }
}

function renderRailTools(el) {
  if (!state.highlights.length) return;
  const wrap = document.createElement("div");
  wrap.className = "railtools";
  const seg = document.createElement("div"); seg.className = "seg"; seg.id = "railseg";
  wrap.appendChild(seg);
  const inp = document.createElement("input");
  inp.id = "railsearch"; inp.placeholder = "filter…"; inp.value = railQuery;
  inp.oninput = (e) => { railQuery = e.target.value; renderHlist(); };  // list-only → input keeps focus
  inp.onfocus = () => { railSearchFocused = true; };
  inp.onblur = () => { railSearchFocused = false; };
  wrap.appendChild(inp);
  el.appendChild(wrap);
  fillSeg();
}

function fillSeg() {
  const seg = $("railseg"); if (!seg) return;
  seg.innerHTML = "";
  const counts = { all: state.highlights.length, context: 0, comment: 0, posted: 0 };
  state.highlights.forEach((hl) => { counts[commentState(hl)] += 1; });
  [["all", "All"], ["context", "Cards"], ["comment", "Comments"], ["posted", "Posted"]].forEach(([k, label]) => {
    seg.appendChild(btn(`${label} ${counts[k]}`, "btn" + (railFilter === k ? " on" : ""),
      () => { railFilter = k; fillSeg(); renderHlist(); }));
  });
}

function renderHlist() {
  const list = $("hlist"); if (!list) return;
  list.innerHTML = "";
  if (!state.highlights.length) { list.appendChild(empty("highlight a line to ask for context")); return; }
  let shown = 0;
  state.highlights.forEach((hl, i) => { if (railMatch(hl)) { list.appendChild(hlRow(hl, i + 1)); shown += 1; } });
  if (!shown) list.appendChild(empty("no highlights match this filter"));
}

function hlRow(hl, n) {
  const st = commentState(hl);
  const card = state.cards.find((c) => c.highlight_id === hl.id);
  const d = state.drafts.find((x) => x.highlight_id === hl.id);
  const buf = (hl.id in draftBuffers) ? draftBuffers[hl.id] : (d ? d.body : "");
  const lr = hl.line_range;
  const loc = `${hl.file}:${lr.start}${lr.end !== lr.start ? "-" + lr.end : ""}`;
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
    (isStale(hl) ? `<span class="chip stale" title="made on an earlier version — its lines may have moved">older ver</span>` : "") +
    `<span class="loc">${esc(loc)}</span></div>` +
    `<div class="prev">${esc(prev)}</div>`;
  // the index is the always-visible surface, so an escalation still waiting shows a live cue here
  // too — otherwise the reviewer has to open the panel to learn whether anything is happening
  if (!buf && st === "context" && !card && hl.context_requested) {
    const p = row.querySelector(".prev");
    p.textContent = "";
    p.appendChild(agentWaitLine(hl.context_requested_at || hl.created_at, true));
  }
  row.onclick = () => { selected = { kind: "hl", id: hl.id }; renderRail(); };
  row.querySelector(".x").onclick = (e) => { e.stopPropagation(); post({ type: "remove_highlight", highlight_id: hl.id }); };
  return row;
}

function insightRow(c) {
  const active = selected && selected.kind === "insight" && selected.id === c.id;
  const row = document.createElement("div");
  row.className = "hrow agent" + (active ? " active" : "");
  row.innerHTML =
    `<button class="x" title="dismiss">×</button>` +
    `<div class="top"><span class="chip insight">MR-level</span></div>` +
    `<div class="prev">${esc(firstLine(c.body))}</div>`;
  row.onclick = () => { selected = { kind: "insight", id: c.id }; renderRail(); };
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
  row.onclick = () => { selected = { kind: "mr" }; renderRail(); };
  if (d && !posted) row.querySelector(".x").onclick = (e) => {
    e.stopPropagation(); delete draftBuffers[MR_KEY]; post({ type: "remove_draft", highlight_id: null });
  };
  el.appendChild(row);
}

function mrDraft() { return state.drafts.find((d) => !d.highlight_id); }  // the MR-level comment, if any

// the non-blocking detail overlay: full card + roomy editor for the selected row
function renderDetail() {
  const el = $("detail");
  const close = () => { selected = null; renderRail(); };

  if (selected && selected.kind === "mr") {
    const d = mrDraft();
    el.hidden = false; el.innerHTML = "";
    const head = document.createElement("div");
    head.className = "dhead";
    head.innerHTML = `<span class="chip">MR-level</span><span class="dlabel">review comment — whole MR</span>`;
    head.appendChild(btn("×", "dclose", close));
    el.appendChild(head);
    el.appendChild(draftEditor(MR_KEY, null, d));
    if (!(d && d.status === "posted") && focusedDraft === MR_KEY) {
      const ta = el.querySelector("textarea.draftbox");
      if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
    }
    return;
  }

  if (selected && selected.kind === "insight") {
    const c = state.cards.find((x) => x.id === selected.id);
    if (!c) { selected = null; el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false; el.innerHTML = "";
    const head = document.createElement("div");
    head.className = "dhead";
    head.innerHTML = `<span class="byclaude">Claude's insight</span>`;
    head.appendChild(btn("×", "dclose", close));
    el.appendChild(head);
    const body = document.createElement("div");
    body.className = "card md"; body.innerHTML = md(c.body);
    el.appendChild(body);
    return;
  }

  if (selected && selected.kind === "thread") {
    const t = (state.threads || []).find((x) => x.id === selected.id);
    if (!t) { selected = null; el.hidden = true; el.innerHTML = ""; return; }
    const loc = t.anchor && t.anchor.file
      ? `${t.anchor.file}${t.anchor.line ? ":" + t.anchor.line : ""}` : "whole MR";
    el.hidden = false; el.innerHTML = "";
    const head = document.createElement("div");
    head.className = "dhead";
    head.innerHTML = (t.resolved ? `<span class="chip posted">✓ resolved</span>` : `<span class="chip comment">open</span>`) +
      `<span class="dlabel">${esc(loc)}</span>`;
    head.appendChild(btn("×", "dclose", close));
    el.appendChild(head);
    el.appendChild(threadConversationBlock(t));
    return;
  }

  const hl = selected ? state.highlights.find((h) => h.id === selected.id) : null;
  if (!hl) { selected = null; el.hidden = true; el.innerHTML = ""; return; }
  const card = state.cards.find((c) => c.highlight_id === hl.id);
  const draft = state.drafts.find((d) => d.highlight_id === hl.id);
  const posted = draft && draft.status === "posted";
  const lr = hl.line_range;
  const loc = `${hl.file}:${lr.start}${lr.end !== lr.start ? "-" + lr.end : ""}`;

  el.hidden = false; el.innerHTML = "";
  const head = document.createElement("div");
  head.className = "dhead";
  head.innerHTML = `<span class="num">#${state.highlights.indexOf(hl) + 1}</span>` +
    (hl.author === "agent" ? `<span class="byclaude">Claude flagged</span>` : "") +
    `<span class="loc" title="jump to code">${esc(loc)}</span>`;
  head.appendChild(btn("×", "dclose", close));
  el.appendChild(head);
  head.querySelector(".loc").onclick = () => goToHighlight(hl);

  if (hl.question) {
    const q = document.createElement("div");
    q.className = "q"; q.textContent = hl.question;
    el.appendChild(q);
  }
  // the cheap, deterministic context tier — shown by default, no agent (D21)
  if (!posted) el.appendChild(cheapContextBlock(hl));
  // the agent tier is on demand: the card if present, else escalate — or "waiting" once escalated
  if (!posted) {
    if (card) {
      const c = document.createElement("div");
      c.className = "card md"; c.innerHTML = md(card.body);
      el.appendChild(c);
    } else if (hl.context_requested) {
      el.appendChild(agentWaitLine(hl.context_requested_at || hl.created_at));
    } else {
      el.appendChild(askContextControl(hl));
    }
  }
  // once posted, the reviewer's comment IS a live thread — show it inline (draft-as-thread)
  const postedThread = posted && draft && draft.thread_id
    ? (state.threads || []).find((x) => x.id === draft.thread_id) : null;
  if (postedThread) {
    const lbl = document.createElement("div"); lbl.className = "yourthread"; lbl.textContent = "your comment";
    el.appendChild(lbl);
    el.appendChild(threadConversationBlock(postedThread));
  } else {
    el.appendChild(draftEditor(hl.id, hl.id, draft));
  }

  if (!posted && focusedDraft === hl.id) {  // restore focus across a WS re-render; never steal it
    const ta = el.querySelector("textarea.draftbox");
    if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
  }
  if (askFocused === hl.id) {
    const ai = el.querySelector("input.askinp");
    if (ai) { ai.focus(); ai.setSelectionRange(ai.value.length, ai.value.length); }
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
    const hl = state.highlights.find((h) => h.id === anchor);
    const seed = (draft && draft.suggestion != null) ? draft.suggestion
               : (hl ? newSideLines(hl.file, hl.line_range.start, hl.line_range.end) : "");
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
    renderRail();
  }));
  if (draft) row.appendChild(btn("Remove", "btn ghost", () => {
    post({ type: "remove_draft", highlight_id: anchor });
    delete draftBuffers[key]; delete suggBuf[key]; suggOpen[key] = false;
  }));
  wrap.appendChild(row);
  return wrap;
}

// existing MR discussions (host threads) — list, filter, and open a conversation in the overlay
function renderThreads(el) {
  // a posted draft is already shown inline under its highlight ("your comment"); don't also list
  // its thread here, or the reviewer's own comments double up once refresh re-mirrors them
  const ownPosted = new Set((state.drafts || [])
    .filter((d) => d.status === "posted" && d.thread_id)
    .map((d) => d.thread_id));
  const threads = (state.threads || []).filter((t) => !ownPosted.has(t.id));
  const head = document.createElement("div");
  head.className = "chathdr";
  head.appendChild(h3("Discussions"));
  head.appendChild(btn("↻ refresh", "btn ghost", refreshThreads));
  el.appendChild(head);
  if (!threads.length) { el.appendChild(empty("no discussions on this MR")); return; }

  const seg = document.createElement("div");
  seg.className = "seg";
  const unresolved = threads.filter((t) => !t.resolved).length;
  [["unresolved", `Unresolved ${unresolved}`], ["all", `All ${threads.length}`]].forEach(([k, label]) => {
    seg.appendChild(btn(label, "btn" + (threadFilter === k ? " on" : ""),
      () => { threadFilter = k; renderRail(); }));
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
  row.onclick = () => { selected = { kind: "thread", id: t.id }; renderRail(); };
  if (t.anchor && t.anchor.file) {   // the location links to the code — jump the diff there + flash
    const locEl = row.querySelector(".loc");
    locEl.classList.add("jumpcode"); locEl.title = "jump to this line in the diff";
    locEl.onclick = (e) => { e.stopPropagation(); jumpToCode(t.anchor.file, t.anchor.line); };
  }
  const m = matchingHighlight(t);   // also overlaps one of your highlights → offer a jump to it
  if (m) {
    const jump = btn(`→ #${m.n}`, "btn ghost jump", (e) => {
      e.stopPropagation(); selected = { kind: "hl", id: m.hl.id }; renderRail();
    });
    row.querySelector(".top").appendChild(jump);
  }
  return row;
}

function matchingHighlight(t) {
  if (!t.anchor || !t.anchor.file) return null;
  const line = t.anchor.line;
  const i = state.highlights.findIndex((h) => h.file === t.anchor.file && line != null &&
    line >= h.line_range.start && line <= h.line_range.end);
  return i >= 0 ? { hl: state.highlights[i], n: i + 1 } : null;
}

async function threadAction(path, body, okMsg) {
  setStatus("…");
  try {
    const r = await fetch(`/api/sessions/${SID}/${path}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { setStatus("✕ " + (data.error || "failed")); return false; }
    setStatus(okMsg || "");
    return true;
  } catch (e) { setStatus("✕ " + e); return false; }
}

async function refreshThreads() {
  // re-syncs the whole MR from the host (head + diff + discussions), not just threads — so an
  // updated MR is noticed and the "Since last review" banner can appear (diff-versions)
  if (await threadAction("refresh-threads", {}, "re-synced with host")) await load();
}

async function replyThread(tid) {
  const body = (threadReplyBuf[tid] || "").trim();
  if (!body) return;
  if (await threadAction(`threads/${tid}/reply`, { body }, "reply posted")) {
    delete threadReplyBuf[tid]; await load();
  }
}

async function resolveThread(tid, resolved) {
  if (await threadAction(`threads/${tid}/resolve`, { resolved }, resolved ? "resolved" : "reopened")) {
    await load();
  }
}

async function submitNoteEdit(tid, nid) {
  const body = (noteEdit[nid] || "").trim();
  if (!body) return;
  if (await threadAction(`threads/${tid}/notes/${nid}/edit`, { body }, "edited")) {
    delete noteEdit[nid]; await load();
  }
}

async function deleteNote(tid, nid) {
  if (!confirm("Delete this comment?")) return;
  if (await threadAction(`threads/${tid}/notes/${nid}/delete`, {}, "deleted")) await load();
}

// the conversation for a thread — notes (edit/delete on your own) + reply + resolve.
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
      row.appendChild(btn("Cancel", "btn ghost", () => { delete noteEdit[c.id]; renderRail(); }));
      d.innerHTML = `<div class="who">${esc(c.author)}</div>`;
      d.appendChild(ta); d.appendChild(row);
    } else {
      d.innerHTML = `<div class="who">${esc(c.author)}</div><div class="md">${md(c.body)}</div>`;
      if (canThreads && me && c.author === me) {    // your own note → edit / delete
        const acts = document.createElement("div"); acts.className = "noteacts";
        acts.appendChild(btn("edit", "btn ghost", () => { noteEdit[c.id] = c.body; renderRail(); }));
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
  if (!reviewStatus) return;
  const cap = state.mr && (state.mr.capabilities || {}).diff_versions === true;
  if (!cap) return;
  if (reviewStatus.behind) {
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
  } else if (!reviewStatus.watermark) {
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
  const pending = state.drafts.filter((d) => d.status !== "posted");
  const posted = state.drafts.filter((d) => d.status === "posted");
  const canApprove = state.mr && (state.mr.capabilities || {}).approvals !== false;
  // the bar is worth showing when there is something to submit or an approval to give
  if (!pending.length && !posted.length && !canApprove) return;

  const bar = document.createElement("div");
  bar.className = "reviewbar sticky";
  const lbl = document.createElement("span");
  lbl.textContent = `Your review · ${pending.length} pending${posted.length ? ` · ${posted.length} posted` : ""}`;
  bar.appendChild(lbl);
  if (approvalStatus && approvalStatus.you_approved) {   // your prior review approved this MR
    const ap = document.createElement("span");
    ap.className = "chip posted"; ap.textContent = "✓ you approved";
    bar.appendChild(ap);
  }

  if (canApprove) {
    const tog = document.createElement("label");
    tog.className = "approve-tog";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = approveToggle;
    cb.onchange = (e) => { approveToggle = e.target.checked; };
    tog.appendChild(cb);
    tog.appendChild(document.createTextNode(" Approve MR"));
    bar.appendChild(tog);
  }
  // submittable when there are drafts to post, or an approve is toggled on (approve-only)
  const label = pending.length ? "Submit review" : (approveToggle ? "Approve MR" : "Submit review");
  const b = btn(label, "btn primary", submitReview);
  if (!pending.length && !approveToggle) b.disabled = true;
  bar.appendChild(b);
  el.appendChild(bar);
}

async function submitReview() {
  setStatus(approveToggle ? "submitting review…" : "posting review…");
  try {
    const r = await fetch(`/api/sessions/${SID}/submit-review`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ approve: approveToggle }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { setStatus("✕ " + (data.error || "submit failed")); return; }
    const failed = (data.results || []).filter((x) => !x.ok);
    const parts = [];
    if (data.total) parts.push(failed.length ? `posted ${data.posted}/${data.total} — ${failed.length} failed`
                                             : `posted ${data.posted} comment${data.posted === 1 ? "" : "s"}`);
    if (data.approved) parts.push("approved");
    else if (data.approve_error) parts.push("approve failed: " + data.approve_error);
    setStatus(parts.join(" · ") || "nothing to submit");
    approveToggle = false;
  } catch (e) { setStatus("✕ " + e); }
}

function renderChat(el) {
  const wrap = document.createElement("div");
  wrap.className = "chat";
  const head = document.createElement("div");
  head.className = "chathdr";
  head.appendChild(h3("Chat with Claude"));
  if (state.messages.length) {
    const clear = btn("clear", "btn ghost", () => {
      if (confirm("Clear the chat thread?")) post({ type: "clear_chat" });
    });
    head.appendChild(clear);
  }
  wrap.appendChild(head);
  const msgs = document.createElement("div");
  msgs.className = "msgs";
  if (!state.messages.length) msgs.appendChild(empty('ask about a card (e.g. "expand on #2") or anything in the diff'));
  state.messages.forEach((m) => {
    const d = document.createElement("div");
    d.className = "msg " + (m.role === "user" ? "user" : "agent");
    d.innerHTML = `<div class="who">${m.role}</div><div class="md">${md(m.body)}</div>`;
    msgs.appendChild(d);
  });
  // your turn is still unanswered — say whether it's being worked on or nothing picked it up
  const lastMsg = state.messages[state.messages.length - 1];
  if (lastMsg && lastMsg.role === "user") msgs.appendChild(agentWaitLine(lastMsg.created_at));
  wrap.appendChild(msgs);

  const box = document.createElement("div");
  box.className = "chatbox";
  const inp = document.createElement("input");
  inp.placeholder = "message Claude — reference a card by #N";
  inp.value = chatDraft;
  inp.oninput = (e) => { chatDraft = e.target.value; };
  inp.onfocus = () => { chatFocused = true; };
  inp.onblur = () => { chatFocused = false; };
  const send = () => {
    const v = inp.value.trim();
    if (!v) return;
    post({ type: "post_message", body: v });
    chatDraft = ""; inp.value = "";
  };
  inp.onkeydown = (e) => { if (e.key === "Enter") send(); };
  box.appendChild(inp);
  box.appendChild(btn("Send", "btn", send));
  wrap.appendChild(box);
  el.appendChild(wrap);

  msgs.scrollTop = msgs.scrollHeight;
  if (chatFocused) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
}

// escalate a highlight to the agent tier (D21) — separate from the review-comment box (D14):
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

// the cheap, deterministic context tier (D21): last-touch + linked issues, fetched once per highlight
async function fetchCheapContext(hl) {
  const lr = hl.line_range;
  cheapCtx[hl.id] = "loading";
  try {
    cheapCtx[hl.id] = await fetch(
      `/api/sessions/${SID}/context?file=${encodeURIComponent(hl.file)}&start=${lr.start}&end=${lr.end}`
    ).then((r) => r.json());
  } catch (e) { cheapCtx[hl.id] = { blame: [], linked_issues: [] }; }
  if (selected && selected.kind === "hl" && selected.id === hl.id) renderDetail();
}

function cheapContextBlock(hl) {
  const c = cheapCtx[hl.id];
  if (c === undefined) { fetchCheapContext(hl); }
  const box = document.createElement("div");
  box.className = "cheapctx";
  if (c === undefined || c === "loading") { box.appendChild(empty("looking up context…")); return box; }
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
  const cell = $("diff").querySelector(`td.code[data-line="${hl.line_range.start}"]`);
  if (cell) cell.scrollIntoView({ block: "center", behavior: "smooth" });
}

function h2(t) { const e = document.createElement("h2"); e.textContent = t; return e; }
function h3(t) { const e = document.createElement("h3"); e.textContent = t; return e; }
function empty(t) { const e = document.createElement("div"); e.className = "empty"; e.textContent = t; return e; }
function btn(t, cls, fn) { const b = document.createElement("button"); b.className = cls; b.textContent = t; b.onclick = fn; return b; }

boot();
