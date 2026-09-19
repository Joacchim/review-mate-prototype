"use strict";
// Pure-JS checks for the browser's row builders, run under node by the pytest suite so the
// project keeps one gate.
const assert = require("node:assert");
const { test } = require("node:test");
const { tokenSpans, unifiedRowsHtml, splitRowsHtml } = require("../../review_mate/web/diffrows.js");

const hunk = {
  old_start: 1, old_count: 2, new_start: 1, new_count: 3, heading: "def f():", gap_before: 4,
  lines: [
    { side: "context", old: 1, new: 1, text: "def f():", tokens: [[0, 3, "keyword"]] },
    { side: "removed", old: 2, new: null, text: "    return 1", tokens: [[4, 6, "keyword"]] },
    { side: "added", old: null, new: 2, text: "    return 2", tokens: [[4, 6, "keyword"]] },
    { side: "added", old: null, new: 3, text: "    # done", tokens: [[4, 6, "comment"]] },
  ],
};

test("token spans wrap only what they cover", () => {
  assert.strictEqual(tokenSpans("def f", [[0, 3, "keyword"]]),
                     '<span class="tok-kw">def</span> f');
});

test("an unknown kind renders plain", () => {
  assert.strictEqual(tokenSpans("xy", [[0, 2, "no-such-kind"]]), "xy");
});

test("text is escaped, inside spans and out", () => {
  const html = tokenSpans("<a> & b", [[0, 3, "tag"]]);
  assert.ok(html.includes("&lt;a&gt;"));
  assert.ok(html.includes("&amp;"));
  assert.ok(!html.includes("<a>"));
});

test("a span running past the line is clamped", () => {
  assert.strictEqual(tokenSpans("ab", [[0, 99, "keyword"]]), '<span class="tok-kw">ab</span>');
});

test("unified rows number the new side and leave deletions unnumbered", () => {
  const html = unifiedRowsHtml([hunk]);
  const numbers = [...html.matchAll(/<td class="ln">([^<]*)<\/td>/g)].map((m) => m[1]);
  assert.deepStrictEqual(numbers, ["", "1", "", "2", "3"]);   // header, ctx, del, add, add
});

test("only selectable lines carry a data-line", () => {
  const html = unifiedRowsHtml([hunk]);
  assert.deepStrictEqual([...html.matchAll(/data-line="(\d+)"/g)].map((m) => m[1]), ["1", "2", "3"]);
});

test("a highlighted new-side line is marked", () => {
  const html = unifiedRowsHtml([hunk], new Set([2]));
  assert.ok(html.includes('class="line add hl"'));
  assert.strictEqual((html.match(/ hl"/g) || []).length, 1);
});

test("a deletion is never highlighted even at a matching number", () => {
  const html = unifiedRowsHtml([hunk], new Set([2]));
  assert.ok(!/class="line del hl"/.test(html));
});

test("split rows pair a deletion with an addition", () => {
  const html = splitRowsHtml([hunk]);
  const rows = html.split("<tr").filter((r) => r.includes('class="line"'));
  assert.strictEqual(rows.length, 3);              // ctx, then del+add paired, then add+gap
  assert.ok(rows[1].includes("delc") && rows[1].includes("addc"));
});

test("an unpaired addition gets a gap cell opposite", () => {
  const html = splitRowsHtml([hunk]);
  assert.ok(html.includes('class="code gap"'));
});

test("the hunk header shows both ranges", () => {
  assert.ok(unifiedRowsHtml([hunk]).includes("@@ -1,2 +1,3 @@ def f():"));
});

test("no hunks renders nothing", () => {
  assert.strictEqual(unifiedRowsHtml([]), "");
  assert.strictEqual(splitRowsHtml(undefined), "");
});
