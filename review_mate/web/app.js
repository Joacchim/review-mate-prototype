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
const fileContents = {};             // path -> content (cache for non-diff file views)
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

// --- boot -------------------------------------------------------------------

async function boot() {
  wireToolbar();
  const params = new URLSearchParams(location.search);
  SID = params.get("s");
  if (!SID) return showLanding();
  $("sid").textContent = SID.slice(0, 8);
  try { me = (await fetch("/api/me").then((r) => r.json())).username; } catch (e) { me = null; }
  await load();
  connectWS();
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

// the queue page doubles as the session hub: resume or close an in-flight review
function renderOpenSessions(land, sessions) {
  if (!sessions.length) return;
  const h = document.createElement("h2");
  h.textContent = "Open reviews";
  land.appendChild(h);
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
      const row = document.createElement("div");
      row.className = "sitem";
      row.innerHTML =
        `<button class="x" title="close review">×</button>` +
        `<div class="t">${esc(s.title || "(untitled review)")}</div>` +
        `<div class="m">${loc}${bits.length ? " · " + bits.join(" · ") : ""}</div>`;
      row.onclick = () => { location.search = `?s=${s.id}`; };
      row.querySelector(".x").onclick = (e) => {
        e.stopPropagation();
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
  renderOpenSessions(land, sessions.filter((s) => s.status === "active"));

  const head = document.createElement("div");
  head.innerHTML = `<h2>Pick a merge request</h2>`;
  land.appendChild(head);
  const queueBox = document.createElement("div");
  queueBox.appendChild(empty("loading your review queue…"));
  land.appendChild(queueBox);

  let items = [];
  try { items = await fetch("/api/queue").then((r) => r.json()); } catch (e) {}
  if (items && items.error) items = [];
  renderQueue(queueBox, items);
}

function renderQueue(box, items) {
  box.innerHTML = "";
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
    shown.forEach((it) => {
      const b = document.createElement("button");
      b.className = "qitem";
      b.innerHTML = `<div class="t">${esc(it.title)}</div><div class="m">${esc(it.project)} !${it.iid}</div>`;
      b.onclick = () => loadRef(`${it.project}!${it.iid}`);
      list.appendChild(b);
    });
  };
  filter.oninput = renderList;
  box.appendChild(filter);
  box.appendChild(list);
  renderList();
}

async function loadRef(ref) {
  const btn = $("load");
  btn.disabled = true; btn.textContent = "Loading…"; setStatus("resolving " + (ref || "(queue)") + "…");
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
  render();
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
  const brand = document.querySelector("header .brand");
  if (brand) { brand.style.cursor = "pointer"; brand.title = "back to the queue"; brand.onclick = () => { location.href = "/"; }; }
  $("t-left").onclick = () => $("shell").classList.toggle("hl");
  $("t-right").onclick = () => $("shell").classList.toggle("hr");
  $("t-split").classList.toggle("on", splitMode);
  $("t-split").onclick = () => {
    splitMode = !splitMode;
    localStorage.setItem("rm-split", splitMode ? "1" : "0");
    $("t-split").classList.toggle("on", splitMode);
    renderDiff();
  };
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
  let items = [];
  try { items = await fetch(`/api/search?q=${encodeURIComponent(query)}`).then((r) => r.json()); } catch (e) {}
  if (items && items.error) items = [];
  if ($("ref").value.trim() !== query) return;  // box moved on while we fetched
  list.innerHTML = "";
  items.forEach((it) => {
    const b = document.createElement("button");
    b.className = "qitem";
    b.innerHTML = `<div class="t">${esc(it.title)}</div><div class="m">${esc(it.project)} !${it.iid}</div>`;
    b.onclick = () => loadRef(`${it.project}!${it.iid}`);
    list.appendChild(b);
  });
  if (!items.length) list.appendChild(empty("no GitLab matches — try another term, or paste a full MR URL"));
  // the agent is a fallback, not the default: prompt it more strongly when direct search came up empty
  fallback.appendChild(document.createTextNode(items.length ? "Not the one? " : "Looking for it by description? "));
  fallback.appendChild(btn("✦ Ask Claude to find it", "btn ghost", () => askClaude(query, claudePanel)));
}

// route a fuzzy query to Claude's lookup channel; render its answer + loadable candidates
async function askClaude(query, panel) {
  panel.innerHTML = "";
  panel.appendChild(empty("asking Claude…"));
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
  (req.candidates || []).forEach((it) => {
    const b = document.createElement("button");
    b.className = "qitem";
    b.innerHTML = `<div class="t">${esc(it.title)}</div><div class="m">${esc(it.project)} !${it.iid}</div>`;
    b.onclick = () => loadRef(`${it.project}!${it.iid}`);
    panel.appendChild(b);
  });
}

function render() {
  $("mr").textContent = state.mr
    ? `${state.mr.project} !${state.mr.iid} — ${state.mr.title}` : "(no MR loaded)";
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

  const diffPaths = new Set(state.files.map((f) => f.path));
  const entries = state.files.map((f) => ({ path: f.path, change_type: f.change_type, diff: true }));
  if (showAll && repoTree) {
    repoTree.forEach((p) => { if (!diffPaths.has(p)) entries.push({ path: p, diff: false }); });
  }
  if (!entries.length) { el.appendChild(empty("no files")); return; }
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
    row.innerHTML = `<span class="ct ${entry.change_type || ""}">${c}</span>${esc(name)}`;
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

function highlightExact(path, lo, hi) {
  return state.highlights.find((h) => h.file === path && h.line_range.start === lo && h.line_range.end === hi);
}

function renderDiff() {
  const el = $("diff");
  el.innerHTML = "";
  if (viewingPath) { renderFileView(el, viewingPath); return; }
  const file = state.files.find((f) => f.path === currentFile);
  if (!file) { el.innerHTML = '<div class="empty" style="padding:16px">select a file</div>'; return; }
  const name = document.createElement("div");
  name.className = "fname"; name.textContent = file.path + "  ·  click a line, or drag to select a block";
  el.appendChild(name);
  const hl = highlightLines(file.path);
  const table = document.createElement("table");
  table.className = "hunk";
  (file.hunks || []).forEach((h) => {
    (splitMode ? splitRows : unifiedRows)(h.diff || "", hl, table);
  });
  wireSelection(table, file.path);
  el.appendChild(table);
}

function renderFileView(el, path) {
  const name = document.createElement("div");
  name.className = "fname";
  name.textContent = path + "  ·  related file (not in the diff) · click or drag to ask for context";
  el.appendChild(name);
  const content = fileContents[path];
  if (content === undefined) { el.appendChild(empty("loading " + path + "…")); return; }
  const hl = highlightLines(path);
  const table = document.createElement("table");
  table.className = "hunk";
  content.split("\n").forEach((line, i) => {
    const n = i + 1;
    const tr = document.createElement("tr");
    tr.className = "line ctx" + (hl.has(n) ? " hl" : "");
    tr.innerHTML = `<td class="ln">${n}</td><td class="code" data-line="${n}">${esc(line)}</td>`;
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

function unifiedRows(diff, hl, table) {
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
    const code = `<td class="code"${kind !== "del" ? ` data-line="${newLine}"` : ""}>${esc(raw)}</td>`;
    tr.innerHTML = `<td class="ln">${kind === "del" ? "" : newLine}</td>${code}`;
    if (kind !== "del") newLine += 1;
    table.appendChild(tr);
  });
}

function splitRows(diff, hl, table) {
  let oldLine = 0, newLine = 0;
  let pendDel = [], pendAdd = [];
  const flush = () => {
    const n = Math.max(pendDel.length, pendAdd.length);
    for (let i = 0; i < n; i++) {
      const d = pendDel[i], a = pendAdd[i];
      const tr = document.createElement("tr"); tr.className = "line";
      appendCell(tr, d ? "del" : "gap", d ? d.ln : "", d ? d.text : "", null, false);
      appendCell(tr, a ? "add" : "gap", a ? a.ln : "", a ? a.text : "", a ? a.ln : null, a && hl.has(a.ln));
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
      appendCell(tr, "ctx", oldLine, txt, null, false);
      appendCell(tr, "ctx", newLine, txt, newLine, hl.has(newLine));
      table.appendChild(tr); oldLine += 1; newLine += 1;
    }
  });
  flush();
}

function appendCell(tr, kind, ln, text, dataLine, isHl) {
  const tdLn = document.createElement("td");
  tdLn.className = "ln" + (kind === "gap" ? " gap" : kind === "del" ? " delln" : kind === "add" ? " addln" : "");
  tdLn.textContent = ln === "" ? "" : ln;
  const tdCode = document.createElement("td");
  tdCode.className = "code" + (kind === "gap" ? " gap" : kind === "del" ? " delc" : kind === "add" ? " addc" : "")
                   + (isHl ? " hlc" : "");
  tdCode.textContent = text || "";
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
             : st === "context" ? (card ? "context ready" : "waiting for context…")
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
      const p = document.createElement("div");
      p.className = "pending"; p.textContent = "waiting for Claude's context…";
      el.appendChild(p);
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
  const threads = state.threads || [];
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
  row.className = "hrow" + (active ? " active" : "");
  row.innerHTML =
    `<div class="top">${chip}<span class="loc">${esc(loc)}</span>` +
    (t.comments && t.comments.length > 1 ? `<span class="num">${t.comments.length}</span>` : "") +
    `</div><div class="prev">${esc(prev)}</div>`;
  row.onclick = () => { selected = { kind: "thread", id: t.id }; renderRail(); };
  const m = matchingHighlight(t);   // anchored to a highlight → offer a jump
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
  if (await threadAction("refresh-threads", {}, "discussions refreshed")) await load();
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
  if (!reviewStatus || !reviewStatus.behind) return;
  const bar = document.createElement("div");
  bar.className = "verbanner";
  bar.appendChild(document.createTextNode("Updated since your last review"));
  bar.appendChild(btn("Mark reviewed", "btn ghost", markReviewed));
  el.appendChild(bar);
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

function h3(t) { const e = document.createElement("h3"); e.textContent = t; return e; }
function empty(t) { const e = document.createElement("div"); e.className = "empty"; e.textContent = t; return e; }
function btn(t, cls, fn) { const b = document.createElement("button"); b.className = cls; b.textContent = t; b.onclick = fn; return b; }

boot();
