"use strict";
// Making a rendered panel match a freshly built one by updating the nodes already there, rather
// than replacing them.
//
// The panels rebuild from their topic on every frame, which is the protocol working as designed —
// a client renders what a topic carries and derives nothing. Committing that rebuild by throwing
// the old DOM away is a separate choice, and it is the one that costs: focus, the caret, a
// selection being dragged, scroll position, a box resized by hand and anything else the browser
// keeps on a node go with it. Each was rediscovered as a bug and answered with a variable that
// saves and restores one of them.
//
// Reusing a node keeps all of it, and needs nobody to enumerate what "all of it" is.
//
// Children are matched by `data-key` where a builder supplies one, and by position otherwise.
// Position is correct for fixed layouts and wrong for a list that can reorder — a positional match
// across a reordered list would move one row's text into another row, which is worse than the
// problem being solved. So a list whose rows can move carries keys, and this file cannot tell the
// difference on its own.

function keyOf(node) {
  return node.nodeType === 1 && node.dataset && node.dataset.key !== undefined
    ? node.dataset.key : null;
}

// Whether two nodes are the same thing wearing different content — so one can become the other.
function compatible(a, b) {
  if (a.nodeType !== b.nodeType) return false;
  if (a.nodeType !== 1) return true;                    // text and comments: content only
  if (a.tagName !== b.tagName) return false;
  return keyOf(a) === keyOf(b);                         // both unkeyed, or the same key
}

function morphAttributes(el, next) {
  for (const attr of Array.from(el.attributes)) {
    // `style` is not declarative here: it is where the browser records a box the reviewer dragged
    // taller, and a builder that says nothing about it is not asking for it to be discarded
    if (attr.name !== "style" && !next.hasAttribute(attr.name)) el.removeAttribute(attr.name);
  }
  for (const attr of Array.from(next.attributes)) {
    if (el.getAttribute(attr.name) !== attr.value) el.setAttribute(attr.name, attr.value);
  }
}

// Handlers assigned as properties — `el.onclick = ...` — are the whole reason this is not simply
// an attribute copy. A reused node keeps the handlers it was built with, and those close over the
// state of the render that built them: the panel's own toggles would go on flipping a value from
// several frames ago, silently, while everything looked right. Listeners added with
// addEventListener are deliberately left alone: they belong to the surviving node and re-adding
// them would stack a second copy on every frame.
const HANDLERS = ["onclick", "oninput", "onchange", "onfocus", "onblur", "onkeydown", "onkeyup",
                  "onkeypress", "onmousedown", "onmouseup", "onmousemove", "onmouseover",
                  "onmouseleave", "onsubmit", "onscroll", "ondblclick"];

function morphHandlers(el, next) {
  for (const name of HANDLERS) if (el[name] !== next[name]) el[name] = next[name];
}

// Form state is not in an attribute, and assigning it is not free: writing `value` moves the caret
// even when the string is unchanged, which is the exact bug this file exists to stop. So each is
// written only when it actually differs.
function morphFormState(el, next) {
  if ("value" in el && typeof el.value === "string" && el.value !== next.value) el.value = next.value;
  if ("checked" in el && el.checked !== next.checked) el.checked = next.checked;
  if ("selected" in el && el.selected !== next.selected) el.selected = next.selected;
}

function morphChildren(el, next) {
  const byKey = new Map();                    // a keyed row is reused wherever it ended up
  for (const child of Array.from(el.childNodes)) {
    const k = keyOf(child);
    if (k !== null) byKey.set(k, child);
  }
  const used = new Set();
  let cursor = el.firstChild;
  for (const wanted of Array.from(next.childNodes)) {
    const k = keyOf(wanted);
    let match = null;
    if (k !== null) {
      match = byKey.get(k) || null;
    } else {
      let probe = cursor;                     // the next unclaimed, unkeyed node that can become it
      while (probe && (used.has(probe) || keyOf(probe) !== null)) probe = probe.nextSibling;
      if (probe && compatible(probe, wanted)) match = probe;
    }
    if (match === null) {
      used.add(wanted);
      el.insertBefore(wanted, cursor);        // genuinely new
      continue;
    }
    used.add(match);
    morphNode(match, wanted);
    if (match === cursor) cursor = cursor.nextSibling;
    else el.insertBefore(match, cursor);      // it exists, but not here
  }
  for (const child of Array.from(el.childNodes)) if (!used.has(child)) child.remove();
}

function morphNode(el, next) {
  if (el.nodeType !== 1) {
    if (el.nodeValue !== next.nodeValue) el.nodeValue = next.nodeValue;
    return;
  }
  morphAttributes(el, next);
  morphHandlers(el, next);
  morphChildren(el, next);
  // after the children, not before: a <select>'s value is one of its options, and assigning it
  // while the old options are still in place silently selects nothing
  morphFormState(el, next);
}

// Make `into` hold what `built` holds. Only the contents are touched — `into` is the page's own
// element and keeps its own id and classes. `built` is consumed: its nodes are moved, not copied.
function morph(into, built) {
  morphChildren(into, built);
}

window.morph = morph;
