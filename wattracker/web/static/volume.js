// Weekly training-volume bar charts. Data comes from /api/volume.
//
// The four charts share one x-window: presets (1m/1y/All) or a custom date
// range slice the dense weekly series and re-set labels/data on all four.
// A horizontal drag on any chart (chartjs-plugin-zoom, same gesture as the
// dashboard) re-windows all four to the dragged range and switches to Custom.

async function fetchVolume() {
    const resp = await fetch("/api/volume");
    if (!resp.ok) return { weeks: [] };
    return await resp.json();
}

// Bar colour per metric (matches the app's dashboard palette).
const VOLUME_COLORS = {
    hours: "#4caf7d",
    tss: "#f2a900",
    distance_km: "#5a9bd4",
    calories: "#e05252",
};

const CHART_SPECS = [
    { id: "hoursChart", label: "Hours", key: "hours" },
    { id: "tssChart", label: "TSS", key: "tss" },
    { id: "distanceChart", label: "Distance (km)", key: "distance_km" },
    { id: "caloriesChart", label: "Calories", key: "calories" },
];

// Module state for windowing.
let dense = [];          // gap-filled full weekly series
const charts = {};       // canvasId -> Chart
let winStart = 0;        // current window absolute index (inclusive)
let winEnd = 0;
let applying = false;    // re-entrancy guard while re-windowing

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

function makeBarChart(spec) {
    const el = document.getElementById(spec.id);
    if (!el) return null;
    const color = VOLUME_COLORS[spec.key] || "#999";
    return new Chart(el, {
        type: "bar",
        data: {
            labels: dense.map((w) => w.week_start),
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
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: (items) => (items.length ? "Week of " + items[0].label : ""),
                        label: (item) => spec.label + ": " + item.parsed.y,
                    },
                },
                zoom: {
                    zoom: {
                        drag: { enabled: true, backgroundColor: "rgba(242,169,0,0.15)" },
                        mode: "x",
                        onZoomComplete: ({ chart }) => onDragZoom(chart),
                    },
                },
            },
            scales: {
                x: {
                    ticks: {
                        color: "#c2cad4", maxTicksLimit: 16, maxRotation: 0,
                        autoSkip: true,
                    },
                    grid: {
                        color: "rgba(138,148,160,0.10)",
                        tickColor: "rgba(194,202,212,0.8)",
                    },
                    border: { color: "rgba(138,148,160,0.6)" },
                },
                y: {
                    beginAtZero: true,
                    title: { display: true, text: spec.label },
                    ticks: { color: "#c2cad4" },
                    grid: { color: "rgba(138,148,160,0.10)" },
                },
            },
        },
    });
}

// A drag on one chart selects a range in that chart's CURRENT (windowed)
// labels; map those local indices back to absolute indices in `dense` and
// re-window all four charts to match.
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
// four charts, clearing any drag-zoom transform, then reflect the window into
// the custom date inputs.
function setWindow(lo, hi) {
    if (!dense.length) return;
    lo = Math.max(0, Math.min(lo, dense.length - 1));
    hi = Math.max(lo, Math.min(hi, dense.length - 1));
    winStart = lo;
    winEnd = hi;
    const slice = dense.slice(lo, hi + 1);
    applying = true;
    CHART_SPECS.forEach((spec) => {
        const c = charts[spec.id];
        if (!c) return;
        if (c.resetZoom) c.resetZoom("none");
        c.data.labels = slice.map((w) => w.week_start);
        c.data.datasets[0].data = slice.map((w) => w[spec.key]);
        c.update("none");
    });
    applying = false;
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
    const controls = document.getElementById("volumeControls");
    if (controls) controls.style.display = "";
    renderSummary(dense);
    CHART_SPECS.forEach((spec) => { charts[spec.id] = makeBarChart(spec); });
    winStart = 0;
    winEnd = dense.length - 1;
    wireControls();
    // Seed the custom inputs with the full (default "All") window.
    const from = document.getElementById("volFrom");
    const to = document.getElementById("volTo");
    if (from) from.value = dense[0].week_start;
    if (to) to.value = dense[dense.length - 1].week_start;
}
