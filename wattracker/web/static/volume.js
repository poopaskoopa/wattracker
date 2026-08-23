// Weekly training volume. Data comes from /api/volume.
//
// Four metrics, but not four plots. Hours, TSS, distance and calories are the
// same rides measured four ways, so four full-width panels drew the same shape
// four times down a 1400px column - four charts' worth of ink for one chart's
// worth of information. The layout instead splits the two questions apart:
//
//   "how am I doing across the board?"  -> the tile row. Four cards, each with
//        its 4-week total, its change against the previous four, and a
//        sparkline of the whole history. Small multiples at their honest size.
//   "what happened, week by week?"      -> ONE hero chart, showing whichever
//        metric the reader picked. The tiles are the selector (aria-pressed,
//        exactly one active), so choosing a metric and reading its summary are
//        the same gesture.
//
// The hero chart pairs the weekly bars with a trailing 4-week mean on a SINGLE
// y axis - they are the same quantity in the same unit, which is the whole
// reason the pairing is honest. A second y scale would not be.

async function fetchVolume() {
    const resp = await fetch("/api/volume");
    if (!resp.ok) return { weeks: [] };
    return await resp.json();
}

// The four metrics, in tile order. `token` is the series colour (never a
// literal - see docs/ui-refresh Part 2); `label` names the metric in the tile,
// the y-axis title and the tooltip; `digits` formats both the tile total and
// the direct-labelled latest value.
//
// One metric is plotted at a time and it wears one hue. The four series tokens
// were validated as ADJACENT pairs, not as a four-way set: co-plotted, their
// worst all-pairs separation is deltaE 4.8 under deuteranopia, which is a fail.
// Bars and the mean line therefore share the active metric's token and are
// told apart by form, not by colour.
const METRICS = [
    { key: "hours", label: "Hours", token: "--s-1", digits: 1 },
    { key: "tss", label: "TSS", token: "--s-2", digits: 0 },
    { key: "distance_km", label: "Distance (km)", token: "--s-3", digits: 0 },
    { key: "calories", label: "Calories", token: "--s-4", digits: 0 },
];

// Trailing window for the mean line, in weeks. Same 4 weeks the tiles total
// over, so the line and the summary answer with the same horizon.
const MEAN_WEEKS = 4;

// Module state.
let dense = [];          // gap-filled full weekly series
let means = {};          // metric key -> rolling mean over the FULL series
let chart = null;        // the one chart
let activeKey = METRICS[0].key;
let winStart = 0;        // current window absolute index (inclusive)
let winEnd = 0;
let applying = false;    // re-entrancy guard while re-windowing

function activeMetric() {
    return METRICS.find((m) => m.key === activeKey) || METRICS[0];
}

// Add days to an ISO date (yyyy-mm-dd), returning a new ISO date. Uses UTC to
// avoid any local-timezone drift on the pure-date arithmetic.
function addDays(iso, days) {
    const d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
}

// Given the API weeks (only active Mondays), produce a dense series from the
// first to the last week with every intervening Monday present, missing weeks
// filled with zeros so gaps render as zero-height bars.
function fillWeeks(weeks) {
    if (!weeks.length) return [];
    const byWeek = {};
    weeks.forEach((w) => { byWeek[w.week_start] = w; });
    const out = [];
    let cur = weeks[0].week_start;
    const last = weeks[weeks.length - 1].week_start;
    // Guard against a malformed range: cap iterations generously.
    for (let i = 0; i < 5000 && cur <= last; i++) {
        out.push(byWeek[cur] || {
            week_start: cur, hours: 0, tss: 0, distance_km: 0, calories: 0,
        });
        cur = addDays(cur, 7);
    }
    return out;
}

// ------------------------------------------------------------- rolling mean
// Trailing mean over the DENSE series, so a week off counts as the zero it
// was - averaging only the weeks that happen to have rides would report a
// training load nobody carried.
//
// Computed once over the whole history and then sliced with the window, never
// recomputed per window: recomputing would restart the ramp-in at whatever
// week the current zoom begins, so the first four bars of every zoom level
// would show a mean that is an artifact of the zoom rather than of the
// training.
//
// The first weeks average what exists rather than sitting at null; a gap at
// the left edge of the line reads as missing data, which it is not.
function rollingMean(values, window) {
    const out = [];
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
        sum += values[i] || 0;
        if (i >= window) sum -= values[i - window] || 0;
        out.push(sum / Math.min(i + 1, window));
    }
    return out;
}

function computeMeans() {
    means = {};
    METRICS.forEach((m) => {
        means[m.key] = rollingMean(dense.map((w) => w[m.key] || 0), MEAN_WEEKS);
    });
}

// ------------------------------------------------------------- bar spacing
// The design calls for 2px of surface between adjacent bars, but the bar count
// moves over two orders of magnitude with the window: ~5 on the 1m preset,
// several hundred on All. A fixed barPercentage tuned at 5 bars leaves ~1px of
// ink at 300; a fixed 2px gap subtracted at 300 bars erases them outright,
// because the whole category slot is only ~3px wide there.
//
// So the gap is a target, not a constant: 2px wherever the slot can afford it,
// degrading to a quarter of the slot once it cannot, which keeps the bars
// readable as bars all the way down. `maxBarThickness` caps the other end -
// without it, five weeks across a 1000px chart would render as five 200px
// mesas rather than bars.
const BAR_SPACING_PLUGIN = {
    id: "volumeBarSpacing",
    beforeUpdate(chart) {
        const ds = (chart.data.datasets || [])[0];
        const n = (chart.data.labels || []).length;
        if (!ds || !n) return;
        // chartArea is last layout's; on the very first pass it does not exist
        // yet, so approximate and let the next update converge.
        const area = chart.chartArea;
        const plot = area && area.width > 0 ? area.width : chart.width * 0.9;
        const slot = plot / n;
        const gap = Math.min(2, Math.max(0.5, slot * 0.25));
        ds.categoryPercentage = 1;
        ds.barPercentage = Math.max(0.25, (slot - gap) / slot);
        ds.maxBarThickness = 40;
    },
};

// ------------------------------------------------- direct latest-value label
// The number worth reading is the most recent week, so it is written next to
// its own bar rather than left for the tooltip. Text token, never the series
// colour - the bar already carries the hue, and colouring the text too would
// imply it encodes something.
//
// It cannot collide with the bar: the y scale carries `grace` so the tallest
// bar always stops short of the top, and the label is clamped into that
// headroom (and to the plot edges) rather than allowed to run off.
const LATEST_LABEL_PLUGIN = {
    id: "volumeLatestLabel",
    afterDatasetsDraw(chart) {
        const spec = chart.$volumeMetric;
        const area = chart.chartArea;
        if (!spec || !area) return;
        const meta = chart.getDatasetMeta(0);
        const bars = (meta && meta.data) || [];
        const values = (chart.data.datasets[0] || {}).data || [];
        const i = bars.length - 1;
        if (i < 0 || values[i] == null) return;
        const bar = bars[i];
        const text = Number(values[i]).toFixed(spec.digits);

        const ctx = chart.ctx;
        const size = 11;
        ctx.save();
        ctx.font = "600 " + size + "px " + Chart.defaults.font.family;
        ctx.fillStyle = cssVar("--text", "#e6e6e6");
        ctx.textBaseline = "bottom";
        const w = ctx.measureText(text).width;
        let x = bar.x;
        ctx.textAlign = "center";
        if (x + w / 2 > area.right) { x = area.right; ctx.textAlign = "right"; }
        else if (x - w / 2 < area.left) { x = area.left; ctx.textAlign = "left"; }
        const y = Math.max(bar.y - 4, area.top + size);
        ctx.fillText(text, x, y);
        ctx.restore();
    },
};

// ---------------------------------------------------------------- x ticks
// Tick selection is `dateAxisTicks` in chart-theme.js, shared with the
// dashboard's dated category axes. 16 rather than the dashboard's 12: this
// chart is the full page width and its legend sits above the plot, so nothing
// competes with the tick row.
const MAX_X_TICKS = 16;

// Both datasets for one metric over one slice of the dense series. Bars carry
// the raw week at 45% alpha so the mean line stays legible across them; the
// line is the same token at full strength, 2px, lightly smoothed, with no
// point markers - it is a summary, not a set of observations.
function datasetsFor(spec, lo, hi) {
    const color = cssVar(spec.token);
    return [
        {
            label: "Weekly",
            data: dense.slice(lo, hi + 1).map((w) => w[spec.key]),
            backgroundColor: tokenAlpha(spec.token, 0.45),
            borderWidth: 0,
            order: 2,
            pointStyle: "rect",
        },
        {
            type: "line",
            label: MEAN_WEEKS + "-wk average",
            data: (means[spec.key] || []).slice(lo, hi + 1),
            borderColor: color,
            backgroundColor: color,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 0,
            pointHoverRadius: 4,
            order: 1,
            pointStyle: "line",
        },
    ];
}

function makeChart() {
    const el = document.getElementById("volumeChart");
    if (!el) return null;
    const spec = activeMetric();
    const labels = dense.map((w) => w.week_start);
    const c = new Chart(el, {
        type: "bar",
        data: { labels, datasets: datasetsFor(spec, 0, dense.length - 1) },
        options: {
            responsive: true,
            // Height is CSS (.volume-chart); the canvas fills its box rather
            // than deriving a height from a width ratio.
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                // Two series in one hue: the legend is what says which form is
                // which, so unlike the old one-series panels it stays on. The
                // explicit sort undoes Chart.js ordering legend items by
                // `order` - that key exists here only to draw the line over
                // the bars, and reading "4-wk average, Weekly" inverts which
                // of the two is the measurement and which is the summary.
                legend: {
                    display: true, position: "top", align: "end",
                    labels: { sort: (a, b) => a.datasetIndex - b.datasetIndex },
                },
                tooltip: {
                    callbacks: {
                        title: (items) => (items.length ? "Week of " + items[0].label : ""),
                        label: (item) => {
                            const m = item.chart.$volumeMetric;
                            return item.dataset.label + ": " +
                                Number(item.parsed.y).toFixed(m ? m.digits : 1);
                        },
                    },
                },
                zoom: {
                    zoom: {
                        drag: {
                            enabled: true,
                            backgroundColor: tokenAlpha("--accent", 0.15, "#f2a900"),
                        },
                        mode: "x",
                        onZoomComplete: ({ chart }) => onDragZoom(chart),
                    },
                },
            },
            scales: {
                x: {
                    afterBuildTicks: dateAxisTicks(MAX_X_TICKS),
                    ticks: {
                        maxRotation: 0, autoSkip: false,
                        callback: monthYearTicks(labels),
                    },
                },
                y: {
                    beginAtZero: true,
                    // Headroom for the direct label above the tallest bar.
                    grace: "12%",
                    ticks: { maxTicksLimit: 5 },
                    title: { display: true, text: spec.label },
                },
            },
        },
        plugins: [BAR_SPACING_PLUGIN, LATEST_LABEL_PLUGIN],
    });
    c.$volumeMetric = spec;
    return c;
}

// A drag selects a range in the chart's CURRENT (windowed) labels; map those
// local indices back to absolute indices in `dense` and re-window.
function onDragZoom(c) {
    if (applying) return;
    const sx = c.scales.x;
    if (!sx || sx.min == null || sx.max == null) return;
    const lo = winStart + Math.round(sx.min);
    const hi = winStart + Math.round(sx.max);
    // A near-0px drag is a click, not a range selection. Below a floor of 2
    // weekly buckets the plugin still fires and still transforms the chart's
    // own scales, so without a floor a stray click would zoom to a single bar
    // - recoverable, but only via Reset zoom or a preset, which is a trap for
    // an accidental gesture. Leave the window and preset alone and cancel the
    // chart's own zoom transform so it snaps back to what winStart/winEnd
    // already say, rather than sitting zoomed to a state module state
    // disagrees with. resetZoom itself re-fires onZoomComplete (with the
    // now-full scale), so guard with `applying` or that reentrant call would
    // sail past the floor and land here again, this time accepted.
    if (hi - lo < 1) {
        if (c.resetZoom) {
            applying = true;
            c.resetZoom("none");
            applying = false;
        }
        return;
    }
    setPreset("custom");
    setWindow(lo, hi);
}

// Slice `dense` to [lo, hi] (absolute, inclusive) and repaint, clearing any
// drag-zoom transform, then reflect the window into the custom date inputs.
function setWindow(lo, hi) {
    if (!dense.length) return;
    lo = Math.max(0, Math.min(lo, dense.length - 1));
    hi = Math.max(lo, Math.min(hi, dense.length - 1));
    winStart = lo;
    winEnd = hi;
    repaint();
    const slice = dense.slice(lo, hi + 1);
    const from = document.getElementById("volFrom");
    const to = document.getElementById("volTo");
    if (from) from.value = slice[0].week_start;
    if (to) to.value = slice[slice.length - 1].week_start;
}

// Push the active metric over the current window into the chart. Everything
// that varies with either - data, colours, y title, the label formatter the
// direct label and tooltip read - is a leaf assignment, so switching metric
// and switching window go through one path and cannot disagree.
function repaint() {
    if (!chart) return;
    const spec = activeMetric();
    const labels = dense.slice(winStart, winEnd + 1).map((w) => w.week_start);
    applying = true;
    if (chart.resetZoom) chart.resetZoom("none");
    chart.$volumeMetric = spec;
    chart.data.labels = labels;
    chart.data.datasets = datasetsFor(spec, winStart, winEnd);
    // The tick formatter closes over the label array, so a new window needs a
    // new closure or the ticks name the old dates.
    chart.options.scales.x.ticks.callback = monthYearTicks(labels);
    chart.options.scales.y.title.text = spec.label;
    chart.update("none");
    applying = false;
}

// Highlight the active preset button; `name` of "custom" clears all.
function setPreset(name) {
    document.querySelectorAll("#volumeControls .range-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.preset === name);
    });
}

function applyPreset(name) {
    const n = dense.length;
    if (!n) return;
    if (name === "1m") {
        setWindow(n - 5, n - 1);   // last ~4-5 weekly buckets
    } else if (name === "1y") {
        setWindow(n - 52, n - 1);  // last 52 weekly buckets
    } else {
        setWindow(0, n - 1);       // All
    }
    setPreset(name);
}

// Custom from/to dates map to the nearest enclosed weekly buckets. Empty or
// inverted (from > to) inputs are ignored (window unchanged), never crash.
function applyCustom() {
    const fromEl = document.getElementById("volFrom");
    const toEl = document.getElementById("volTo");
    const from = fromEl ? fromEl.value : "";
    const to = toEl ? toEl.value : "";
    if (!from || !to || from > to || !dense.length) return;
    const lo = dense.findIndex((w) => w.week_start >= from);
    if (lo < 0) return;                 // range starts after the last bucket
    let hi = -1;
    for (let i = 0; i < dense.length; i++) {
        if (dense[i].week_start <= to) hi = i;
    }
    if (hi < lo) return;                // no bucket falls inside the range
    setPreset("custom");
    setWindow(lo, hi);
}

// Make `key` the plotted metric. Exactly one tile is pressed at a time; the
// pressed state is carried by aria-pressed (for assistive tech) and by a rule
// the unpressed tiles simply do not draw (for everyone else), never by colour
// alone.
function setMetric(key) {
    if (!METRICS.some((m) => m.key === key)) return;
    activeKey = key;
    document.querySelectorAll("#volumeSummary .metric-tile").forEach((btn) => {
        btn.setAttribute("aria-pressed", String(btn.dataset.key === key));
    });
    repaint();
}

// --------------------------------------------------------------- sparklines
// Inline SVG rather than a fifth and sixth and seventh Chart.js instance:
// four more canvases to size, theme and destroy would cost far more than four
// polylines are worth. `preserveAspectRatio="none"` lets the viewBox be the
// data's own coordinate space (x = week index, y = value) and the CSS box do
// all the scaling, so there is no layout maths here at all; the strokes carry
// `vector-effect` so that non-uniform scale cannot smear them.
//
// aria-hidden: the tile's total and delta already state the number. The
// sparkline is the shape of that number over time and has no separate reading
// a screen reader could usefully be given.
const SPARK_HEIGHT = 100;   // viewBox units, not pixels

function sparklineSvg(values, token) {
    // One week of history has no line, and an all-zero history has no scale:
    // both are real states for a new account, so flatten rather than divide by
    // zero. A duplicated point gives the single week something to be flat
    // against.
    const ys = values.length > 1 ? values : [values[0] || 0, values[0] || 0];
    const span = ys.length - 1;
    const max = Math.max.apply(null, ys.concat([0]));
    const scale = max > 0 ? SPARK_HEIGHT / max : 0;
    const pts = ys.map((v, i) => i + "," + (SPARK_HEIGHT - (v || 0) * scale).toFixed(2));
    const area = "M0," + SPARK_HEIGHT + " L" + pts.join(" L") +
        " L" + span + "," + SPARK_HEIGHT + " Z";
    return '<svg class="sparkline" viewBox="0 0 ' + span + " " + SPARK_HEIGHT +
        '" preserveAspectRatio="none" aria-hidden="true" focusable="false">' +
        '<path d="' + area + '" fill="' + tokenAlpha(token, 0.18) + '"></path>' +
        '<polyline points="' + pts.join(" ") + '" fill="none" stroke="' +
        cssVar(token) + '" stroke-width="1.5" vector-effect="non-scaling-stroke" ' +
        'stroke-linejoin="round"></polyline></svg>';
}

// --------------------------------------------------------------- tile row
// Last-4-weeks total vs the previous four, per metric. Always computed over
// ALL data (the dense series), not the visible window: the tiles are the
// standing summary, and a total that moved every time you dragged the chart
// would be a different statistic each time you read it.
function sumOver(weeks, key) {
    return weeks.reduce((acc, w) => acc + (w[key] || 0), 0);
}

function pctChange(current, previous) {
    // A zero baseline has no percentage - 0/0 is undefined and x/0 is
    // infinite - regardless of what `current` is. null => no baseline.
    if (previous === 0) return null;
    return ((current - previous) / previous) * 100;
}

// The head (heading, period pill, note) is markup, not state, so it lives in
// the template and only the cards are rewritten here - `el` is the section,
// `cards` the container inside it. Clearing the section itself would delete the
// head the first time the tiles were drawn.
function renderSummary(all) {
    const el = document.getElementById("volumeSummary");
    const cards = document.getElementById("volumeSummaryTiles");
    if (!el || !cards) return;
    const last4 = all.slice(-4);
    const prev4 = all.slice(-8, -4);
    if (last4.length < 1) return;
    el.style.display = "";
    cards.innerHTML = "";
    // Whether any tile ended up with a real percentage. If none did, the note
    // must not claim a comparison against the preceding four that the tiles are
    // simultaneously denying with "no prior" / "no baseline".
    let compared = false;
    METRICS.forEach((m) => {
        const cur = sumOver(last4, m.key);
        const prev = sumOver(prev4, m.key);
        const pct = pctChange(cur, prev);
        let deltaHtml;
        if (pct === null) {
            // Two different reasons pct can be null and they read differently:
            // no earlier window at all (a new account) vs. an earlier window
            // that logged nothing (a zero baseline, whatever `cur` is now).
            if (prev4.length === 0) {
                deltaHtml = '<span class="label">last 4 wks (no prior)</span>';
            } else {
                deltaHtml = '<span class="label">last 4 wks (no baseline)</span>';
            }
        } else {
            compared = true;
            const arrow = pct > 0 ? "▲" : (pct < 0 ? "▼" : "▬");
            const sign = pct > 0 ? "+" : "";
            deltaHtml = '<span class="label">' + arrow + " " + sign +
                pct.toFixed(0) + "% vs prev 4</span>";
        }
        // A real control, not a div with a click handler: focusable, operable
        // from the keyboard, and announced as a pressed/unpressed toggle.
        const tile = document.createElement("button");
        tile.type = "button";
        tile.className = "card metric-tile";
        tile.dataset.key = m.key;
        tile.setAttribute("aria-pressed", String(m.key === activeKey));
        tile.innerHTML =
            '<span class="label">' + m.label + "</span>" +
            '<span class="value">' + cur.toFixed(m.digits) + "</span>" +
            sparklineSvg(all.map((w) => w[m.key] || 0), m.token) +
            deltaHtml;
        cards.appendChild(tile);
    });
    const compare = document.getElementById("volumeSummaryCompare");
    if (compare) compare.style.display = compared ? "" : "none";
}

function wireControls() {
    document.querySelectorAll("#volumeControls .range-btn").forEach((btn) => {
        btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
    });
    const apply = document.getElementById("volApply");
    if (apply) apply.addEventListener("click", applyCustom);
    const reset = document.getElementById("volResetZoom");
    if (reset) reset.addEventListener("click", () => applyPreset("all"));
    // Delegated from the SECTION, not from the cards container, so the tiles
    // can be re-rendered (container contents and all) without re-wiring them.
    // The head is inside this element too, but it holds no .metric-tile, so a
    // click on the heading or the pill resolves to null and does nothing.
    const tiles = document.getElementById("volumeSummary");
    if (tiles) {
        tiles.addEventListener("click", (e) => {
            const tile = e.target.closest(".metric-tile");
            if (tile) setMetric(tile.dataset.key);
        });
    }
}

async function renderVolume() {
    const data = await fetchVolume();
    dense = fillWeeks(data.weeks || []);
    if (!dense.length) {
        const empty = document.getElementById("volumeEmpty");
        if (empty) empty.style.display = "block";
        return;
    }
    const block = document.getElementById("volumeBlock");
    if (block) block.style.display = "";
    computeMeans();
    renderSummary(dense);
    winStart = 0;
    winEnd = dense.length - 1;
    chart = makeChart();
    wireControls();
    // Seed the custom inputs with the full (default "All") window.
    const from = document.getElementById("volFrom");
    const to = document.getElementById("volTo");
    if (from) from.value = dense[0].week_start;
    if (to) to.value = dense[dense.length - 1].week_start;
}
