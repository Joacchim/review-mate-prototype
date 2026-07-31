# Light Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use shadowpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a light theme to review-mate's web UI that follows the OS's `prefers-color-scheme` by default, with a manual header-button override that persists across reloads.

**Architecture:** Every color in `review_mate/web/index.html`'s inline `<style>` block becomes (or already is) a CSS custom property on `:root`; a `:root[data-theme="light"]` block overrides all of them. `document.documentElement.dataset.theme` is set by a no-flash inline `<script>` in `<head>` (before first paint) and kept in sync by `app.js` (theme-toggle click + live OS-preference changes).

**Tech Stack:** Plain HTML/CSS/JS, no build step, no frameworks — matches the existing codebase exactly. No frontend test harness exists in this repo (confirmed: no Playwright/Puppeteer/jsdom, no `tests/` coverage of `web/`), so verification in this plan is mechanical grep checks (for the CSS migration, which has an exact right answer) plus manual browser QA (for visual/behavioral correctness) — not invented unit tests.

**Key Integration Points:**
- `review_mate/web/index.html:8-12` — the existing `:root{...}` variable block (14 vars) that every other rule already reads from.
- `review_mate/web/index.html:23-233` — ~20 individual rules that hardcode colors directly instead of using a variable; each must be repointed at a new variable.
- `review_mate/web/index.html:238-249` — the `<header>` toggle-button group (`t-left`, `t-split`, `t-right`, `t-commits`) whose exact markup/CSS pattern the new `t-theme` button must match.
- `review_mate/web/app.js:9-20` (module-level state) and `:588-597` (button wiring) — where `splitMode`/`rm-split` is read, applied, and persisted; the new theme toggle follows this pattern exactly.

Design reference: [docs/superpowers/specs/2026-07-31-light-mode-design.md](../specs/2026-07-31-light-mode-design.md) — every hex value and variable name below is taken verbatim from that approved spec.

---

### Task 1: Add the CSS variable scaffolding (dark defaults + light overrides)

**Files:**
- Modify: `review_mate/web/index.html:8-12` — extend the existing `:root{...}` block with 21 new variables (dark values), and add a new `:root[data-theme="light"]{...}` block right after it with all 35 variables' light values.

**Integration:** This task only adds variable *definitions*. Nothing consumes the 21 new ones yet (Tasks 2-5 do that), so there is no visual change to verify beyond "the page still loads" — the new variables are inert until referenced.

- [ ] **Step 1: Read the current block to confirm it's unchanged since the design spec was written**

Run: `sed -n '7,13p' review_mate/web/index.html`

Expected output:
```
<style>
  :root{
    --bg:#0f1117; --panel:#171a23; --panel2:#1c2030; --line:#272b3a; --line2:#33384a; --fg:#e6e8ee;
    --fg2:#9aa3b2; --accent:#6ea8fe; --add:#10301f; --addln:#1f5135; --addfg:#9be8bf;
    --del:#34141a; --delln:#5a2230; --delfg:#f3aab4; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  *{box-sizing:border-box} html,body{margin:0;height:100%}
```

If this doesn't match, stop and re-diff against the design spec before continuing — the line-numbered edits below assume this exact text.

- [ ] **Step 2: Replace the block with the extended dark block + new light block**

Use the Edit tool on `review_mate/web/index.html`:

old_string:
```
  :root{
    --bg:#0f1117; --panel:#171a23; --panel2:#1c2030; --line:#272b3a; --line2:#33384a; --fg:#e6e8ee;
    --fg2:#9aa3b2; --accent:#6ea8fe; --add:#10301f; --addln:#1f5135; --addfg:#9be8bf;
    --del:#34141a; --delln:#5a2230; --delfg:#f3aab4; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
```

new_string:
```
  :root{
    --bg:#0f1117; --panel:#171a23; --panel2:#1c2030; --line:#272b3a; --line2:#33384a; --fg:#e6e8ee;
    --fg2:#9aa3b2; --accent:#6ea8fe; --add:#10301f; --addln:#1f5135; --addfg:#9be8bf;
    --del:#34141a; --delln:#5a2230; --delfg:#f3aab4; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
    --ok:#3ddc84; --warn:#d0a24a; --warnfg:#f0c88a; --warnbg:rgba(208,162,74,.1); --warnline:#7a5a2a;
    --stalebg:#5a3a1a; --stalefg:#f0c890;
    --purple:#c9a2ef; --purpleln:#a56ef0; --purplebg:rgba(201,162,239,.28);
    --accentline:#3a4a6b; --accentbg:rgba(110,168,254,.13); --accentborder:rgba(110,168,254,.4);
    --accentglow:rgba(110,168,254,.22); --fg2bg:rgba(154,163,178,.08);
    --postline:#2e6b45; --delline:#6b2e38; --usermsgbg:#1d2a44;
    --tokcmt:#7f8aa3; --tokstr:#8fdcb0; --toknum:#e0b892; --shadow:rgba(0,0,0,.55);
  }
  :root[data-theme="light"]{
    --bg:#ffffff; --panel:#f6f8fa; --panel2:#eaeef2; --line:#d0d7de; --line2:#afb8c1; --fg:#1b1f24;
    --fg2:#59636e; --accent:#0969da; --add:#dafbe1; --addln:#aceebb; --addfg:#116329;
    --del:#ffebe9; --delln:#ffc1ba; --delfg:#82071e;
    --ok:#1a7f37; --warn:#9a6700; --warnfg:#7d4e00; --warnbg:rgba(154,103,0,.12); --warnline:#d4a72c;
    --stalebg:#fff8c5; --stalefg:#7d4e00;
    --purple:#8250df; --purpleln:#6639ba; --purplebg:rgba(130,80,223,.16);
    --accentline:#c3d2e8; --accentbg:rgba(9,105,218,.08); --accentborder:rgba(9,105,218,.35);
    --accentglow:rgba(9,105,218,.18); --fg2bg:rgba(89,99,110,.08);
    --postline:#1a7f37; --delline:#cf222e; --usermsgbg:#ddf4ff;
    --tokcmt:#6e7781; --tokstr:#116329; --toknum:#953800; --shadow:rgba(0,0,0,.16);
  }
```

- [ ] **Step 3: Verify both blocks define the same set of variables**

Run:
```bash
python3 -c "
import re
text = open('review_mate/web/index.html').read()
dark = re.search(r':root\{(.*?)\}', text, re.S).group(1)
light = re.search(r':root\[data-theme=\"light\"\]\{(.*?)\}', text, re.S).group(1)
dark_vars = set(re.findall(r'--([\w-]+):', dark)) - {'mono'}
light_vars = set(re.findall(r'--([\w-]+):', light))
print('dark-only (should be empty):', dark_vars - light_vars)
print('light-only (should be empty):', light_vars - dark_vars)
print('dark count:', len(dark_vars), 'light count:', len(light_vars))
"
```

Expected: both "should be empty" lines print `set()`, and both counts print the same number (35).

- [ ] **Step 4: Commit**

```bash
git add review_mate/web/index.html
git commit -m "feat(web): add light-theme CSS variable set"
```

---

### Task 2: Migrate agent-status and rebase-note colors to variables

**Files:**
- Modify: `review_mate/web/index.html:23-25` — the three agent-status-dot/label rules.
- Modify: `review_mate/web/index.html:67` — `.sincenote` (the "diff since last push" banner).

**Integration:** These rules render in the header's agent indicator (`#agent .dot`/`.alab`) and above the diff when reviewing "since last push". After this task, both read `--warn`/`--warnfg`/`--warnbg`/`--warnline`/`--ok` instead of hardcoded hex — so they'll already flip correctly once Task 6/7 make the theme switchable, even though nothing switches it yet.

- [ ] **Step 1: Replace the three agent-status rules**

old_string:
```
  header .agent.watching .dot{background:#3ddc84}
  header .agent.working .dot{background:#d0a24a;animation:apulse 1.1s ease-in-out infinite}
  header .agent.working .alab{color:#f0c88a}
```

new_string:
```
  header .agent.watching .dot{background:var(--ok)}
  header .agent.working .dot{background:var(--warn);animation:apulse 1.1s ease-in-out infinite}
  header .agent.working .alab{color:var(--warnfg)}
```

- [ ] **Step 2: Replace `.sincenote`**

old_string:
```
  .sincenote{margin:10px 14px 0;padding:6px 10px;font-size:11.5px;color:#f0c88a;background:rgba(208,162,74,.1);border:1px solid #7a5a2a;border-radius:6px}
```

new_string:
```
  .sincenote{margin:10px 14px 0;padding:6px 10px;font-size:11.5px;color:var(--warnfg);background:var(--warnbg);border:1px solid var(--warnline);border-radius:6px}
```

- [ ] **Step 3: Verify no hardcoded hex remains in these four rules**

Run: `grep -n "agent.watching .dot\|agent.working\|sincenote" review_mate/web/index.html`

Expected: all four matched lines contain only `var(--...)` for color values — no `#` or `rgba(` literals.

- [ ] **Step 4: Commit**

```bash
git add review_mate/web/index.html
git commit -m "feat(web): theme the agent-status indicator and since-push banner"
```

---

### Task 3: Migrate discussion-purple accent and syntax-token colors to variables

**Files:**
- Modify: `review_mate/web/index.html:84-87` — the host-discussion box-shadow/flash-highlight rules and the four `.tok-*` syntax-highlight rules.
- Modify: `review_mate/web/index.html:161` — `.rdiff .rd-cmod` (the rebase-interdiff "modified commit" marker).
- Modify: `review_mate/web/index.html:227` — `.sitem.st-merged` (the review-hub "merged" state marker).

**Integration:** `.tok-*` colors render inside every diff line via the JS tokenizer's output classes (`.tok-cmt`/`.tok-str`/`.tok-num`/`.tok-kw`) — the highest-traffic rule in this task, visible on almost every screen.

- [ ] **Step 1: Replace the discussion/flash/token rules**

old_string:
```
  tr.thl td.code{box-shadow:inset -3px 0 0 #c9a2ef}                    /* a host discussion anchors here */
  tr.hl.thl td.code{box-shadow:inset 3px 0 0 var(--accent), inset -3px 0 0 #c9a2ef}   /* both */
  tr.flash td.code{background:rgba(201,162,239,.28)}                    /* jumped-to discussion line */
  .tok-cmt{color:#7f8aa3;font-style:italic} .tok-str{color:#8fdcb0} .tok-num{color:#e0b892} .tok-kw{color:#c9a2ef}
```

new_string:
```
  tr.thl td.code{box-shadow:inset -3px 0 0 var(--purple)}                    /* a host discussion anchors here */
  tr.hl.thl td.code{box-shadow:inset 3px 0 0 var(--accent), inset -3px 0 0 var(--purple)}   /* both */
  tr.flash td.code{background:var(--purplebg)}                    /* jumped-to discussion line */
  .tok-cmt{color:var(--tokcmt);font-style:italic} .tok-str{color:var(--tokstr)} .tok-num{color:var(--toknum)} .tok-kw{color:var(--purple)}
```

- [ ] **Step 2: Replace `.rdiff .rd-cmod`**

old_string:
```
  .rdiff .rd-cmod{border-left-color:#d0a24a;color:#f0c88a}
```

new_string:
```
  .rdiff .rd-cmod{border-left-color:var(--warn);color:var(--warnfg)}
```

- [ ] **Step 3: Replace `.sitem.st-merged`**

old_string:
```
  .sitem.st-merged{border-left:3px solid #a56ef0} .sitem.st-merged .ststate{color:#c9a2ef;border-color:#5a3a8a}
```

new_string:
```
  .sitem.st-merged{border-left:3px solid var(--purpleln)} .sitem.st-merged .ststate{color:var(--purple);border-color:var(--purpleln)}
```

- [ ] **Step 4: Verify**

Run: `grep -n "tok-cmt\|tok-str\|tok-num\|tok-kw\|rd-cmod\|st-merged\|tr\.thl\|tr\.flash" review_mate/web/index.html`

Expected: every color/background/box-shadow value in the matched lines is `var(--...)` — no hex or rgba literals.

- [ ] **Step 5: Commit**

```bash
git add review_mate/web/index.html
git commit -m "feat(web): theme syntax highlighting and discussion/merged-state accents"
```

---

### Task 4: Migrate accent-border, chip, and verbanner colors to variables

**Files:**
- Modify: `review_mate/web/index.html:103` — `.hrow.agent`.
- Modify: `review_mate/web/index.html:116-117` — `.chip.comment`/`.chip.posted`/`.chip.insight`.
- Modify: `review_mate/web/index.html:133` — `.detail .byclaude`.
- Modify: `review_mate/web/index.html:148,150,151,153` — `.verbanner`, `.vblabel::before`, `.verbanner.baseline`, `.chip.stale`.
- Modify: `review_mate/web/index.html:207-208` — `.claudeans`, `.claudeans .byclaude`.

**Integration:** These render in the highlight rail (`.hrow`), chat/insight chips, the diff-version banner (`.verbanner`, shown above the diff when reviewing an older push), and the Claude-answer panel (`.claudeans`) on the landing page.

- [ ] **Step 1: Replace `.hrow.agent`**

old_string:
```
  .hrow.agent{border-color:#3a4a6b}
```

new_string:
```
  .hrow.agent{border-color:var(--accentline)}
```

- [ ] **Step 2: Replace the chip rules**

old_string:
```
  .chip.comment{color:var(--accent);border-color:#3a4a6b} .chip.posted{color:var(--addfg);border-color:#2e6b45}
  .chip.insight{color:var(--accent);border-color:#3a4a6b}
```

new_string:
```
  .chip.comment{color:var(--accent);border-color:var(--accentline)} .chip.posted{color:var(--addfg);border-color:var(--postline)}
  .chip.insight{color:var(--accent);border-color:var(--accentline)}
```

- [ ] **Step 3: Replace `.detail .byclaude`**

old_string:
```
  .detail .byclaude{display:inline-block;font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--accent);border:1px solid #3a4a6b;border-radius:5px;padding:0 5px}
```

new_string:
```
  .detail .byclaude{display:inline-block;font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--accent);border:1px solid var(--accentline);border-radius:5px;padding:0 5px}
```

- [ ] **Step 4: Replace the verbanner block**

old_string:
```
  .verbanner{display:flex;align-items:center;justify-content:space-between;gap:8px;background:rgba(110,168,254,.13);border:1px solid rgba(110,168,254,.4);border-left:3px solid var(--accent);border-radius:8px;padding:9px 12px;margin-bottom:10px;font-size:12px}
  .verbanner .vblabel{font-weight:600;color:var(--fg);display:flex;align-items:center;gap:7px}
  .verbanner .vblabel::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px rgba(110,168,254,.22);flex:none}
  .verbanner.baseline{background:rgba(154,163,178,.08);border-color:var(--line2);border-left-color:var(--line2)}
  .verbanner.baseline .vblabel{color:var(--fg2);font-weight:500} .verbanner.baseline .vblabel::before{background:var(--fg2);box-shadow:none}
  .chip.stale{background:#5a3a1a;color:#f0c890;border-color:#7a5a2a}
```

new_string:
```
  .verbanner{display:flex;align-items:center;justify-content:space-between;gap:8px;background:var(--accentbg);border:1px solid var(--accentborder);border-left:3px solid var(--accent);border-radius:8px;padding:9px 12px;margin-bottom:10px;font-size:12px}
  .verbanner .vblabel{font-weight:600;color:var(--fg);display:flex;align-items:center;gap:7px}
  .verbanner .vblabel::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px var(--accentglow);flex:none}
  .verbanner.baseline{background:var(--fg2bg);border-color:var(--line2);border-left-color:var(--line2)}
  .verbanner.baseline .vblabel{color:var(--fg2);font-weight:500} .verbanner.baseline .vblabel::before{background:var(--fg2);box-shadow:none}
  .chip.stale{background:var(--stalebg);color:var(--stalefg);border-color:var(--warnline)}
```

- [ ] **Step 5: Replace the `.claudeans` rules**

old_string:
```
  .claudeans{background:var(--panel2);border:1px solid #3a4a6b;border-radius:8px;padding:10px 12px;margin-bottom:8px}
  .claudeans .byclaude{display:inline-block;font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--accent);border:1px solid #3a4a6b;border-radius:5px;padding:0 5px;margin-bottom:6px}
```

new_string:
```
  .claudeans{background:var(--panel2);border:1px solid var(--accentline);border-radius:8px;padding:10px 12px;margin-bottom:8px}
  .claudeans .byclaude{display:inline-block;font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--accent);border:1px solid var(--accentline);border-radius:5px;padding:0 5px;margin-bottom:6px}
```

- [ ] **Step 6: Verify**

Run: `grep -n "#3a4a6b\|#2e6b45\|#5a3a1a\|#f0c890\|rgba(110,168,254\|rgba(154,163,178" review_mate/web/index.html`

Expected: the only matches left are inside the two `:root` blocks from Task 1 (lines containing `--accentline:`, `--postline:`, `--stalebg:`, `--stalefg:`, `--accentbg:`, `--accentborder:`, `--accentglow:`, `--fg2bg:`) — zero matches in any other rule.

- [ ] **Step 7: Commit**

```bash
git add review_mate/web/index.html
git commit -m "feat(web): theme highlight-rail, chip, verbanner, and claude-answer accents"
```

---

### Task 5: Migrate remaining misc colors and dedupe the error/reject-button reds

**Files:**
- Modify: `review_mate/web/index.html:128` — `.detail` box-shadow.
- Modify: `review_mate/web/index.html:171` — `.searcherr` (search-error box).
- Modify: `review_mate/web/index.html:180` — `button.ok`/`button.no` (cross-repo access-request buttons).
- Modify: `review_mate/web/index.html:190` — `.msg.user` (your own chat bubble).

**Integration:** `.searcherr` and `button.no` switch to reusing `--del`/`--delln`/`--delfg`/`--delline` — the same "error red" the diff view already uses — rather than keeping their own hardcoded copies; this is the one intentional dedup called out in the design spec.

- [ ] **Step 1: Replace the `.detail` box-shadow**

old_string:
```
    background:var(--panel);border:1px solid var(--line2);border-radius:10px;box-shadow:0 16px 48px rgba(0,0,0,.55);padding:14px 16px;z-index:40}
```

new_string:
```
    background:var(--panel);border:1px solid var(--line2);border-radius:10px;box-shadow:0 16px 48px var(--shadow);padding:14px 16px;z-index:40}
```

- [ ] **Step 2: Replace `.searcherr`**

old_string:
```
  .searcherr{background:#5a1f1f;border:1px solid #7a3030;color:#f0c0c0;border-radius:6px;padding:9px 11px;font-size:12.5px;line-height:1.5}
```

new_string:
```
  .searcherr{background:var(--del);border:1px solid var(--delln);color:var(--delfg);border-radius:6px;padding:9px 11px;font-size:12.5px;line-height:1.5}
```

- [ ] **Step 3: Replace the `button.ok`/`button.no` rule**

old_string:
```
  .req button{margin-right:6px} button.ok{border-color:#2e6b45;color:var(--addfg)} button.no{border-color:#6b2e38;color:var(--delfg)}
```

new_string:
```
  .req button{margin-right:6px} button.ok{border-color:var(--postline);color:var(--addfg)} button.no{border-color:var(--delline);color:var(--delfg)}
```

- [ ] **Step 4: Replace `.msg.user`**

old_string:
```
  .msg.user{background:#1d2a44;margin-left:22px} .msg.agent{background:var(--panel2);margin-right:22px}
```

new_string:
```
  .msg.user{background:var(--usermsgbg);margin-left:22px} .msg.agent{background:var(--panel2);margin-right:22px}
```

- [ ] **Step 5: Verify every hardcoded color literal outside the two `:root` blocks is gone**

Run:
```bash
python3 -c "
import re
text = open('review_mate/web/index.html').read()
style = re.search(r'<style>(.*?)</style>', text, re.S).group(1)
style = re.sub(r':root\{.*?\}', '', style, flags=re.S)
style = re.sub(r':root\[data-theme=\"light\"\]\{.*?\}', '', style, flags=re.S)
leftovers = re.findall(r'#[0-9a-fA-F]{3,8}|rgba?\([^)]+\)', style)
print('leftover literals:', leftovers)
"
```

Expected: `leftover literals: []`

- [ ] **Step 6: Commit**

```bash
git add review_mate/web/index.html
git commit -m "feat(web): theme detail-panel shadow, search error, and reject-button colors"
```

---

### Task 6: Add the no-flash bootstrap script and the header toggle button

**Files:**
- Modify: `review_mate/web/index.html:235-236` — insert an inline `<script>` between `</style>` and `</head>`.
- Modify: `review_mate/web/index.html:241-244` — insert the `#t-theme` button into the header's toggle-button group.

**Integration:** The bootstrap script sets `document.documentElement.dataset.theme` before `app.js` loads (avoiding a flash of the wrong theme on reload); the button gives the reviewer a manual override. Neither does anything meaningful yet — `app.js` (Task 7) is what makes the button clickable and keeps the bootstrap script's decision in sync.

- [ ] **Step 1: Confirm the current head/header markup matches what this step edits**

Run: `sed -n '235,244p' review_mate/web/index.html`

Expected:
```
</style>
</head>
<body>
<header>
  <span class="agent off" id="agent"><span class="dot"></span><span class="alab"></span></span>
  <a class="brand" href="/" title="back to the queue">review-mate</a>
  <button class="btn" id="t-left" title="toggle file tree">◧</button>
  <button class="btn" id="t-split" title="unified / side-by-side">⇄ split</button>
  <button class="btn" id="t-right" title="toggle context panel">◨</button>
  <button class="btn" id="t-commits" title="review per commit">⑃ commits</button>
```

- [ ] **Step 2: Insert the bootstrap script**

old_string:
```
</style>
</head>
```

new_string:
```
</style>
<script>
  (function () {
    var stored = localStorage.getItem("rm-theme");
    var light = stored ? stored === "light" : matchMedia("(prefers-color-scheme: light)").matches;
    if (light) document.documentElement.dataset.theme = "light";
  })();
</script>
</head>
```

- [ ] **Step 3: Insert the toggle button**

old_string:
```
  <button class="btn" id="t-commits" title="review per commit">⑃ commits</button>
```

new_string:
```
  <button class="btn" id="t-commits" title="review per commit">⑃ commits</button>
  <button class="btn" id="t-theme" title="toggle light / dark theme">☾</button>
```

- [ ] **Step 4: Verify in a browser that dark mode is still pixel-identical to before this task**

Run: `uv run review-mate` (starts the server at http://127.0.0.1:8765)

Open <http://127.0.0.1:8765> in a browser with the OS in dark mode (or no theme preference). Confirm:
- The page looks exactly as it did before this plan (dark background, same colors) — the bootstrap script should leave `data-theme` unset, so nothing changes yet.
- A new "☾" button appears in the header after "⑃ commits". Clicking it does nothing yet (that's Task 7) — this is expected.

Stop the server with Ctrl-C when done.

- [ ] **Step 5: Commit**

```bash
git add review_mate/web/index.html
git commit -m "feat(web): add theme bootstrap script and header toggle button"
```

---

### Task 7: Wire the theme toggle and live OS-preference tracking in app.js

**Files:**
- Modify: `review_mate/web/app.js:9-20` — add an `applyTheme()` function and a `matchMedia` change listener alongside the existing `splitMode`/`showAll` state.
- Modify: `review_mate/web/app.js:588-597` — call `applyTheme()` on load and wire the `#t-theme` click handler, in the same block that wires `t-left`/`t-split`/`t-commits`.

**Integration:** This is what makes the Task 6 button and bootstrap script actually functional. `applyTheme()` is the single source of truth for both the DOM attribute and the button's glyph — it's called on load, on every click, and on every OS-preference change (when the reviewer has no manual override stored).

- [ ] **Step 1: Read the current state-declaration block to confirm line numbers**

Run: `sed -n '1,22p' review_mate/web/app.js`

Confirm lines 9 and 15 are:
```
let splitMode = localStorage.getItem("rm-split") === "1";
```
and
```
let showAll = localStorage.getItem("rm-showall") === "1";
```

- [ ] **Step 2: Add `applyTheme()` and the OS-preference listener**

old_string:
```
let splitMode = localStorage.getItem("rm-split") === "1";
```

new_string:
```
let splitMode = localStorage.getItem("rm-split") === "1";

function applyTheme() {
  const stored = localStorage.getItem("rm-theme");
  const light = stored ? stored === "light" : matchMedia("(prefers-color-scheme: light)").matches;
  document.documentElement.dataset.theme = light ? "light" : "";
  $("t-theme").textContent = light ? "☀" : "☾";
}
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
  if (!localStorage.getItem("rm-theme")) applyTheme();
});
```

- [ ] **Step 3: Read the button-wiring block to confirm line numbers**

Run: `sed -n '585,598p' review_mate/web/app.js`

Confirm it contains:
```
  $("t-left").onclick = () => $("shell").classList.toggle("hl");
  $("t-right").onclick = () => $("shell").classList.toggle("hr");
  $("t-split").classList.toggle("on", splitMode);
  $("t-split").onclick = () => {
```

- [ ] **Step 4: Wire the button and call `applyTheme()` on load**

old_string:
```
  $("t-left").onclick = () => $("shell").classList.toggle("hl");
  $("t-right").onclick = () => $("shell").classList.toggle("hr");
```

new_string:
```
  $("t-left").onclick = () => $("shell").classList.toggle("hl");
  $("t-right").onclick = () => $("shell").classList.toggle("hr");
  applyTheme();
  $("t-theme").onclick = () => {
    const light = document.documentElement.dataset.theme !== "light";
    localStorage.setItem("rm-theme", light ? "light" : "dark");
    applyTheme();
  };
```

- [ ] **Step 5: Verify manually in the browser**

Run: `uv run review-mate`

Open <http://127.0.0.1:8765>. Confirm, in order:
1. The page loads in your OS's current color scheme (flip your OS setting and reload to confirm both directions) — no flash of the wrong theme.
2. Click "☾"/"☀" in the header. The whole UI (background, diff add/delete colors, chips, chat bubbles, syntax highlighting if you open a file with code) switches theme immediately, and the button's glyph flips too.
3. Reload the page. The manually-chosen theme persists (does *not* revert to OS preference).
4. Open devtools → Application → Local Storage, and delete the `rm-theme` key. Reload — the page now follows OS preference again.
5. With `rm-theme` still unset, flip your OS's color-scheme setting *without reloading the page*. The UI should switch live (the `matchMedia` change listener firing).

Stop the server with Ctrl-C when done.

- [ ] **Step 6: Commit**

```bash
git add review_mate/web/app.js
git commit -m "feat(web): wire the theme toggle and OS-preference tracking"
```

---

### Task 8: Full visual regression pass across every screen

**Files:** none (verification-only task; fixes go back into whichever Task 2-5 rule was wrong, then re-run this task).

**Integration:** Tasks 1-7 touched every color-bearing rule in the stylesheet individually; this task is the only point where every screen gets looked at together, in both themes, to catch anything a single-rule diff couldn't (e.g. two colors that read fine alone but clash together, or contrast that's technically "themed" but hard to read).

- [ ] **Step 1: Start the server and open the landing page in both themes**

Run: `uv run review-mate`

Open <http://127.0.0.1:8765>. Toggle the theme with "☾"/"☀". In both themes, confirm the landing page's review queue/search list is fully readable: state chips (`st-merged`/`st-closed`/`st-git`/`st-disc`/`st-prog`/`st-done`/`st-new`), any Claude-answer panel (`.claudeans`), and the search-error box (trigger it by searching for something that 404s, if you have a GitLab project loaded — see Task 5's `.searcherr`).

- [ ] **Step 2: Load an MR/diff and check the diff view in both themes**

Load any MR (paste its URL or `group/proj!iid` into the header input and click Load). In both themes, confirm:
- Added/deleted line backgrounds and gutters are readable (`--add`/`--addln`/`--addfg`, `--del`/`--delln`/`--delfg`).
- Syntax highlighting (`.tok-cmt`/`.tok-str`/`.tok-num`/`.tok-kw`) is readable against the code background.
- The file tree, split-mode toggle, and commit-mode toggle all still render correctly.

- [ ] **Step 3: Check highlight/discussion/chat surfaces in both themes**

Click a line to create a highlight, and open its detail panel. In both themes, confirm:
- The rail's highlight rows (`.hrow`, including `.hrow.agent` and `.hrow.resolved` if applicable) are readable.
- The chat thread (`.msg.user`/`.msg.agent`) and any chips (`comment`/`posted`/`insight`/`stale`) are readable.
- If the MR has host discussions, the purple discussion-thread indicator (`tr.thl`) and the detail panel's `.byclaude` badge (if a Claude-authored note is present) are readable.

- [ ] **Step 4: Check the diff-version banner and rebase-interdiff view, if reachable**

If the MR has multiple pushes, switch to an older diff version and confirm `.verbanner` (both the active and `.baseline` variants) reads correctly in both themes. If a rebase produced a range-diff fallback, confirm the `.rdiff`/`.rd-cmod`/`.rd-cnew`/`.rd-cdrop` commit markers read correctly too.

- [ ] **Step 5: Fix anything that reads poorly**

If a color combination is hard to read in either theme, go back to the relevant Task 1-5 step, adjust the variable's light or dark hex value in *both* the `:root` block and any place it's documented in the design spec, and re-run that task's verification step before continuing.

- [ ] **Step 6: Stop the server and do a final full-suite sanity check**

Run: `uv run pytest -q`

Expected: same pass/fail counts as `main` had before this branch (this plan touches no Python — `review_mate/web/` isn't imported by any test). If any *new* failures appear, they're unrelated to this plan's changes and should be reported, not silently ignored.

- [ ] **Step 7: Commit (only if Step 5 required fixes; otherwise skip)**

```bash
git add review_mate/web/index.html
git commit -m "fix(web): adjust theme colors found during visual regression pass"
```
