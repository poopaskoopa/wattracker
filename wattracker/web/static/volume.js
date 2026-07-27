// Weekly training-volume small multiples. Data comes from /api/volume.
//
// Four metrics, four stacked panels, ONE x axis: the panels share a single
// window (presets 1m/1y/All or a custom date range) that slices the dense
// weekly series and re-sets labels/data on all four. Only the bottom panel
// draws the date labels; `alignPanels` (chart-theme.js) keeps every panel's
// plot area on the same left and right edges so the gridlines stay in column.
// A horizontal drag on any panel (chartjs-plugin-zoom, same gesture as the
// dashboard) re-windows all four to the dragged range and switches to Custom.

async function fetchVolume() {
    const resp = await fetch("/api/volume");
    if (!resp.ok) return { weeks: [] };
    return await resp.json();
}

// One entry per panel, top to bottom. `token` is the series colour (never a
// literal - see docs/ui-refresh Part 2); `label` is both the y-axis title and
// the tooltip's series name; `digits` formats the direct-labelled latest value.
const CHART_SPECS = [
    { id: "hoursChart", label: "Hours", key: "hours", token: "--s-1", digits: 1 },
    { id: "tssChart", label: "TSS", key: "tss", token: "--s-2", digits: 0 },
    { id: "distanceChart", label: "Distance (km)", key: "distance_km", token: "--s-3", digits: 0 },
    { id: "caloriesChart", label: "Calories (kcal)", key: "calories", token: "--s-4", digits: 0 },
];

// Module state for windowing.
let dense = [];          // gap-filled full weekly series
const charts = {};       // canvasId -> Chart
let winStart = 0;        // current window absolute index (inclusive)
let winEnd = 0;
let applying = false;    // re-entrancy guard while re-windowing
let panelCrosshair = null;

function panelCharts() {
    return CHART_SPECS.map((s) => charts[s.id]).filter((c) => c && c.ctx);
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
// without it, five weeks across a 1000px panel would render as five 200px
// mesas rather than bars.
const BAR_SPACING_PLUGIN = {
    id: "volumeBarSpacing",
    beforeUpdate(chart) {
        const ds = (chart.data.datasets || [])[0];
        const n = (chart.data.labels || []).length;
        if (!ds || !n) return;
        // chartArea is last layout's; on the very first pass it does not exist
        // yet, so approximate and let the next update converge (alignPanels
        // runs two update passes over every panel anyway).
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
// One panel, one series, no legend: the number worth reading is the most
// recent week, so it is written next to its own bar rather than left for the
// tooltip. Text token, never the series colour - the bar already carries the
// hue, and colouring the text too would imply it encodes something.
//
// It cannot collide with the bar: the y scale carries `grace` so the tallest
// bar always stops short of the top, and the label is clamped into that
// headroom (and to the plot edges) rather than allowed to run off.
const LATEST_LABEL_PLUGIN = {
    id: "volumeLatestLabel",
    afterDatasetsDraw(chart) {
        const spec = chart.$volumeSpec;
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
// Tick selection is `dateAxisTicks` in chart-theme.js - the dashboard's stacked
// fitness panels have exactly the same problem (autoSkip is gated on
// `ticks.display`, so label-free panels never thin and fall out of column) and
// a second copy would drift. 16 rather than the dashboard's 12: the volume
// panels are the full page width with no legend competing for the row.
const MAX_X_TICKS = 16;

function makeBarChart(spec, isBottom) {
    const el = document.getElementById(spec.id);
    if (!el) return null;
    const labels = dense.map((w) => w.week_start);
    const color = cssVar(spec.token);
    const chart = new Chart(el, {
        type: "bar",
        data: {
            labels,
            datasets: [{
                label: spec.label,
                data: dense.map((w) => w[spec.key]),
                backgroundColor: color,
                borderColor: color,
                borderWidth: 0,
            }],
        },
        options: {
            responsive: true,
            // Panel heights are CSS (.volume-panels); the canvas fills its
            // panel rather than deriving a height from a width ratio.
            maintainAspectRatio: false,
            onResize: scheduleVolumeAlign,
            // Declared here so alignPanels only ever writes the leaves: on
            // Chart.js v4 assigning a nested object into `chart.options` after
            // construction recurses into a stack overflow.
            layout: { padding: { left: 0, right: 0 } },
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: false }, // one series per panel; the y title names it
                tooltip: {
                    callbacks: {
                        title: (items) => (items.length ? "Week of " + items[0].label : ""),
                        label: (item) => spec.label + ": " + item.parsed.y,
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
                    // Identical tick generation on every panel keeps the
                    // gridlines in the same columns; only the bottom panel
                    // draws the labels, so the four read as one plot with one
                    // date axis.
                    afterBuildTicks: dateAxisTicks(MAX_X_TICKS),
                    ticks: {
                        display: isBottom, maxRotation: 0, autoSkip: false,
                        callback: monthYearTicks(labels),
                    },
                },
                y: {
                    beginAtZero: true,
                    // Headroom for the direct label above the tallest bar.
                    grace: "12%",
                    ticks: { maxTicksLimit: 4 },
                    title: { display: true, text: spec.label },
                },
            },
        },
        plugins: [BAR_SPACING_PLUGIN, LATEST_LABEL_PLUGIN],
    });
    chart.$volumeSpec = spec;
    return chart;
}

function scheduleVolumeAlign() {
    schedulePanelAlign(panelCharts());
}

// A drag on one panel selects a range in that panel's CURRENT (windowed)
// labels; map those local indices back to absolute indices in `dense` and
// re-window all four panels to match.
function onDragZoom(chart) {
    if (applying) return;
    const sx = chart.scales.x;
    if (!sx || sx.min == null || sx.max == null) return;
    const lo = winStart + Math.round(sx.min);
    const hi = winStart + Math.round(sx.max);
    setPreset("custom");
    setWindow(lo, hi);
}

// Slice `dense` to [lo, hi] (absolute, inclusive) and push labels+data to all
// four panels, clearing any drag-zoom transform, then reflect the window into
// the custom date inputs.
function setWindow(lo, hi) {
    if (!dense.length) return;
    lo = Math.max(0, Math.min(lo, dense.length - 1));
    hi = Math.max(lo, Math.min(hi, dense.length - 1));
    winStart = lo;
    winEnd = hi;
    const slice = dense.slice(lo, hi + 1);
    const labels = slice.map((w) => w.week_start);
    applying = true;
    CHART_SPECS.forEach((spec) => {
        const c = charts[spec.id];
        if (!c) return;
        if (c.resetZoom) c.resetZoom("none");
        c.data.labels = labels;
        c.data.datasets[0].data = slice.map((w) => w[spec.key]);
        // Leaf assignment: the tick formatter closes over the label array, so
        // a new window needs a new closure or the ticks name the old dates.
        c.options.scales.x.ticks.callback = monthYearTicks(labels);
        c.update("none");
    });
    applying = false;
    // New window, new tick text on both axes: re-measure the shared insets.
    scheduleVolumeAlign();
    const from = document.getElementById("volFrom");
    const to = document.getElementById("volTo");
    if (from) from.value = slice[0].week_start;
    if (to) to.value = slice[slice.length - 1].week_start;
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

// Compact last-4-weeks vs previous-4-weeks summary. Totals per metric with a
// percentage change; always computed over ALL data (the dense series), not the
// visible window.
const SUMMARY_METRICS = [
    { key: "hours", label: "Hours", digits: 1 },
    { key: "tss", label: "TSS", digits: 0 },
    { key: "distance_km", label: "Distance (km)", digits: 0 },
    { key: "calories", label: "Calories", digits: 0 },
];

function sumOver(weeks, key) {
    return weeks.reduce((acc, w) => acc + (w[key] || 0), 0);
}

function pctChange(current, previous) {
    if (previous === 0) return current === 0 ? 0 : null; // null => no baseline
    return ((current - previous) / previous) * 100;
}

function renderSummary(all) {
    const el = document.getElementById("volumeSummary");
    if (!el) return;
    const last4 = all.slice(-4);
    const prev4 = all.slice(-8, -4);
    if (last4.length < 1) return;
    el.style.display = "";
    el.innerHTML = "";
    SUMMARY_METRICS.forEach((m) => {
        const cur = sumOver(last4, m.key);
        const prev = sumOver(prev4, m.key);
        const pct = pctChange(cur, prev);
        let deltaHtml;
        if (pct === null) {
            deltaHtml = '<span class="label">last 4 wks (no prior)</span>';
        } else {
            const arrow = pct > 0 ? "▲" : (pct < 0 ? "▼" : "▬");
            const sign = pct > 0 ? "+" : "";
            deltaHtml = '<span class="label">' + arrow + " " + sign +
                pct.toFixed(0) + "% vs prev 4</span>";
        }
        const card = document.createElement("div");
        card.className = "card";
        card.innerHTML =
            '<span class="label">' + m.label + "</span>" +
            '<span class="value">' + cur.toFixed(m.digits) + "</span>" +
            deltaHtml;
        el.appendChild(card);
    });
}

function wireControls() {
    document.querySelectorAll("#volumeControls .range-btn").forEach((btn) => {
        btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
    });
    const apply = document.getElementById("volApply");
    if (apply) apply.addEventListener("click", applyCustom);
    const reset = document.getElementById("volResetZoom");
    if (reset) reset.addEventListener("click", () => applyPreset("all"));
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
    renderSummary(dense);
    CHART_SPECS.forEach((spec, i) => {
        charts[spec.id] = makeBarChart(spec, i === CHART_SPECS.length - 1);
    });
    winStart = 0;
    winEnd = dense.length - 1;
    // One hover reads the same week on all four panels - the point of stacking
    // them on a shared x axis in the first place.
    if (panelCrosshair) panelCrosshair.destroy();
    panelCrosshair = linkedCrosshair(panelCharts());
    scheduleVolumeAlign();
    wireControls();
    // Seed the custom inputs with the full (default "All") window.
    const from = document.getElementById("volFrom");
    const to = document.getElementById("volTo");
    if (from) from.value = dense[0].week_start;
    if (to) to.value = dense[dense.length - 1].week_start;
}
