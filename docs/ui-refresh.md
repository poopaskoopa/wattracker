# UI refresh — work plan and division of labour

Status: **spec agreed, WP-0 lands first.** Read `AGENTS.md` first — the worktree,
commit-identity and integration rules there apply unchanged.

## Goal

The information architecture is fine. The *rendering* is dated: Chart.js v4 stock
chrome on a dark surface, ad-hoc per-chart styling copied between four files, two
dual-axis charts, radar charts used for ordered data, and a flat CSS surface with
no elevation, spacing scale or type scale.

**Constraint from the owner: same information, roughly the same screen footprint.**
This is a re-skin plus a small number of form corrections — not a redesign, not a
re-layout, not a feature change. If a change makes a section materially taller or
shorter, say so in the handoff rather than shipping it silently.

Non-goals: no new charting library (Chart.js 4.5.1 stays, vendored), no CDN, no
build step, no framework. Everything stays hand-written CSS + vanilla JS.

---

## Part 1 — What is actually wrong

Concrete, per-symptom. Each maps to a work package in Part 3.

### 1.1 Chart chrome is Chart.js default (the main "dated" signal)

`Chart.defaults` is never touched. Every chart re-specifies axis styling by hand
and inconsistently:

- `app.js:203-224` (dashboard) styles the **x** axis ticks/grid/border and leaves
  **both y axes** at stock — Chart.js default tick grey `#666` and grid
  `rgba(0,0,0,0.1)`, both designed for a *white* page. On `#1a2028` the y grid is
  effectively invisible and the y labels are muddy.
- `app.js:319-348` (power–duration curve) and `app.js:525-541` (activity detail)
  style **nothing** — fully stock axes, stock legend boxes, stock tooltip
  (white box, black text, rounded 6, `boxWidth: 40` colour swatches).
- `volume.js:98-115` styles both axes, with a *third* set of literal colours.
- Default `borderWidth` is 3 for lines — heavy and blobby at these chart heights.
- Default legend renders 40px colour rectangles with a strikethrough on toggle;
  the dashboard already replaced it with a nice HTML legend (`.chart-legend`),
  but activity detail still uses the stock one (`app.js:564`).

**Fix:** one `static/chart-theme.js`, loaded after Chart.js in `base.html`, that
sets `Chart.defaults` for font, colour, grid, border, tooltip, point and line
geometry from the CSS custom properties. Every chart then deletes its local
axis-cosmetics block and keeps only what is genuinely per-chart (titles, ranges,
callbacks). This single change does most of the visible work.

### 1.2 Dual-axis charts (two y-scales on one plot)

Two places, and it is the single most misleading chart pattern there is — the
crossing point of two series is an artifact of the two scales, not a fact:

- **Dashboard** `app.js:219-223`: `y` = CTL/ATL/TSB (load, ~0–100) on the left,
  `yFtp` = Training FTP watts (~200–350) on the right, four series overplotted.
- **Activity detail** `app.js:530-540`: `yPower` left, `yBio` (HR *and* cadence,
  two different units sharing one axis) right, plus a hidden third `yAlt`.

**Fix — dashboard:** split into two stacked panels sharing one x axis and one
zoom/range control: load (CTL/ATL/TSB) on top, Training FTP below at ~⅓ height.
Total height stays within the current `height="130"` canvas + legend + hint
footprint if the FTP panel takes the space the hint line already reserves.
**Fix — activity detail:** same treatment, power on top, HR/cadence below.
Elevation stays a background band on whichever panel it reads best in (power).

Keep the shared crosshair across both panels so a hover still reads all series
at one date — that is what the dual axis was buying, and a linked crosshair buys
it without the lie.

### 1.3 Radar chart for ordered continuous data

`profile.html:60-72` (chart built at `profile.html:189`) renders the power profile
as a **radar** — the only radar in the app. Duration is an ordered continuous
variable; a radar puts it on a
circle, so the enclosed-area shape (the thing the eye reads first) is an artifact
of the arbitrary starting angle and axis order. The values are also normalised to
100% per duration, which a radar makes hard to compare across two series.

**Fix:** a horizontal grouped bar / dumbbell chart — one row per duration, two
marks (all-time vs last 60 d), sorted by duration. Same information, same
approximate footprint, and the "where am I weak" question becomes a one-glance
read of bar length. Direct-label the % on each row.

### 1.4 The categorical palette fails on the dark surface

Validated with the dataviz validator against surface `#1a2028`:

```
current: #4caf7d,#f2a900,#5a9bd4,#e05252
  [FAIL] Lightness band   #4caf7d L=0.681, #f2a900 L=0.785  (band is 0.48–0.67)
  [PASS] CVD separation   worst adjacent ΔE 10.1 (protan)
  [PASS] Contrast vs surface
```

Two series colours are too light for the dark panel — they glare and flatten
against each other at the top of the plot. CVD safety is already fine, so this
is a small re-step, not a repaint.

**Replacement, validated (all five checks PASS, worst adjacent CVD ΔE 10.6):**

```
#1baf7a,#c98500,#3987e5,#e05a5a
```

Re-run before changing any of these:

```sh
node <dataviz-skill>/scripts/validate_palette.js "#1baf7a,#c98500,#3987e5,#e05a5a" \
  --mode dark --surface "#1a2028"
```

**Important scoping rule:** this is the **series** palette only. The brand accent
`--accent: #f2a900` stays exactly as it is for UI chrome — buttons, links, active
states, the header rule. Series gold darkens to `#c98500`; chrome gold does not.
Never use `--accent` as a series colour and never use a series colour on a button.

Ride page (`ride.html`) validated separately against its `#10161d` surface:
`#c98500` power, `#3987e5` cadence, `#d55181` HR — PASS all-pairs. **Target power
must not become a fourth hue** (it fails CVD against HR); it is the *same measure*
as actual power, so it stays the power hue at `borderDash: [6,4]` and 60% alpha.
Elevation stays a neutral gray band — it is a context surface, not a series, so
it is exempt from the chroma floor.

### 1.5 No design tokens beyond colour

`style.css` has 9 custom properties, all colour. Everything else is a literal:
`#2a333d` appears 30+ times as a border, `#10161d` 12+ times as an inset surface,
radii are 3/4/6/8/10/999px picked ad hoc, spacing is a freehand mix of `0.3` /
`0.35` / `0.4` / `0.45` / `0.5` / `0.6` / `0.65` / `0.75` / `0.8` / `0.9` rem.
That inconsistency is a large part of why it reads as unpolished even where
nothing is *wrong*.

**Fix:** extend `:root` with the token set in Part 2 and replace the literals.
Mechanical, high-value, zero behaviour change.

### 1.6 Flat, borderless surfaces

Every panel is `background: var(--panel); border-radius: 8px` with no border and
no shadow, sitting on a `--bg` only 7% darker. Panels therefore have no edge —
the page reads as one undifferentiated dark field. `.ooto-panel` and `.modal-box`
already add `1px solid #2a333d` and look better for it.

**Fix:** one `--surface-border` + one `--shadow-1` applied to `.card`,
`.chart-block`, `.profile-section`, `.zone-summary`, `.scan-panel`,
`.ride-preview`. Costs 0px of layout.

### 1.7 Typography is undifferentiated

One system font stack, sizes from 0.6rem to 1.4rem chosen per-component, no
`line-height` set anywhere, and numeric readouts (`.card .value`, table power
columns, `.legend-value`) are proportional-figure — so digits jitter as values
update on the live ride page and columns fail to align in tables.
`font-variant-numeric: tabular-nums` is set in only 3 of ~15 numeric contexts.

**Fix:** a 7-step type scale as tokens, and `tabular-nums` on every numeric
readout and every numeric table cell.

### 1.8 Tables are unstyled beyond a bottom border

`th, td` get `border-bottom: 1px solid #2a333d` and nothing else — no zebra, no
row hover, no sticky header on the long ones (`activities.html`, `races.html`,
`plan.html`), and numeric columns are left-aligned in most tables
(`.power-table` is the exception).

**Fix:** shared `.data-table` treatment: right-align + tabular-nums on numeric
cells, `:hover` row tint, sticky `thead` inside `.table-scroll`, and a subtle
`--surface-2` zebra. Same row height.

### 1.9 The inline workout SVG is fixed-scale

`workout_graph.js` emits a `560x200` viewBox with hard-coded `10px` label text.
`style.css:419-420` then caps `.profile-wrap` at 760px specifically so the labels
don't blur when scaled up — i.e. the layout is working around the graph rather
than the graph adapting. It also uses a single accent fill with no zone colouring,
so an interval's *intensity* is invisible.

**Fix:** `vector-effect: non-scaling-stroke` on the strokes, label sizing via CSS
(`font-size: var(--fs-xs)` with `dominant-baseline`), and fill each segment by its
power zone using a sequential ramp of the accent hue. Then lift the 760px cap.

### 1.10 Misc

- No focus-visible styling anywhere; keyboard focus is the browser default ring
  on a dark background.
- `.zone-bar` is capped at `max-width: 70px`, so the bars are not proportional to
  each other beyond the cap — the encoding breaks exactly where it matters most.
  Make it a full-width track with a filled portion.
- `.badge` is `color: #fff` on `--accent` gold — ~1.9:1, unreadable. Should be
  `#1a1a1a` like every other accent-background element in the file.
- Nav has no active-page indicator.
- `.status-banner` hard-codes three text colours (`#bfe8d2`, `#f3ddae`,
  `#f0c4c4`) outside the token set.

---

## Part 2 — Design tokens (WP-0 output — the shared contract)

Both agents code against these names. **Do not add tokens outside WP-0**; if you
need one, ask the integrator to add it to WP-0 and rebase.

```css
:root {
    /* ---- surfaces (existing --bg/--panel keep their values) ---- */
    --bg:              #0f1419;
    --panel:           #1a2028;
    --surface-2:       #212934;  /* raised: table zebra, hover rows, chips  */
    --surface-inset:   #10161d;  /* recessed: inputs, code, progress track  */
    --surface-border:  #2a333d;  /* the literal used 30+ times today        */
    --shadow-1:        0 1px 2px rgba(0,0,0,.35), 0 2px 8px rgba(0,0,0,.22);

    /* ---- ink ---- */
    --text:            #e6e6e6;
    --text-bright:     #f5f7fa;
    --muted:           #8a94a0;

    /* ---- brand / status (UI chrome only — never a series colour) ---- */
    --accent:          #f2a900;
    --on-accent:       #1a1a1a;
    --ok:              #4caf7d;
    --alert:           #e05252;

    /* ---- series palette (charts only — validated, see 1.4) ---- */
    --s-1:             #1baf7a;  /* CTL / fitness / hours   */
    --s-2:             #c98500;  /* ATL / fatigue / TSS / power */
    --s-3:             #3987e5;  /* TSB / form / distance / cadence */
    --s-4:             #e05a5a;  /* FTP / calories          */
    --s-hr:            #d55181;  /* heart rate              */
    --s-context:       #8a94a0;  /* elevation band, non-series context */
    --grid:            rgba(255,255,255,.07);
    --axis:            rgba(255,255,255,.20);

    /* ---- spacing (4px base) ---- */
    --sp-1: .25rem; --sp-2: .5rem;  --sp-3: .75rem;
    --sp-4: 1rem;   --sp-5: 1.5rem; --sp-6: 2rem;

    /* ---- radius ---- */
    --r-sm: 4px; --r-md: 8px; --r-lg: 12px; --r-pill: 999px;

    /* ---- type scale ---- */
    --fs-xs: .72rem; --fs-sm: .8rem;  --fs-base: .9375rem;
    --fs-md: 1.05rem; --fs-lg: 1.3rem; --fs-xl: 1.6rem; --fs-2xl: 2rem;
    --lh-tight: 1.2; --lh-base: 1.5;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;

    /* ---- motion ---- */
    --ease: cubic-bezier(.2,.6,.3,1);
    --dur:  140ms;
}
@media (prefers-reduced-motion: reduce) {
    * { animation-duration: .01ms !important; transition-duration: .01ms !important; }
}
```

Plus a `.num` utility (`font-variant-numeric: tabular-nums; font-feature-settings: "tnum"`)
and a global `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }`.

### Chart rules both agents follow

1. Never a second y-axis. Two units → two stacked panels sharing an x axis.
2. Series colours come from `--s-*` only, read via
   `getComputedStyle(document.documentElement).getPropertyValue('--s-1')` (helper
   provided by WP-0 as `cssVar(name)`), never as literal hex in JS.
3. `borderWidth: 2`, `pointRadius: 0`, `pointHoverRadius: 4`, `tension: 0`
   (the 0.2/0.3 tensions currently invent data between samples).
4. Grid `--grid`, axis border `--axis`, ticks `--muted`, titles `--text`.
5. Tooltips: dark surface, `--surface-border` 1px, no colour boxes — a 8px dot,
   tabular-nums values, `mode: "index", intersect: false` on time series.
6. ≥2 series → an HTML `.chart-legend` (the dashboard pattern), never the stock
   Chart.js legend. 1 series → no legend, the panel `<h3>` names it.
7. Re-run the palette validator if you touch any `--s-*` value.

---

## Part 3 — Work packages

Each WP is self-contained: one branch, one commit range, its own done-criteria.
Ownership is by **file** to keep merges trivial.

### WP-0 — Foundation (integrator, lands first, everything else rebases on it)

- `style.css`: add the token block; replace `#2a333d` / `#10161d` / literal radii
  and spacing with tokens repo-wide. No visual change intended beyond 1.6/1.7.
- New `static/chart-theme.js`: `Chart.defaults` for font, colour, grid, border,
  tooltip, point/line geometry; export `cssVar(name)` and
  `linkedCrosshair(charts)` for the split-panel pattern.
- `base.html`: load `chart-theme.js` after the zoom plugin.
- Focus-visible, `.num`, `.badge` contrast fix, nav active state.

**Done when:** every page renders unchanged in layout, tokens resolve, and no
literal `#2a333d`/`#10161d` remains in `style.css`.

**LANDED.** Notes for everyone rebasing on it:

- `chart-theme.js` is the shared chart API. All globals; use them rather than
  rolling your own, and add to it rather than copying into a page script:
  - `cssVar(name, fallback)` — read a token.
  - `tokenAlpha(name, alpha, fallback)` — tokens are opaque `#rrggbb`, so
    translucent fills (area fills, the elevation band, zoom drag boxes) go
    through this.
  - `monthYearTicks(labels)` — `ticks.callback` for a dated category axis.
    Format follows the window: day-of-month under ~10 weeks, month names above.
  - `dateAxisTicks(maxTicks)` — `afterBuildTicks` for the same axis. **Use this
    instead of `autoSkip` on any stacked panel.** Chart.js gates `autoSkip` on
    `ticks.display`, so a panel that draws no labels never thins and its
    gridlines fall out of column with the labelled one. Set
    `ticks.autoSkip: false` alongside it.
  - `alignPanels(charts)` / `schedulePanelAlign(charts)` — give N stacked
    panels a common left edge (each y axis is otherwise sized to its own tick
    text) and a common right inset (only the labelled panel reserves room for
    its last label to overhang). Callers must declare
    `layout: { padding: { left: 0, right: 0 } }` at construction — on v4,
    assigning a nested object into `chart.options` afterwards recurses into a
    stack overflow, and Chart.js's own default is the scalar `padding: 0`,
    which silently swallows a later `.left`.
  - `linkedCrosshair(charts) → {destroy()}` — one hover reads every panel.
- `Chart.defaults.scales.<type>` shadows `Chart.defaults.scale` — `radialLinear`
  hard-codes tick colour `#666`, for instance. `chart-theme.js` loops every
  registered scale type, so this is handled; do not assume writing
  `defaults.scale` alone is enough if you register a new scale type.
- Panel borders cost **+2px height and −2px inner width** everywhere, plus +2px
  outer width on the one content-sized `.card` per row (`box-sizing: border-box`
  only absorbs a border into an element that has a set size). Measured at
  1600/1280/900px: nothing re-wraps, worst page grows 8px.
- `plugins.legend.display` was deliberately **left at its default**. Charts not
  yet migrated still use the stock legend; turning it off globally would have
  silently removed legends from them. Each WP opts into `.chart-legend` itself.
- Spacing literals were **not** swept — the freehand rem values are entangled with
  layout and a blind sweep shifts the page. The `--sp-*` tokens exist; convert
  spacing opportunistically within the rules your WP already touches, never as a
  standalone sweep.
- `--grid` dropped from `.12` to `.07`. That is right for the analysis pages and
  wrong for the ride chart, which is read at arm's length; `--grid-strong` was
  added for it and `ride.html:339` already points at it.
- The nav is now a `(href, label)` loop in `base.html` setting `aria-current` from
  `request.url.path`. Adding a page means adding a tuple, not a new `<a>`.
- Radii below the ladder were left alone on purpose: `2px` (`.legend-swatch`,
  `.erg-led`), `50%` (`.spinner`), `0` (fullscreen).
- Known cosmetic deltas already visible, each owned by a later WP: volume bars
  inherited rounded 4px tops (WP-2 wants them anyway); `.zone-bar` went 3px → 4px
  on an ~9px-tall bar and now reads pill-shaped (WP-3 rebuilds it);
  `.erg-toggle.erg-on` ink swept to `--surface-inset` where it semantically wants
  `--on-accent` (value-identical today — WP-8's call).
- Regression found and stopgapped: the profile radar lost its vertex dots and hit
  targets to the global `point.radius: 0`; `pointRadius: 3` / `pointHitRadius: 8`
  are restated in `profile.html`'s `dataset()` helper. WP-4 deletes all of it.

### Agent 1 (integrator) — the JSON-fed Chart.js surfaces

| WP | Scope | Files owned |
|----|-------|-------------|
| **WP-1** | Dashboard: split the dual axis into two linked panels (1.2); restyle via defaults; power–duration curve gets real axis styling and a log-x tick treatment that isn't stock | `app.js` (dashboard + curve sections), `dashboard.html` |
| **WP-2** | Volume: four stacked charts → proper small multiples — one shared x axis, labels only on the bottom chart, 2px inter-bar gap, rounded 4px data-ends, per-chart direct-labelled latest value | `volume.js`, `volume.html` |
| **WP-3** | Activity detail: split power / HR+cadence panels (1.2), replace stock legend with `.chart-legend`, rebuild `.zone-bar` as a full-width proportional track (1.10) | `app.js` (detail section), `activity_detail.html` |

### Agent 2 — the server-rendered and inline-SVG surfaces

| WP | Scope | Files owned |
|----|-------|-------------|
| **WP-4** | Power profile: replace the radar with the horizontal grouped-bar/dumbbell form (1.3). One call site. Keep `.power-radar-wrap`'s `clamp(300px,36vw,440px)` footprint and the `.sr-only` table alternative at `profile.html:75`. **Priority — WP-0 regressed this chart:** the global `elements.point.radius: 0` killed the radar's vertex dots and its hit-testing, so the tooltip (the only place raw watts/W·kg are shown) is now near-unhittable. A `pointRadius: 3, pointHitRadius: 8` in the `dataset()` helper is the stopgap if WP-4 will not land immediately | `profile.html`, new `static/power_profile.js` |
| **WP-5** | Workout profile SVG: non-scaling strokes, CSS-driven label sizing, per-zone segment fill, lift the 760px cap (1.9) | `workout_graph.js`, `.profile-svg` / `.profile-wrap` rules in `style.css` |
| **WP-6** | Tables + chrome: shared `.data-table` treatment — numeric right-align + tabular-nums, row hover, sticky header, zebra (1.8). Applies to activities, races, plan, settings, profile tables | `activities.html`, `races.html`, `plan.html`, `settings.html`, `power_corrections.html`, table rules in `style.css` |
| **WP-7** | Calendar: cell rhythm on the spacing scale, badge/tag restyle on `--r-sm`, hatch and status treatments re-derived from tokens; keep every existing status distinction (completed / adapted / skipped / missed / OOTO / race A-B / phase) | `calendar.html`, calendar rules in `style.css` |

**Deliberately left for last / not assigned yet:** `ride.html` (1578 lines, live
WebSocket rendering, fullscreen, device LEDs). It is the highest-risk file and the
hardest to verify without hardware. Do it as WP-8 only after WP-0…7 are merged,
and restrict it to swapping literals for tokens and series colours for `--s-*`.
Do not restructure the ride chart.

---

## Part 4 — Coordination protocol

Follow `AGENTS.md` (worktrees, `git config user.email wattrackerboss@…`, only the
integrator pushes `main`, full `pytest` green before merge). On top of that:

1. **WP-0 lands on `main` before any other WP starts.** Agent 2 cuts
   `agent2/ui-wp4`… *from* that commit. Do not start against the pre-token tree.
2. **`style.css` is the only shared file.** It is partitioned by the existing
   section comments. Agent 1 touches `:root`, chart, card and zone rules; Agent 2
   touches table, calendar, `.profile-svg` and modal rules. **Never reformat or
   reorder a section you don't own** — that turns a 3-line diff into a conflict.
3. **`dashboard.html` belongs entirely to Agent 1.** (An earlier draft split it
   for a dashboard radar block — there is no radar there; that canvas is the
   power–duration curve. No split needed.)
4. **One WP per branch, one branch in flight per agent.** Push and report "WP-n
   ready to merge"; the integrator rebases and merges.
5. **No new tokens outside WP-0.** Need one? Ask; it gets added to WP-0 and both
   branches rebase.
6. **No layout-size changes without a note.** If a WP makes a section more than
   ~15% taller or shorter, say so in the handoff — the owner's constraint is
   "same information, roughly the same screen space".
7. **Verification per WP:** `.venv/bin/python -m pytest` green, plus load the
   affected pages in the running app and confirm each chart renders with real
   data *and* with the empty-state path (every chart here has one).

## Part 5 — Order of work

```
WP-0  (integrator)          ← blocks everything
  ├── Agent 1: WP-1 → WP-2 → WP-3
  └── Agent 2: WP-4 → WP-6 → WP-5 → WP-7
WP-8 (ride)                 ← after all of the above are on main
```

Agent 2: start at **WP-4**, it is the one with a real form change in it and the
most visible payoff. WP-6 next because it touches the most pages.
