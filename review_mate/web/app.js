"use strict";
// review-mate diff browser — a thin surface. Presents session state and captures highlights;
// it never reasons. State of truth is the bridge-server; we re-fetch the snapshot on every event.

let SID = null;
let state = null;
let currentFile = null;

const $ = (id) => document.getElementById(id);

async function boot() {
  const params = new URLSearchParams(location.search);
  SID = params.get("s");
  if (!SID) {
    const list = await fetch("/api/sessions").then((r) => r.json());
    if (list.length) SID = list[0].id;
    else SID = (await fetch("/api/sessions", { method: "POST" }).then((r) => r.json())).id;
  }
  $("sid").textContent = SID.slice(0, 8);
  await load();
  connectWS();
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
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(command),
  });
}

function render() {
  $("mr").textContent = state.mr
    ? `${state.mr.project} !${state.mr.iid} — ${state.mr.title}`
    : "(no MR loaded)";
  renderFiles();
  renderDiff();
  renderRail();
}

function renderFiles() {
  const el = $("files");
  el.innerHTML = "";
  state.files.forEach((f) => {
    const d = document.createElement("div");
    d.className = "f" + (f.path === currentFile ? " on" : "");
    d.innerHTML = `<span class="ct">${(f.change_type || "")[0] || "~"}</span>${f.path}`;
    d.onclick = () => { currentFile = f.path; render(); };
    el.appendChild(d);
  });
  if (!state.files.length) el.innerHTML = '<div class="empty" style="padding:12px">no files</div>';
}

function highlightsFor(file) {
  return state.highlights.filter((h) => h.file === file);
}

function renderDiff() {
  const el = $("diff");
  el.innerHTML = "";
  const file = state.files.find((f) => f.path === currentFile);
  if (!file) { el.innerHTML = '<div class="empty" style="padding:16px">select a file</div>'; return; }
  const name = document.createElement("div");
  name.className = "fname";
  name.textContent = file.path;
  el.appendChild(name);

  const hlLines = new Set(highlightsFor(file.path).map((h) => h.line_range.start));
  const table = document.createElement("table");
  table.className = "hunk";
  (file.hunks || []).forEach((h) => {
    let newLine = 0;
    (h.diff || "").split("\n").forEach((raw) => {
      if (raw === "") return;  // trailing element from the final newline — not a real diff row
      const tr = document.createElement("tr");
      if (raw.startsWith("@@")) {
        tr.className = "hh";
        const m = raw.match(/\+(\d+)/);
        if (m) newLine = parseInt(m[1], 10);
        tr.innerHTML = `<td class="ln"></td><td class="code">${escape(raw)}</td>`;
        table.appendChild(tr);
        return;
      }
      const kind = raw[0] === "+" ? "add" : raw[0] === "-" ? "del" : "ctx";
      const lineNo = kind === "del" ? "" : newLine;
      tr.className = "line " + kind + (hlLines.has(newLine) && kind !== "del" ? " hl" : "");
      tr.innerHTML = `<td class="ln">${lineNo}</td><td class="code">${escape(raw)}</td>`;
      if (kind !== "del") {
        const n = newLine, text = raw.slice(1);
        tr.onclick = () => post({
          type: "add_highlight", file: file.path, side: "new",
          line_range: { start: n, end: n }, anchor: text,
        });
        newLine += 1;
      }
      table.appendChild(tr);
    });
  });
  el.appendChild(table);
}

function renderRail() {
  const el = $("rail");
  el.innerHTML = "";

  el.appendChild(h3("Highlights & cards"));
  if (!state.highlights.length) el.appendChild(empty("highlight a line to ask for context"));
  state.highlights.forEach((hl) => {
    const card = state.cards.find((c) => c.highlight_id === hl.id);
    const box = document.createElement("div");
    box.className = "hcard";
    box.innerHTML =
      `<div class="loc">${hl.file}:${hl.line_range.start}</div>` +
      (hl.question ? `<div class="q">${escape(hl.question)}</div>` : "") +
      (card
        ? `<div class="card">${escape(card.body)}</div>`
        : `<div class="pending">waiting for context…</div>`);
    el.appendChild(box);
  });

  const pending = state.access_requests.filter((r) => r.status === "pending");
  el.appendChild(h3("Access requests"));
  if (!pending.length) el.appendChild(empty("none"));
  pending.forEach((r) => {
    const box = document.createElement("div");
    box.className = "req";
    box.innerHTML = `<div class="repo">${escape(r.repo)}</div><div class="why">${escape(r.reason)}</div>`;
    const ok = btn("Approve", "ok", () => post({ type: "decide_access", request_id: r.id, approve: true }));
    const no = btn("Deny", "no", () => post({ type: "decide_access", request_id: r.id, approve: false }));
    box.appendChild(ok); box.appendChild(no);
    el.appendChild(box);
  });
}

function h3(t) { const e = document.createElement("h3"); e.textContent = t; return e; }
function empty(t) { const e = document.createElement("div"); e.className = "empty"; e.textContent = t; return e; }
function btn(t, cls, fn) { const b = document.createElement("button"); b.className = cls; b.textContent = t; b.onclick = fn; return b; }
function escape(s) { return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

boot();
