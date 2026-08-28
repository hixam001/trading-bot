# Frontend Design System — trading-bot terminal

Synthesized from `.clinerules/awesome-design-skills` — **mono** (data-dense,
high-contrast terminal), **sleek** (minimalist foundations, 8pt grid,
semantic tokens), **impeccable** (testable quality gates, explicit states) —
and held to the project's **defense-first** and **performance-discipline**
skills.

**Design intent (one sentence):** a calm, dense, dark trading terminal that
presents live money truth with zero decoration, zero ambiguity, and zero
client-side invention — every number exactly as the backend sent it.

## 1. Tokens (single source of truth: `tailwind.config.js`)

### Color — surface ladder
| token | value | use |
|---|---|---|
| `ink` | `#0a0e14` | page background |
| `panel` | `#11161f` | card surface |
| `raised` | `#161e2c` | hover / nested surface / expanded rows |
| `line` | `#1e2633` | hairline borders |
| `line-strong` | `#2c3849` | emphasized borders, table headers |

### Color — text ladder (all pass WCAG AA on `ink`/`panel`)
| token | value | use |
|---|---|---|
| `bright` | `#eef2f8` | headline numbers, emphasis |
| `body` | `#c7d0dd` | default text |
| `dim` | `#8b96a5` | labels, secondary (≥4.5:1 on panel) |
| `faint` | `#5c6773` | timestamps, chrome — never meaning |

### Color — semantics (meaning, never decoration)
| token | value | meaning |
|---|---|---|
| `pos` | `#3fb950` | profit / pass / connected / verified |
| `neg` | `#f85149` | loss / fail / offline / refused |
| `warn` | `#d29922` | degraded / unknown / flagged |
| `info` | `#58a6ff` | neutral accent, links, focus |

Rules: semantic colors are used ONLY for their meaning. Never use `neg` as a
brand accent; the LIVE indicator is `neg`-red deliberately (real money is a
warning-level fact).

### Typography
- **Data + default:** `'JetBrains Mono Variable'` — all numbers, hashes,
  tables, feed rows. Tabular figures via `font-variant-numeric: tabular-nums`
  on every numeric column (columns must not jiggle as values change).
- **UI prose:** `'Inter Variable'` — panel titles, labels, explanatory text.
- Scale: 11 / 12 / 13 / 15 / 18 / 24. Headline stats 24 semibold mono;
  table body 12; labels 11 uppercase tracking-wide.

### Space & shape
- 8pt grid: 4 / 8 / 12 / 16 / 24 / 32 (compact density mode for tables:
  row padding 6px vertical).
- Radius: 6px panels, 4px controls. No shadows — hierarchy via surface ladder
  + borders (flat terminal, not cards-floating-on-nothing).
- Focus: `outline: 2px solid info; outline-offset: 2px` on `:focus-visible`.

## 2. Component anatomy

**Panel** — `bg-panel border border-line rounded-md`. Header row: 11px
uppercase Inter label (dim) + optional right-side status. Body: 12–13px.
Panels never nest more than one level deep.

**Stat** — label (11 dim) above value (15–24 mono bright). Signed money is
always `+$x.xx` / `−$x.xx` colored pos/neg; null renders `—` (em dash),
never `$0.00`, never blank.

**Badge** — 11px, 1px border in its semantic color, transparent bg. Variants:
pos / neg / warn / info / neutral.

**Row (feed/refusals)** — full-width `<button>` (keyboard operable), hover
`bg-raised`, expanded state `bg-raised/60 border-l-2 border-info`.

**Table** — header row 11 uppercase dim over `line-strong` border; rows
separated by `line` hairlines; numeric columns right-aligned tabular-nums.

## 3. Required states (every data panel implements all five)
1. **loading** — skeleton bars (`animate-pulse bg-raised`), never a spinner
   alone, never blank.
2. **empty** — explicit sentence: what is empty and why it might be
   ("No decisions yet this cycle.").
3. **error** — what failed + that retry is automatic; `neg` border.
4. **offline** (app-level) — single global banner; panels keep last data.
5. **stale** — if a poll hasn't refreshed in >3× its interval, show age.

## 4. Accessibility acceptance criteria (testable)
- All interactive elements are `<button>`/`<a>` with visible focus rings.
- Expandable rows: `aria-expanded`, operable with Enter/Space.
- Copy-address buttons announce "copied" state (text, not color-only).
- No meaning carried by color alone — every pos/neg value also carries a
  sign (`+`/`−`) or a word (PASS/FAIL).
- Hit areas ≥ 24px tall.

## 5. Anti-patterns (prohibited)
- Raw hex values in components (must go through tokens).
- Client-side money math — render backend values verbatim.
- Decorative motion (no entrance animations; only `animate-pulse` skeletons).
- Emoji as status (use badges; the single exception is the ● live dot).
- Invented data to fill gaps — `—` or the documented empty state.
- New runtime JS dependencies without a documented reason.

## 6. QA checklist (executed in review + Playwright)
- [ ] `npm run build` clean (tsc strict + vite)
- [ ] Playwright: loads with zero console errors
- [ ] Playwright: all panels render loading→data or loading→empty, never blank
- [ ] Playwright: feed expand/collapse + copy interaction works
- [ ] Playwright: offline banner appears when API unreachable
- [ ] Grep: no `#` hex literals outside tailwind.config.js
- [ ] Tab order walks header → tabs → panels without traps
