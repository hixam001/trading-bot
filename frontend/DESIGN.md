# Frontend Design System — trading-bot terminal

The **SIGNAL world** (§59, shipped 2026-09-10): the operator-supplied
`demo_2_signal.html` evolved and ported into the production SPA. Ultra-black
blue-tinted console, cyan signal accent, bracket verdicts, shell-prompt
titles — quiet chrome, loud data. The demo files themselves were deleted
after the port, per operator directive; this document is now the single
source of truth.

**Design intent (one sentence):** a calm, dense, dark trading terminal that
presents live money truth with zero decoration, zero ambiguity, and zero
client-side invention — every number exactly as the backend sent it.

## 1. Tokens (single source of truth: `tailwind.config.js`)

### Color — surface ladder (stepped blue-black)
| token | value | use |
|---|---|---|
| `base` | `#080b0e` | page background, column ground |
| `surface` | `#0f1419` | panels, hero band, statusline |
| `raised` | `#141b22` | hover / stat cards / expanded rows |
| `surface-3` | `#182129` | skeleton sweep block |
| `line` | `#223040` | hairline borders |
| `line-soft` | `#182029` | row separators, nested borders |
| `line-strong` | `#2d3f52` | emphasized borders, tx-link underline |

### Color — text ladder (all pass WCAG AA on `base`/`surface`/`raised`)
| token | value | use |
|---|---|---|
| `bright` | `#e4ecf2` | headline numbers, emphasis |
| `body` | `#aebbc7` | default text |
| `dim` | `#8595a8` | labels, secondary |
| `faint` | `#73869c` | timestamps, chrome — never meaning |

### Color — signal accent + semantics (meaning, never decoration)
| token | value | meaning |
|---|---|---|
| `live` | `#4ab8ff` | STRUCTURE ONLY: brand, focus, tabs, `> ` prompts, WS chrome, fresh-arrival flash |
| `reject` | `#b8c4d1` | the `[SKIP]` decision chip (a decision, not a failure) |
| `pass` | `#3ee089` | profit / pass / connected / filled |
| `fail` | `#f2695c` | loss / fail / offline / refused |
| `warn` | `#d8a03a` | degraded / memo-only / flagged |

Rules: `live` is never used for meaning; pass/fail/warn are never used as
brand. The LIVE indicator is bracketed signal chrome but the real-money
warning it carries is deliberate.

### Typography
- **Data + default:** `'JetBrains Mono Variable'` (self-hosted; kept over the
  demo's IBM Plex Mono to avoid a new font dependency — visually equivalent
  console voice). Every number/tab/receipt line, `tnum` everywhere.
- **UI prose:** `'Inter Variable'` — body copy, thesis lines.
- Scale: 9.5 / 10 / 10.5 / 11 / 11.5 / 12 / 13 / 15 / 16 / 30. Hero equity 30
  semibold mono; stat cards 16; labels 10 uppercase tracking.

### Space & shape
- Radius 4px everywhere; no shadows — hierarchy via the surface ladder +
  hairlines. Hero band carries the bracket-corner marks (top-left/top-right).
- Focus: `outline: 2px solid live; offset 2px`. Selection/caret/scrollbars
  ship the signal tint.

## 2. Component anatomy

**Shell** — full-height app grid: hero band → tab bar → main → statusline.
Five views: live (3-column mission control: decisions tape / positions /
book-health stack — each column independently scrolled on xl), holdings,
journal, market, system.

**Hero band** — `trading-bot` wordmark + sub, `EQUITY` headline (30px mono,
verbatim from `/api/live/portfolio`), SVG equity-curve sparkline (the new
`Spark` primitive in `ui.tsx`, fed by `stats.equity_curve`, no chart
library), WIN RATE / PROFIT FACTOR / DRAWDOWN stats, WS badge, UTC clock
with cursor, `[● LIVE · real money]` tag.

**Titles** speak with the shell prompt: `.panel-title` renders `> ` in cyan.

**Verdicts** are bracketed mono chips: `[ENTER]` in `pass`, `[SKIP]` in
`reject`; per-rule dots (`●`) in pass/fail inside the expanded breakdown.

**Stat card** — `bg-raised` nested surface, 10px uppercase mono label over a
16px mono value.

**Tables** — 10px uppercase mono headers over `line`; rows separated by
`line-soft`; numeric columns right-aligned `tnum`. Scroll-bounded tables
(journal decisions, money ledger) cap at `max-h-[60vh]` with sticky headers.

**Scroll-bounded lists (§59 operator directives):** the decisions tape caps
at `65vh` below xl / fills its flex column on xl; the journal's order
decisions and money ledger cap at `60vh`. Nothing grows the page forever.

**Contract addresses (§59 operator directive):** the COMPLETE mint address
is always shown — never truncated — via the `CopyText` primitive
(click-to-copy, announces "copied" by text): holdings Contract column,
journal ledger + proof rows, feed expanded rows, dashboard position rows.
Signatures/commit hashes stay short-linked to Solscan.

**Money ledger** shows CLOSED TRADES ONLY (`kind === 'close'`): proceeds +
realized P&L rows. Buys live in the order-decisions lifecycle above.

## 3. Required states (every data panel implements all five)
1. **loading** — skeleton bars (sweeping block), never a spinner, never blank.
2. **empty** — explicit `> `-prompted sentence: what is empty and why.
3. **error** — what failed + that retry is automatic; `fail` border.
4. **offline** (app-level) — single global banner when BOTH primary feeds
   fail; panels keep last data; the WebSocket reconnects with exponential
   backoff (1s–15s).
5. **stale** — deferred by operator decision (2026-09-06): every panel's
   REST poll self-heals on its next interval (see prior revisions).

## 4. Accessibility acceptance criteria (testable)
- All interactive elements are `<button>`/`<a>` with visible focus rings.
- Expandable rows: `aria-expanded`, operable with Enter/Space.
- Copy-address buttons announce "copied" state (text, not color-only).
- No meaning carried by color alone — every pass/fail value also carries a
  sign (`+`/`−`), a word (ENTER/SKIP/PASS/FAIL), or an OK/BAD label.
- Hit areas ≥ 24px tall.

## 5. Anti-patterns (prohibited)
- Raw hex values in components (must go through tokens).
- Client-side money math — render backend values verbatim. (The `Spark`
  polyline visualizes verbatim curve points; it computes no displayed money
  figure. The hero shows no "daily delta": the backend does not publish one.)
- Decorative motion — the only loops are the clock cursor and the skeleton
  sweep; the one authored moment is the fresh-decision `row-flash` tint.
- Emoji as status (the single exceptions are the `●` dot and the clock `_`).
- Invented data to fill gaps — `—` or the documented empty state.
- New runtime JS dependencies without a documented reason.
- Truncating contract addresses (full mint or nothing; shortAddr is only for
  signatures/hashes and the wallet line).

### 5.1 Slop guardrails (awesome-design-skills registry)
Banned outright — none of these appear, and none may be introduced: any
gradient, gradient hero text, emoji in headings, Inter-everywhere typography
(mono is the data voice), colored left-border cards, glassmorphism, icon
boxes in rows of three, a badge above a headline, Lucide or any icon set,
untouched shadcn components, scroll fade-ins, cursor-following beams, hover
fades (state changes swap surface tokens instead), off-scale spacing, em
dashes in rendered copy (`·` separates; `—` is reserved for the null-value
placeholder in data grids), buzzword copy, serif italics, Space Grotesk,
Instrument Serif, grain textures.

## 6. QA checklist (executed in review + Playwright)
- [ ] `npm run build` clean (tsc strict + vite)
- [ ] Playwright: loads with zero console errors
- [ ] Playwright: all panels render loading→data or loading→empty, never blank
- [ ] Playwright: feed expand/collapse + copy interaction works
- [ ] Playwright: offline banner appears when API unreachable
- [ ] Playwright: five tabs navigate (live/holdings/journal/market/system)
- [ ] Playwright: hero shows EQUITY; live book shows Open value
- [ ] Grep: no `#` hex literals outside tailwind.config.js
- [ ] Grep: no `border-l-2` colored accents (fresh rows flash via background
      tint only)
- [ ] Grep: rendered copy has no em dashes (the `—` null placeholder in
      `lib/format.ts` is data, not copy)
- [ ] Grep: no glyph beyond `●` and the clock `_` in statuses
- [ ] Grep: no recharts/lucide/shadcn imports (dead or decorative deps)
- [ ] Tab order walks hero → tabs → panels without traps

## 7. Revision history (condensed)
- **§65 (2026-09-21)** — full keyboard vocabulary + mint-history drill-down.
  (1) The §63 decisions-tape cursor is promoted to a GLOBAL vocabulary:
  j/k move, Enter expands, g/G jump to top/bottom (single `g`, deliberate —
  it is the only `g`-verb, so vim's double-tap chord would buy timeout state
  for nothing), operated by one document-level dispatcher in `App.tsx`. It
  guards the classic failure modes explicitly: nothing fires while the
  palette is open (its own keys still win, Escape included), while the `?`
  overlay is up, while focus is in any text field (input/textarea/select/
  contentEditable), or for any meta/ctrl/alt chord. Lists register a
  controller (`lib/shortcuts.ts`); a list that owns its own focused key
  handling (the tape listbox) exposes `owns()` so keys never double-step.
  The journal's order-decisions table gets the same cursor (j/k/g/G/Enter on
  proof rows); the money ledger is deliberately not registered — its rows
  cannot expand, so Enter would be a lie. (2) `?` opens a help overlay
  listing EVERY real shortcut, rendered verbatim from the same SHORTCUTS
  table the dispatcher implements — the overlay is structurally incapable of
  drifting from actual bindings. Esc/click-outside dismiss. (3) Mint-history
  drill-down: `GET /api/mint/{mint}/history` (backend: db twins
  `get_feed_events_for_mint`/`count_feed_events_for_mint` in BOTH dialects,
  base58-validated via the SEC-07 validator, empty result for unknown mints,
  422 for malformed ones) returns every feed event ever recorded for one
  mint in the SAME row shape as `/api/feed`. The frontend opens it from a
  dedicated `history` affordance BESIDE each CopyText address (expanded feed
  rows, journal proof + ledger rows, holdings/dashboard position rows) —
  copy stays copy; one click target never carries both actions. The
  drill-down is a LEVEL above the tab views (navigation stack, not a
  modal): Esc collapses its expanded row first, then backs out to the
  underlying view; `← back` does the same by click. All five §3 states
  implemented (skeleton / explicit empty / error + scheduled 5s retry /
  app-level offline / refresh-by-reopen instead of hidden polling). No new
  runtime dependencies (§5 holds).

- **§64 (2026-09-17)** — read-only safety state and persistent symptom alerts
  across all views. Empty means quiet; poll failure means unknown/stale, never
  all-clear. No dismiss/toggle control. Persisted refusal-rate Spark requires
  two real non-null snapshots; no synthetic history. Stage buttons investigate
  classified tape rows or bound journal commits; session-only seen marker.
  Hero/clock/shared-panel extraction keeps shell lean; no new dependencies.
  **Responsive remains partial:** table scroll guards are delivered, full mobile
  panel redesign is deferred pending usage, not marked complete.

- **§63 (2026-09-16)** — operator upgrades. (1) Responsive is now a
  *commitment*, not an accident: every dense table carries a min-width and
  scrolls INSIDE its container below natural width (Holdings 720px, journal
  tables 640/720px); stat grids already collapse 2-up; full mobile panel
  redesigns are out of scope until real usage asks. (2) Command palette
  (⌘K / Ctrl-K), hand-rolled ~150 lines — no new runtime dependency (§5
  holds; cmdk needed no exception). READ-ONLY by construction: view
  navigation, journal status/text filtering, jump-to-position. The
  break-state toggle is deliberately absent: a fuzzy-matched keystroke must
  never sit next to live system state; state writes keep their explicit
  gated paths. Dialog+combobox semantics, focus stays in the single input
  (Tab inert), arrows/enter/esc. (3) Refusal funnel on the system view:
  candidates seen → gate passed → model approved → filled, plus the
  §57 model-refusal-rate-of-gate-passers — server-computed by the new
  `/api/funnel` (classification identical to `scripts/perf_report.py`),
  bespoke divs, no chart library (§5), unknowns as `—` (§2). (4) Decisions
  tape keyboard navigation: j/k move a cursor, enter expands, esc collapses;
  scoped to the focused tape (tabIndex=0 listbox) so keys never leak into
  typing contexts; existing focus rings and `aria-expanded` untouched.
  `perf_report.py --since YYYY-MM-DD` added for a post-§57 baseline sample.
- **§59 (2026-09-10)** — SIGNAL world ported from the operator's
  `demo_2_signal.html` (which superseded the §58 PHOSPHOR AMBER gold world):
  full-height shell + hero band + five views + statusline; decisions tape,
  journal decisions and money ledger scroll-bounded; money ledger = closed
  trades only; complete contract addresses everywhere (CopyText); Spark
  equity curve; E2E pins updated in-commit.
- **§58 (2026-09-09)** — terminal redesign, tabbed shell, Performance panel
  with the anchored-book "track equity" contract (the two-books distinction:
  wallet equity vs anchored track equity — still enforced, hero vs Performance).