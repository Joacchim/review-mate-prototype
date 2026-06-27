"use strict";
// review-mate diff browser — a thin surface. Presents session state, captures highlights; never
// reasons. Source of truth is the bridge-server; we re-fetch the snapshot on every WS event.

let SID = null;
let state = null;
let currentFile = null;
const collapsedDirs = new Set();
let splitMode = localStorage.getItem("rm-split") === "1";

const $ = (id) => document.getElementById(id);
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// --- boot -------------------------------------------------------------------

async function boot() {
  wireToolbar();
  const params = new URLSearchParams(location.search);
  SID = params.get("s");
  if (!SID) return showLanding();
  $("sid").textContent = SID.slice(0, 8);
  await load();
  connectWS();
}

function setStatus(msg) { $("status").textContent = msg || ""; }

async function showLanding() {
  $("mr").textContent = "—";
  $("diff").innerHTML = '<div class="land"><p class="empty">loading your review queue…</p></div>';
  let items = [];
  try { items = await fetch("/api/queue").then((r) => r.json()); } catch (e) {}
  if (items && items.error) items = [];
  const land = document.createElement("div");
  land.className = "land";
  land.innerHTML = `<h2>Pick a merge request</h2><p>Your review queue${
    items.length ? "" : " is empty here — paste an MR reference in the top bar (URL or group/proj!iid)."}</p>`;
  items.forEach((it) => {
    const b = document.createElement("button");
    b.className = "qitem";
    b.innerHTML = `<div class="t">${esc(it.title)}</div><div class="m">${esc(it.project)} !${it.iid}</div>`;
    b.onclick = () => loadRef(`${it.project}!${it.iid}`);
    land.appendChild(b);
  });
  const d = $("diff"); d.innerHTML = ""; d.appendChild(land);
  $("files").innerHTML = ""; $("rail").innerHTML = "";
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
    if (!r.ok) { setStatus("✕ " + (data.error || `load failed (${r.status})`)); return; }
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
  render();
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
  $("load").onclick = () => { const v = $("ref").value.trim(); loadRef(v); };
  $("ref").addEventListener("keydown", (e) => { if (e.key === "Enter") $("load").click(); });
}

function render() {
  $("mr").textContent = state.mr
    ? `${state.mr.project} !${state.mr.iid} — ${state.mr.title}` : "(no MR loaded)";
  renderTree();
  renderDiff();
  renderRail();
}

// --- file tree (nested, foldable) -------------------------------------------

function buildTree(files) {
  const root = { dirs: {}, files: [] };
  files.forEach((f) => {
    const parts = f.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      node.dirs[parts[i]] = node.dirs[parts[i]] || { dirs: {}, files: [] };
      node = node.dirs[parts[i]];
    }
    node.files.push({ name: parts[parts.length - 1], file: f });
  });
  return root;
}

function renderTree() {
  const el = $("files");
  el.innerHTML = "";
  if (!state.files.length) { el.innerHTML = '<div class="empty" style="padding:12px">no files</div>'; return; }
  const tree = buildTree(state.files);
  const wrap = document.createElement("div");
  wrap.className = "tree";
  renderNode(tree, "", 0, wrap);
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
  node.files.sort((a, b) => a.name.localeCompare(b.name)).forEach(({ name, file }) => {
    const row = document.createElement("div");
    row.className = "node file" + (file.path === currentFile ? " on" : "");
    row.style.paddingLeft = `${10 + depth * 14 + 14}px`;
    const c = (file.change_type || "")[0] || "~";
    row.innerHTML = `<span class="ct ${file.change_type || ""}">${c}</span>${esc(name)}`;
    row.onclick = () => { currentFile = file.path; render(); };
    out.appendChild(row);
  });
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

// --- rail (highlights + cards, access requests) -----------------------------

function renderRail() {
  const el = $("rail");
  el.innerHTML = "";
  el.appendChild(h3("Highlights & cards"));
  if (!state.highlights.length) el.appendChild(empty("highlight a line to ask for context"));
  state.highlights.forEach((hl, i) => {
    const card = state.cards.find((c) => c.highlight_id === hl.id);
    const lr = hl.line_range;
    const loc = `${hl.file}:${lr.start}${lr.end !== lr.start ? "-" + lr.end : ""}`;
    const box = document.createElement("div");
    box.className = "hcard";
    box.innerHTML =
      `<span class="num">#${i + 1}</span><button class="x" title="discard">×</button>` +
      `<div class="loc">${esc(loc)}</div>` +
      (hl.question ? `<div class="q">${esc(hl.question)}</div>` : "") +
      (card ? `<div class="card">${esc(card.body)}</div>`
            : `<div class="pending">waiting for context…</div>`);
    box.querySelector(".loc").onclick = () => goToHighlight(hl);
    box.querySelector(".x").onclick = () => post({ type: "remove_highlight", highlight_id: hl.id });
    el.appendChild(box);
  });

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
