"use strict";
// Building diff rows from the `diff` scope's hunks. Pure: hunks in, HTML out, no DOM and no
// derivation — line numbers, sides and token spans are all fields the server decided.
//
// Kinds are mapped to CSS classes here because a browser palette is this client's business, the
// same way the terminal client maps them to colour codes. An unknown kind renders plain, so the
// server's vocabulary can grow without this file changing.

const TOKEN_CLASS = {
  keyword: "tok-kw", string: "tok-str", docstring: "tok-str", comment: "tok-cmt",
  number: "tok-num", function: "tok-fn", class: "tok-type", type: "tok-type",
  decorator: "tok-kw", builtin: "tok-builtin", constant: "tok-num", namespace: "tok-type",
  tag: "tok-tag", attribute: "tok-attr", operator: "tok-op", deleted: "tok-del",
  inserted: "tok-ins", heading: "tok-fn",
};

function escHtml(s) {
  return (s == null ? "" : String(s)).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// one line's text with its token spans applied; the gaps between spans stay plain
function tokenSpans(text, tokens) {
  text = text || "";
  if (!tokens || !tokens.length) return escHtml(text);
  let out = "", cursor = 0;
  for (const [rawStart, length, kind] of [...tokens].sort((a, b) => a[0] - b[0])) {
    const start = Math.max(rawStart, cursor);
    const end = Math.min(start + length, text.length);
    if (end <= start) continue;
    if (start > cursor) out += escHtml(text.slice(cursor, start));
    const cls = TOKEN_CLASS[kind];
    const body = escHtml(text.slice(start, end));
    out += cls ? `<span class="${cls}">${body}</span>` : body;
    cursor = end;
  }
  if (cursor < text.length) out += escHtml(text.slice(cursor));
  return out;
}

const MARK = { added: "+", removed: "-", context: " " };
const KIND = { added: "add", removed: "del", context: "ctx" };

function hunkHeader(hunk, cells) {
  const text = escHtml(`@@ -${hunk.old_start},${hunk.old_count} +${hunk.new_start},${hunk.new_count} @@ ${hunk.heading || ""}`.trimEnd());
  const cell = `<td class="ln"></td><td class="code">${text}</td>`;
  return `<tr class="hh">${cells === 4 ? cell + cell : cell}</tr>`;
}

// unified: one row per diff line, new-side numbering, deletions unnumbered and unselectable
function unifiedRowsHtml(hunks, highlighted) {
  const hl = highlighted || new Set();
  let out = "";
  for (const hunk of hunks || []) {
    out += hunkHeader(hunk, 2);
    for (const line of hunk.lines || []) {
      const kind = KIND[line.side] || "ctx";
      const selectable = line.side !== "removed";
      const marked = selectable && line.new != null && hl.has(line.new);
      const body = escHtml(MARK[line.side] || " ") + tokenSpans(line.text, line.tokens);
      const attr = selectable && line.new != null ? ` data-line="${line.new}"` : "";
      out += `<tr class="line ${kind}${marked ? " hl" : ""}">`
           + `<td class="ln">${selectable && line.new != null ? line.new : ""}</td>`
           + `<td class="code"${attr}>${body}</td></tr>`;
    }
  }
  return out;
}

function cell(kind, number, text, tokens, dataLine, marked) {
  const lnClass = kind === "gap" ? " gap" : kind === "del" ? " delln" : kind === "add" ? " addln" : "";
  const codeClass = (kind === "gap" ? " gap" : kind === "del" ? " delc" : kind === "add" ? " addc" : "")
                  + (marked ? " hlc" : "");
  const attr = dataLine != null ? ` data-line="${dataLine}"` : "";
  const body = kind === "gap" ? "" : tokenSpans(text, tokens);
  return `<td class="ln${lnClass}">${number == null ? "" : number}</td>`
       + `<td class="code${codeClass}"${attr}>${body}</td>`;
}

// side-by-side: deletions and additions pair up, a missing counterpart becomes a gap cell
function splitRowsHtml(hunks, highlighted) {
  const hl = highlighted || new Set();
  let out = "";
  for (const hunk of hunks || []) {
    out += hunkHeader(hunk, 4);
    let pendingDel = [], pendingAdd = [];
    const flush = () => {
      const rows = Math.max(pendingDel.length, pendingAdd.length);
      for (let i = 0; i < rows; i++) {
        const d = pendingDel[i], a = pendingAdd[i];
        out += '<tr class="line">'
             + cell(d ? "del" : "gap", d ? d.old : null, d ? d.text : "", d ? d.tokens : null, null, false)
             + cell(a ? "add" : "gap", a ? a.new : null, a ? a.text : "", a ? a.tokens : null,
                    a ? a.new : null, a ? hl.has(a.new) : false)
             + "</tr>";
      }
      pendingDel = []; pendingAdd = [];
    };
    for (const line of hunk.lines || []) {
      if (line.side === "removed") { pendingDel.push(line); continue; }
      if (line.side === "added") { pendingAdd.push(line); continue; }
      flush();
      out += '<tr class="line">'
           + cell("ctx", line.old, line.text, line.tokens, null, false)
           + cell("ctx", line.new, line.text, line.tokens, line.new, hl.has(line.new))
           + "</tr>";
    }
    flush();
  }
  return out;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { tokenSpans, unifiedRowsHtml, splitRowsHtml, TOKEN_CLASS };
}
