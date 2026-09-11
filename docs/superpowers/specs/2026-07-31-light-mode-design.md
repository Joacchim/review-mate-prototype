# Light Mode — Design

**Date:** 2026-07-31
**Status:** Approved

## Goal

review-mate's web UI ([review_mate/web/index.html](../../../review_mate/web/index.html)) is dark-only. Add a light theme that follows the OS's `prefers-color-scheme` by default, with a manual override the reviewer can pin via a header button.

## Scope

- Touches exactly two files: `review_mate/web/index.html` (inline `<style>` block, header markup, one new inline `<script>`) and `review_mate/web/app.js` (theme toggle wiring).
- No new files, no build step, no external assets. The project has no CSS files, no image assets, and no color logic in JS outside the stylesheet — confirmed by reading both files in full.
- No automated frontend test harness exists (no Playwright/Puppeteer/jsdom, no `tests/` coverage of `web/`). Verification is manual: run the app (`/run` or `uv run review-mate` per README), open it in a browser, toggle the theme, and eyeball each screen. This is **not** a TDD-able change — the plan should say so rather than inventing fake tests.

## Mechanism

1. **Resolution order:** `localStorage["rm-theme"]` (`"light"` | `"dark"`, an explicit user override) → else `matchMedia("(prefers-color-scheme: light)")` → else dark (today's behavior, unqualified `:root`).
2. **No flash of wrong theme:** an inline `<script>` placed immediately after `</style>` in `<head>` runs synchronously before the body paints. It reads localStorage/matchMedia and sets `document.documentElement.dataset.theme = "light"` (or leaves it unset for dark, since dark is the bare `:root` default).
3. **Toggle button:** `#t-theme`, styled and placed exactly like the existing `#t-left`/`#t-split`/`#t-right`/`#t-commits` buttons in `<header>`. Click handler flips `document.documentElement.dataset.theme` between `"light"`/`"dark"` and writes the explicit choice to `localStorage["rm-theme"]` — mirrors the existing `splitMode`/`rm-split` pattern in app.js exactly (read on load, `classList.toggle("on", ...)` for pressed state, write on click).
4. **Live system tracking:** app.js registers `matchMedia("(prefers-color-scheme: light)").addEventListener("change", ...)`. The listener re-applies the system theme **only when `localStorage["rm-theme"]` is unset** — once the reviewer clicks the toggle, their choice sticks and system changes are ignored. (No "reset to auto" UI — YAGNI; clearing localStorage manually is the escape hatch, and it wasn't requested.)
5. **CSS switch:** `:root[data-theme="light"] { --bg: #fff; ... }` overrides every variable. Dark stays the bare `:root` block, unchanged — a reviewer who never touches the toggle and whose OS reports dark (or no preference) sees pixel-identical output to today.

## File changes

### `review_mate/web/index.html`

**A. New inline script**, right after the closing `</style>` tag (currently line 235), before `</head>`:

```html
<script>
  (function () {
    var stored = localStorage.getItem("rm-theme");
    var light = stored ? stored === "light" : matchMedia("(prefers-color-scheme: light)").matches;
    if (light) document.documentElement.dataset.theme = "light";
  })();
</script>
```

**B. New header button**, in the toggle-button group (currently lines 241–244), added after `#t-commits` and before the `<span class="mr" ...>`:

```html
<button class="btn" id="t-theme" title="toggle light / dark theme">☾</button>
```

(app.js sets the glyph to ☀ when light is active — see below — so the button always shows the theme you'd switch *to*, matching how `⇄ split` reads as an action label rather than a state label.)

**C. New CSS variables and a light override block.** The existing `:root` block (lines 8–12) keeps its current 14 variables unchanged, but ~20 more variables must be introduced for colors that are currently hardcoded literals scattered through the stylesheet — those literals are pale/light values tuned for a dark background and would be unreadable if the background alone flipped to white. Full table (dark = existing value, unchanged; light = new):

| Variable | Dark (existing) | Light (new) | Used by (selector) |
|---|---|---|---|
| `--bg` | `#0f1117` | `#ffffff` | `body` |
| `--panel` | `#171a23` | `#f6f8fa` | `header`, `.files`, `.rail`, `td.ln`, `.hrow.mr`, `.qitem`, `.sitem`, `.detail` |
| `--panel2` | `#1c2030` | `#eaeef2` | `.node:hover`, `.node.file.on`, `tr.hh td`, hover/active surfaces |
| `--line` | `#272b3a` | `#d0d7de` | borders throughout |
| `--line2` | `#33384a` | `#afb8c1` | input borders, stronger dividers |
| `--fg` | `#e6e8ee` | `#1b1f24` | primary text |
| `--fg2` | `#9aa3b2` | `#59636e` | muted text |
| `--accent` | `#6ea8fe` | `#0969da` | links, active state, focus |
| `--add` | `#10301f` | `#dafbe1` | added-line background |
| `--addln` | `#1f5135` | `#aceebb` | added-line gutter |
| `--addfg` | `#9be8bf` | `#116329` | added-line text |
| `--del` | `#34141a` | `#ffebe9` | deleted-line background |
| `--delln` | `#5a2230` | `#ffc1ba` | deleted-line gutter |
| `--delfg` | `#f3aab4` | `#82071e` | deleted-line text |
| `--ok` *(new)* | `#3ddc84` | `#1a7f37` | `.agent.watching .dot` |
| `--warn` *(new)* | `#d0a24a` | `#9a6700` | `.agent.working .dot`, `.rd-cmod` border |
| `--warnfg` *(new)* | `#f0c88a` | `#7d4e00` | `.agent.working .alab`, `.sincenote`, `.rd-cmod` text |
| `--warnbg` *(new)* | `rgba(208,162,74,.1)` | `rgba(154,103,0,.12)` | `.sincenote` background |
| `--warnline` *(new)* | `#7a5a2a` | `#d4a72c` | `.sincenote` border, `.chip.stale` border |
| `--stalebg` *(new)* | `#5a3a1a` | `#fff8c5` | `.chip.stale` background |
| `--stalefg` *(new)* | `#f0c890` | `#7d4e00` | `.chip.stale` text |
| `--purple` *(new)* | `#c9a2ef` | `#8250df` | `tr.thl` box-shadow, `.tok-kw`, `.sitem.st-merged .ststate` text |
| `--purpleln` *(new)* | `#a56ef0` | `#6639ba` | `.sitem.st-merged` left border, `.ststate` border |
| `--purplebg` *(new)* | `rgba(201,162,239,.28)` | `rgba(130,80,223,.16)` | `tr.flash` background |
| `--accentline` *(new)* | `#3a4a6b` | `#c3d2e8` | `.hrow.agent`, `.chip.comment`/`.chip.insight`, `.byclaude` borders |
| `--accentbg` *(new)* | `rgba(110,168,254,.13)` | `rgba(9,105,218,.08)` | `.verbanner` background |
| `--accentborder` *(new)* | `rgba(110,168,254,.4)` | `rgba(9,105,218,.35)` | `.verbanner` border |
| `--accentglow` *(new)* | `rgba(110,168,254,.22)` | `rgba(9,105,218,.18)` | `.vblabel::before` box-shadow |
| `--fg2bg` *(new)* | `rgba(154,163,178,.08)` | `rgba(89,99,110,.08)` | `.verbanner.baseline` background |
| `--postline` *(new)* | `#2e6b45` | `#1a7f37` | `.chip.posted`, `button.ok` border |
| `--delline` *(new)* | `#6b2e38` | `#cf222e` | `button.no` border |
| `--usermsgbg` *(new)* | `#1d2a44` | `#ddf4ff` | `.msg.user` background |
| `--tokcmt` *(new)* | `#7f8aa3` | `#6e7781` | `.tok-cmt` |
| `--tokstr` *(new)* | `#8fdcb0` | `#116329` | `.tok-str` |
| `--toknum` *(new)* | `#e0b892` | `#953800` | `.tok-num` |
| `--shadow` *(new)* | `rgba(0,0,0,.55)` | `rgba(0,0,0,.16)` | `.detail` box-shadow |

Two additional simplifications ride along with this migration (same-meaning colors, deduped rather than given their own variable):
- `.searcherr` (currently hardcoded `background:#5a1f1f;border:1px solid #7a3030;color:#f0c0c0`) switches to `background:var(--del);border:1px solid var(--delln);color:var(--delfg)` — it's the same "error red" semantics as the diff deletion color.
- `button.no` keeps its own border color (`--delline`, table above) since it sits on the neutral panel background, not inside a red-tinted box, so it needs a border saturated enough to read as "red" on its own.

**D. Add the light override block**, immediately after the existing `:root{...}` block (after line 12):

```css
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

And the dark defaults for the *new* variables go into the existing `:root{...}` block (lines 8–12), alongside the current 14:

```css
--ok:#3ddc84; --warn:#d0a24a; --warnfg:#f0c88a; --warnbg:rgba(208,162,74,.1); --warnline:#7a5a2a;
--stalebg:#5a3a1a; --stalefg:#f0c890;
--purple:#c9a2ef; --purpleln:#a56ef0; --purplebg:rgba(201,162,239,.28);
--accentline:#3a4a6b; --accentbg:rgba(110,168,254,.13); --accentborder:rgba(110,168,254,.4);
--accentglow:rgba(110,168,254,.22); --fg2bg:rgba(154,163,178,.08);
--postline:#2e6b45; --delline:#6b2e38; --usermsgbg:#1d2a44;
--tokcmt:#7f8aa3; --tokstr:#8fdcb0; --toknum:#e0b892; --shadow:rgba(0,0,0,.55);
```

**E. Rewrite every consuming selector** to reference the new variables instead of its hardcoded literal. Exact selectors (line numbers as read in the current file):

- L23 `header .agent.watching .dot{background:#3ddc84}` → `background:var(--ok)`
- L24 `header .agent.working .dot{background:#d0a24a;...}` → `background:var(--warn);...`
- L25 `header .agent.working .alab{color:#f0c88a}` → `color:var(--warnfg)`
- L67 `.sincenote{...color:#f0c88a;background:rgba(208,162,74,.1);border:1px solid #7a5a2a;...}` → `color:var(--warnfg);background:var(--warnbg);border:1px solid var(--warnline);...`
- L84 `tr.thl td.code{box-shadow:inset -3px 0 0 #c9a2ef}` → `box-shadow:inset -3px 0 0 var(--purple)`
- L85 `tr.hl.thl td.code{box-shadow:inset 3px 0 0 var(--accent), inset -3px 0 0 #c9a2ef}` → `..., inset -3px 0 0 var(--purple)`
- L86 `tr.flash td.code{background:rgba(201,162,239,.28)}` → `background:var(--purplebg)`
- L87 `.tok-cmt{color:#7f8aa3;...} .tok-str{color:#8fdcb0;...} .tok-num{color:#e0b892;...} .tok-kw{color:#c9a2ef;...}` → `var(--tokcmt)`, `var(--tokstr)`, `var(--toknum)`, `var(--purple)` respectively
- L103 `.hrow.agent{border-color:#3a4a6b}` → `border-color:var(--accentline)`
- L116 `.chip.comment{color:var(--accent);border-color:#3a4a6b} .chip.posted{color:var(--addfg);border-color:#2e6b45}` → `border-color:var(--accentline)` / `border-color:var(--postline)`
- L117 `.chip.insight{color:var(--accent);border-color:#3a4a6b}` → `border-color:var(--accentline)`
- L133 `.detail .byclaude{...border:1px solid #3a4a6b}` → `border:1px solid var(--accentline)`
- L148 `.verbanner{...background:rgba(110,168,254,.13);border:1px solid rgba(110,168,254,.4);...}` → `background:var(--accentbg);border:1px solid var(--accentborder);...`
- L150 `.vblabel::before{...box-shadow:0 0 0 3px rgba(110,168,254,.22);...}` → `box-shadow:0 0 0 3px var(--accentglow);...`
- L151 `.verbanner.baseline{background:rgba(154,163,178,.08);border-color:var(--line2);...}` → `background:var(--fg2bg);...`
- L153 `.chip.stale{background:#5a3a1a;color:#f0c890;border-color:#7a5a2a}` → `background:var(--stalebg);color:var(--stalefg);border-color:var(--warnline)`
- L161 `.rd-cmod{border-left-color:#d0a24a;color:#f0c88a}` → `border-left-color:var(--warn);color:var(--warnfg)`
- L171 `.searcherr{background:#5a1f1f;border:1px solid #7a3030;color:#f0c0c0;...}` → `background:var(--del);border:1px solid var(--delln);color:var(--delfg);...`
- L180 `button.ok{border-color:#2e6b45;color:var(--addfg)} button.no{border-color:#6b2e38;color:var(--delfg)}` → `border-color:var(--postline)` / `border-color:var(--delline)`
- L190 `.msg.user{background:#1d2a44;margin-left:22px}` → `background:var(--usermsgbg);...`
- L207-208 `.claudeans{...border:1px solid #3a4a6b} .claudeans .byclaude{...border:1px solid #3a4a6b;...}` → `border:1px solid var(--accentline)` (both)
- L227-233 `.sitem.st-merged{border-left:3px solid #a56ef0} .sitem.st-merged .ststate{color:#c9a2ef;border-color:#5a3a8a}` → `border-left:3px solid var(--purpleln)` / `color:var(--purple);border-color:var(--purpleln)`
- L128 `.detail{...box-shadow:0 16px 48px rgba(0,0,0,.55);...}` → `box-shadow:0 16px 48px var(--shadow);...`

### `review_mate/web/app.js`

Add near the top, alongside the existing `splitMode`/`showAll` module-level state (currently lines 9–20):

```js
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

And in the wiring block alongside the existing `t-left`/`t-split`/`t-commits` handlers (currently lines 588–597):

```js
applyTheme();
$("t-theme").onclick = () => {
  const light = document.documentElement.dataset.theme !== "light";
  localStorage.setItem("rm-theme", light ? "light" : "dark");
  applyTheme();
};
```

(`applyTheme()` re-reads `document.documentElement.dataset.theme` fresh each call, so the inline `<script>` in `<head>` and this wiring never disagree — the head script's job is purely to avoid a flash before app.js loads; app.js's `applyTheme()` is the source of truth once it runs, including syncing the button glyph, which the head script can't set because the button doesn't exist yet at that point in parsing.)

## Non-goals

- No "auto" tri-state UI, no reset-to-system control beyond clearing localStorage by hand.
- No changes to `review_mate/web/app.js` logic beyond theme wiring — no refactor of the existing toggle pattern.
- No new automated tests — there's no frontend test harness in this repo to extend, and this is a pure visual change with no branchable logic worth unit-testing beyond "does the right variable win," which is what manual verification checks directly.
