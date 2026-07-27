// Dashboard charts. Data comes from the JSON API endpoints.

async function fetchJSON(url) {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    return await resp.json();
}

// One-line explanations shown when hovering a legend item.
const SERIES_TIPS = {
    CTL: "Chronic Training Load — 42-day average of training stress; your 'fitness'.",
    ATL: "Acute Training Load — 7-day average of training stress; your 'fatigue'.",
    TSB: "Training Stress Balance — CTL minus ATL; your 'form' (positive = fresh).",
    "Training FTP": "Training FTP — best 20-min power x 0.95, adjusted for inactivity (plus recorded points).",
};

// The fitness block is two stacked panels sharing one x axis (docs/ui-refresh
// 1.2): load on top, Training FTP below. They are separate Chart instances, so
// everything that used to be implicit in "one chart" - the legend, the hover,
// the zoom, the y-axis left edge - is wired explicitly below.
let loadChart = null;   // CTL / ATL / TSB
let ftpChart = null;    // Training FTP (estimate + recorded)
let panelCrosshair = null;
let curveChart = null;
let dashboardFtp = null; // the rider's Training FTP, for the curve reference line
let currentMonths = 1; // default view: last 1 month (0 = all)

function alignedFrom(labels, points, valueKey) {
    const m = {};
    (points || []).forEach((p) => { m[p.date] = p[valueKey]; });
    return labels.map((l) => (l in m ? m[l] : null));
}

// Like alignedFrom, but linearly interpolates between known samples so a
// sparse series (e.g. weekly FTP estimates) has a value at EVERY label. The
// drawn line is unchanged (spanGaps already drew the same straight chords),
// but the shared index-mode tooltip can now always include the series.
function interpolatedFrom(labels, points, valueKey) {
    const out = alignedFrom(labels, points, valueKey);
    const t = labels.map((l) => Date.parse(l));
    const known = [];
    out.forEach((v, i) => { if (v != null) known.push(i); });
    for (let k = 0; k + 1 < known.length; k++) {
        const i0 = known[k], i1 = known[k + 1];
        const v0 = out[i0], v1 = out[i1];
        const t0 = t[i0], t1 = t[i1];
        for (let i = i0 + 1; i < i1; i++) {
            const f = t1 > t0 ? (t[i] - t0) / (t1 - t0) : 0;
            out[i] = Math.round((v0 + (v1 - v0) * f) * 10) / 10;
        }
    }
    return out;
}

// `monthYearTicks` now lives in chart-theme.js - the volume small multiples
// want the same dated-category tick treatment, and a second copy would drift.

function unionDates(...arrays) {
    const set = new Set();
    arrays.forEach((arr) => (arr || []).forEach((p) => set.add(p.date)));
    return Array.from(set).sort();
}

// Legend groups. One entry per selectable series, naming the panel it lives on
// (the fitness block is two charts now) and the token its swatch reads from.
const LEGEND_GROUPS = [
    { label: "CTL", panel: "load", datasets: [0], token: "--s-1" },
    { label: "ATL", panel: "load", datasets: [1], token: "--s-2" },
    { label: "TSB", panel: "load", datasets: [2], token: "--s-3" },
    // estimated line + recorded points, one unit
    { label: "Training FTP", panel: "ftp", datasets: [0, 1], token: "--s-4" },
];

// The legend units for the fitness block, resolved against the live charts.
function panelLegendUnits() {
    return LEGEND_GROUPS.map((g) => ({
        label: g.label,
        chart: g.panel === "load" ? loadChart : ftpChart,
        datasets: g.datasets,
        token: g.token,
        tip: SERIES_TIPS[g.label] || g.label,
        showValue: true,
    }));
}

// Most recent value across a legend unit's datasets (a later dataset in the
// unit wins a tie on the same date, e.g. recorded FTP over the estimate).
function latestGroupValue(unit) {
    const chart = unit.chart;
    if (!chart) return null;
    const datasets = unit.datasets.map((i) => chart.data.datasets[i]);
    const n = (chart.data.labels || []).length;
    for (let i = n - 1; i >= 0; i--) {
        for (let d = datasets.length - 1; d >= 0; d--) {
            const v = ((datasets[d] || {}).data || [])[i];
            if (v != null) return v;
        }
    }
    return null;
}

// Shared legend behaviour. A unit is `{ chart, datasets }` - a chart plus the
// dataset indices in it that the entry controls (one for a simple series,
// several for a group like FTP). Units may span several charts, which is what
// the stacked fitness panels need; `target` must be one of `units`.
//   - additive (Ctrl/Cmd-click): toggle just this unit on/off, leave others.
//   - plain click: isolate this unit; clicking the already-isolated unit
//     (the only visible one) restores ALL units.
// Only the datasets listed in `units` are touched, so a background dataset
// (e.g. the ride chart's elevation band) omitted from `units` is never
// hidden or exposed by isolation.
function applyLegendUnits(units, target, additive) {
    const live = units.filter((u) => u.chart);
    const visibleOf = (u) => u.datasets.some((i) => u.chart.isDatasetVisible(i));
    if (additive) {
        const nowVisible = visibleOf(target);
        target.datasets.forEach((i) => target.chart.setDatasetVisibility(i, !nowVisible));
    } else {
        const isIsolated = live.every((u) => (u === target ? visibleOf(u) : !visibleOf(u)));
        live.forEach((u) => {
            const on = isIsolated ? true : u === target;
            u.datasets.forEach((i) => u.chart.setDatasetVisibility(i, on));
        });
    }
    const charts = [];
    live.forEach((u) => { if (charts.indexOf(u.chart) === -1) charts.push(u.chart); });
    charts.forEach((c) => c.update());
}

// Single-chart form, kept for callers that pass bare index lists against one
// chart (the activity-detail legend below).
function applyLegendSelection(chart, units, target, additive) {
    const wrapped = units.map((u) => ({ chart: chart, datasets: u }));
    applyLegendUnits(wrapped, wrapped[units.indexOf(target)], additive);
}

// Render an HTML `.chart-legend` for a set of units. Rebuilt after every click
// so the on/off styling and the latest-value readout stay in step.
function renderLegend(containerId, unitsFactory) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = "";
    const units = unitsFactory().filter((u) => u.chart);
    units.forEach((unit) => {
        const visible = unit.datasets.some((i) => unit.chart.isDatasetVisible(i));
        const item = document.createElement("span");
        item.className = "legend-item" + (visible ? "" : " off");
        item.title = unit.tip || unit.label;
        const latest = unit.showValue ? latestGroupValue(unit) : null;
        const valueHtml = latest == null
            ? ""
            : ' <span class="legend-value num">' + (Math.round(latest * 10) / 10) + "</span>";
        item.innerHTML =
            '<span class="legend-swatch" style="background:' +
            cssVar(unit.token, "#999") + '"></span>' + unit.label + valueHtml;
        item.addEventListener("click", (e) => {
            applyLegendUnits(units, unit, e.ctrlKey || e.metaKey);
            renderLegend(containerId, unitsFactory);
        });
        container.appendChild(item);
    });
}

function buildLegend() {
    renderLegend("mainLegend", panelLegendUnits);
}

// Keeping the two stacked panels on one pair of edges - a common y-axis width
// and a common right inset - is `alignPanels` in chart-theme.js, shared with
// the volume page's four-panel version of the same problem.
function scheduleAlign() {
    schedulePanelAlign([loadChart, ftpChart]);
}

// TSB is signed and "above or below zero" is the whole read, so the load panel
// gets a hairline at zero, drawn under the data. --axis rather than
// --surface-border: the border token resolves to almost exactly the composited
// grid colour on --panel, so the line would be invisible against the gridline
// already sitting at zero. --axis is the "this is a reference, not a gridline"
// step and is still one weight below any series.
const ZERO_LINE_PLUGIN = {
    id: "zeroLine",
    beforeDatasetsDraw(chart) {
        const scale = chart.scales.y;
        const area = chart.chartArea;
        if (!scale || !area || scale.min > 0 || scale.max < 0) return;
        const y = scale.getPixelForValue(0);
        const ctx = chart.ctx;
        ctx.save();
        ctx.beginPath();
        ctx.lineWidth = 1;
        ctx.strokeStyle = cssVar("--axis", "rgba(255,255,255,.20)");
        ctx.moveTo(area.left, y);
        ctx.lineTo(area.right, y);
        ctx.stroke();
        ctx.restore();
    },
};

// Drag-zoom on either panel drives the other: the two panels are one plot with
// one x axis, so a range chosen on one is meaningless if the other keeps its
// own. The guard stops the pair ping-ponging updates at each other.
let syncingPanelZoom = false;

function syncPanelX(source) {
    if (syncingPanelZoom) return;
    const other = source === loadChart ? ftpChart : loadChart;
    if (!other || !other.ctx || !source.scales.x || !other.zoomScale) return;
    syncingPanelZoom = true;
    // Through the plugin's own API, not by writing scale options: the plugin
    // caches each chart's pre-zoom limits the first time it moves that chart,
    // so a hand-written option would make "the range it was already dragged to"
    // look like the original and break Reset zoom.
    other.zoomScale("x", { min: source.scales.x.min, max: source.scales.x.max }, "none");
    syncingPanelZoom = false;
    // A new date range means new tick labels, so the overhang is re-measured.
    scheduleAlign();
}

function resetPanelZoom() {
    syncingPanelZoom = true;
    [loadChart, ftpChart].forEach((c) => {
        if (c && c.ctx && c.resetZoom) c.resetZoom("none");
    });
    syncingPanelZoom = false;
    scheduleAlign();
}

function destroyPanels() {
    if (panelCrosshair) { panelCrosshair.destroy(); panelCrosshair = null; }
    if (loadChart) { loadChart.destroy(); loadChart = null; }
    if (ftpChart) { ftpChart.destroy(); ftpChart = null; }
}

// x axis shared by both panels. Both run the same `dateAxisTicks` selection, so
// their gridlines land in the same columns; only the bottom panel draws the
// labels, so the pair reads as one plot with one date axis. (autoSkip cannot do
// this - Chart.js gates it on `ticks.display`, so the label-free top panel kept
// every tick while the bottom one thinned to 12.)
const PANEL_X_TICKS = 12;

function panelXScale(labels, showLabels) {
    return {
        afterBuildTicks: dateAxisTicks(PANEL_X_TICKS),
        ticks: {
            display: showLabels,
            maxRotation: 0,
            autoSkip: false,
            callback: monthYearTicks(labels),
        },
    };
}

function panelZoomPlugins() {
    return {
        legend: { display: false }, // custom HTML legend instead
        zoom: {
            zoom: {
                drag: { enabled: true, backgroundColor: tokenAlpha("--accent", 0.15, "#f2a900") },
                mode: "x",
                onZoomComplete: (ctx) => syncPanelX(ctx.chart),
            },
        },
    };
}

function buildMainChart(load, ftpSeries) {
    const est = (ftpSeries && ftpSeries.estimated) || [];
    const recorded = (ftpSeries && ftpSeries.recorded) || [];
    const labels = unionDates(load, est, recorded);

    const loadCanvas = document.getElementById("mainChart");
    const ftpCanvas = document.getElementById("ftpChart");
    const panels = document.getElementById("mainPanels");
    const empty = document.getElementById("mainEmpty");
    if (!loadCanvas || !ftpCanvas) return;

    destroyPanels();

    if (!labels.length) {
        if (panels) panels.style.display = "none";
        if (empty) empty.style.display = "block";
        const legend = document.getElementById("mainLegend");
        if (legend) legend.innerHTML = "";
        return;
    }
    if (panels) panels.style.display = "block";
    if (empty) empty.style.display = "none";

    loadChart = new Chart(loadCanvas, {
        type: "line",
        data: {
            labels,
            datasets: [
                { label: "CTL", data: alignedFrom(labels, load, "ctl"),
                  borderColor: cssVar("--s-1"), spanGaps: true },
                { label: "ATL", data: alignedFrom(labels, load, "atl"),
                  borderColor: cssVar("--s-2"), spanGaps: true },
                { label: "TSB", data: alignedFrom(labels, load, "tsb"),
                  borderColor: cssVar("--s-3"), spanGaps: true },
            ],
        },
        options: {
            responsive: true,
            onResize: scheduleAlign,
            // Declared here so alignPanels only ever writes the leaves.
            layout: { padding: { left: 0, right: 0 } },
            interaction: { mode: "index", intersect: false },
            plugins: panelZoomPlugins(),
            scales: {
                x: panelXScale(labels, false),
                y: {
                    position: "left",
                    title: { display: true, text: "Load" },
                },
            },
        },
        plugins: [ZERO_LINE_PLUGIN],
    });

    const ftpColor = cssVar("--s-4");
    ftpChart = new Chart(ftpCanvas, {
        type: "line",
        data: {
            labels,
            datasets: [
                { label: "Training FTP (est)", data: interpolatedFrom(labels, est, "ftp"),
                  borderColor: ftpColor, spanGaps: true, borderDash: [4, 3] },
                { label: "Training FTP (recorded)",
                  data: alignedFrom(labels, recorded, "ftp_watts"),
                  borderColor: ftpColor, backgroundColor: ftpColor,
                  showLine: false, pointRadius: 4, pointHitRadius: 8, spanGaps: true },
            ],
        },
        options: {
            responsive: true,
            onResize: scheduleAlign,
            // Declared here so alignPanels only ever writes the leaves.
            layout: { padding: { left: 0, right: 0 } },
            interaction: { mode: "index", intersect: false },
            plugins: panelZoomPlugins(),
            scales: {
                x: panelXScale(labels, true),
                y: {
                    position: "left",
                    title: { display: true, text: "FTP (W)" },
                    ticks: { maxTicksLimit: 4 },
                },
            },
        },
    });

    // One hover reads both panels at the same date - the only thing the old
    // dual axis was buying.
    panelCrosshair = linkedCrosshair([loadChart, ftpChart]);
    buildLegend();
    scheduleAlign();
}

async function loadMainChart(months) {
    currentMonths = months || 0;
    const q = currentMonths > 0 ? "?months=" + currentMonths : "";
    const [load, ftpSeries] = await Promise.all([
        fetchJSON("/api/load" + q),
        fetchJSON("/api/ftp_series" + q),
    ]);
    buildMainChart(load || [], ftpSeries || { estimated: [], recorded: [] });
}

function wireControls() {
    document.querySelectorAll(".range-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
            btn.classList.add("active");
            const custom = document.getElementById("customMonths");
            if (custom) custom.value = "";
            loadMainChart(parseInt(btn.dataset.months, 10));
        });
    });
    const custom = document.getElementById("customMonths");
    if (custom) {
        custom.addEventListener("change", () => {
            const m = parseInt(custom.value, 10);
            if (m > 0) {
                document.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
                loadMainChart(m);
            }
        });
    }
    const reset = document.getElementById("resetZoom");
    if (reset) reset.addEventListener("click", resetPanelZoom);
}

function fmtDuration(sec) {
    if (sec < 60) return sec + "s";
    if (sec < 3600) {
        const m = sec / 60;
        return (Number.isInteger(m) ? m : m.toFixed(1)) + "m";
    }
    const h = sec / 3600;
    return (Number.isInteger(h) ? h : h.toFixed(1)) + "h";
}

function wrapText(text, maxLen) {
    const lines = [];
    let line = "";
    for (const word of text.split(" ")) {
        if (line && (line.length + 1 + word.length) > maxLen) {
            lines.push(line);
            line = word;
        } else {
            line = line ? line + " " + word : word;
        }
    }
    if (line) lines.push(line);
    return lines;
}

const CURVE_SERIES_DESC = {
    "Measured MMP": "Your best average power actually recorded for each duration, across all rides in the last 90 days.",
    "CP/W' model": "The fitted curve estimating your sustainable power at any duration (Critical Power plus your anaerobic W' reserve divided by time).",
};

// The rider's Training FTP as a dashed reference on the watts axis: it is the
// number every target in the app is derived from, so "where does my curve sit
// relative to it" is the read this chart is usually opened for.
const FTP_REFERENCE_PLUGIN = {
    id: "ftpReference",
    afterDatasetsDraw(chart) {
        const ftp = Number(dashboardFtp);
        const scale = chart.scales.y;
        const area = chart.chartArea;
        if (!ftp || !scale || !area || ftp < scale.min || ftp > scale.max) return;
        const y = scale.getPixelForValue(ftp);
        const ink = cssVar("--muted", "#8a94a0");
        const ctx = chart.ctx;
        ctx.save();
        ctx.beginPath();
        ctx.lineWidth = 1;
        ctx.strokeStyle = ink;
        ctx.setLineDash([6, 4]);
        ctx.moveTo(area.left, y);
        ctx.lineTo(area.right, y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = ink;
        ctx.font = Chart.defaults.font.size + "px " + Chart.defaults.font.family;
        ctx.textAlign = "right";
        ctx.textBaseline = "bottom";
        ctx.fillText("FTP", area.right - 4, y - 3);
        ctx.restore();
    },
};

async function renderCurveChart() {
    const el = document.getElementById("curveChart");
    if (!el) return;
    const empty = document.getElementById("curveEmpty");
    const data = await fetchJSON("/api/curve");
    const measured = ((data && data.measured) || []).map((p) => ({ x: p.t, y: p.power }));
    const model = ((data && data.model) || []).map((p) => ({ x: p.t, y: p.power }));

    if (curveChart) { curveChart.destroy(); curveChart = null; }
    const legend = document.getElementById("curveLegend");
    if (legend) legend.innerHTML = "";

    if (!measured.length && !model.length) {
        el.style.display = "none";
        if (empty) empty.style.display = "block";
        return;
    }
    el.style.display = "block";
    if (empty) empty.style.display = "none";

    // Axis ticks are exactly the sampled durations, so every tick has its
    // measured dot and vice versa - but ~12 of them collide on a narrow
    // viewport, so they are thinned from the long end (the last duration is
    // always kept; it anchors the axis).
    const tickDurations = (measured.length ? measured : model).map((p) => p.x);

    curveChart = new Chart(el, {
        type: "scatter",
        data: {
            datasets: [
                { label: "Measured MMP", data: measured, borderColor: cssVar("--s-2"),
                  backgroundColor: cssVar("--s-2"), showLine: true, pointRadius: 0,
                  pointHitRadius: 8 },
                { label: "CP/W' model", data: model, borderColor: cssVar("--s-1"),
                  showLine: true, pointRadius: 0, pointHitRadius: 8 },
            ],
        },
        options: {
            responsive: true,
            plugins: {
                legend: { display: false }, // custom HTML legend instead
                tooltip: {
                    callbacks: {
                        title: (items) => (items.length ? fmtDuration(items[0].parsed.x) : ""),
                        label: (item) => item.dataset.label + ": " + item.parsed.y + " W",
                        footer: (items) =>
                            items.length
                                ? wrapText(CURVE_SERIES_DESC[items[0].dataset.label] || "", 44)
                                : "",
                    },
                },
            },
            scales: {
                x: {
                    type: "logarithmic",
                    title: { display: true, text: "Duration" },
                    afterBuildTicks: (axis) => {
                        const width = (axis.chart && axis.chart.width) || 300;
                        const room = Math.max(4, Math.floor(width / 90));
                        const step = Math.max(1, Math.ceil(tickDurations.length / room));
                        const keep = [];
                        for (let i = tickDurations.length - 1; i >= 0; i -= step) keep.push(i);
                        axis.ticks = keep.reverse().map((i) => ({ value: tickDurations[i] }));
                    },
                    ticks: {
                        autoSkip: false,
                        maxRotation: 0,
                        callback: (value) => fmtDuration(value),
                    },
                },
                y: { title: { display: true, text: "Power (W)" } },
            },
        },
        plugins: [FTP_REFERENCE_PLUGIN],
    });

    renderLegend("curveLegend", () => [
        { label: "Measured MMP", chart: curveChart, datasets: [0], token: "--s-2",
          tip: CURVE_SERIES_DESC["Measured MMP"] },
        { label: "CP/W' model", chart: curveChart, datasets: [1], token: "--s-1",
          tip: CURVE_SERIES_DESC["CP/W' model"] },
    ]);
}

function renderDashboard(ftp) {
    dashboardFtp = Number(ftp) || null;
    wireControls();
    loadMainChart(1);
    renderCurveChart();
}

// ---------------------------------------------- activity detail ride graphs
const DETAIL_COLORS = {
    power: "#e05252",
    heartrate: "#f2a900",
    cadence: "#5a9bd4",
    altitude: "#8a94a0",
};

function escapeHTML(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
}

function renderZoneSummary(targetId, summary, unit) {
    const target = document.getElementById(targetId);
    if (!target) return;
    const heading = target.querySelector("h4");
    const title = heading ? heading.outerHTML : "";
    if (!summary || !summary.available) {
        target.innerHTML = title + '<p class="hint">' +
            escapeHTML((summary && summary.reason) || "Zone data unavailable") + ".</p>";
        return;
    }
    const anchorLabel = unit === "W" ? "FTP" : "HRmax";
    const anchor = Math.round(Number(summary.anchor) * 10) / 10;
    const rows = (summary.zones || []).map((zone) => {
        const pct = Math.max(0, Math.min(100, Number(zone.percent) || 0));
        return '<tr><th scope="row">' + escapeHTML(zone.label) + '</th>' +
            '<td class="zone-range">' + escapeHTML(zone.range) + " " + escapeHTML(unit) + '</td>' +
            '<td class="zone-duration">' + escapeHTML(zone.duration) + '</td>' +
            '<td class="zone-percent"><span class="zone-bar" style="--zone-pct:' + pct +
            '%"></span><span>' + pct.toFixed(1) + '%</span></td></tr>';
    }).join("");
    target.innerHTML = title + '<p class="hint">' + anchorLabel + ": " + anchor + " " + unit +
        " · " + escapeHTML(summary.source) + '</p><div class="table-scroll"><table class="zone-time-table">' +
        '<caption class="sr-only">Time spent in each zone</caption><thead><tr>' +
        '<th scope="col">Zone</th><th scope="col">Range</th><th scope="col">Time</th>' +
        '<th scope="col">% valid</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
        '<p class="hint">Stream coverage: ' + Number(summary.coverage_pct || 0).toFixed(1) +
        "% · missing/uncredited: " + fmtDuration(Number(summary.missing_s || 0)) + "</p>";
}

// Type-aware perceived-exertion (RPE) control on the activity detail page.
// A ride matched to a verified plan/standalone workout drives that workout's
// rating (feeding the FTP estimate); an unmatched ride stores a subjective
// rating on the activity itself and does not affect FTP.
function renderActivityRpe(activityId, detail) {
    const section = document.getElementById("rpeSection");
    if (!section) return;
    const wrap = document.getElementById("rpeButtons");
    const hint = document.getElementById("rpeHint");
    const status = document.getElementById("rpeStatus");
    section.hidden = false;
    wrap.innerHTML = "";

    const linked = detail.linked_workout;
    const matched = !!(linked && linked.rpe_eligible);
    let endpoint;
    if (matched) {
        endpoint = linked.kind === "plan"
            ? "/api/plan/workout/" + linked.id + "/rpe"
            : "/api/standalone-workout/" + linked.id + "/rpe";
        hint.textContent = "Matched to your planned “" + linked.name +
            "” — this rating tunes your FTP estimate (10 = too hard).";
    } else {
        endpoint = "/api/activity/" + activityId + "/rpe";
        hint.textContent = "Subjective effort rating for this ride.";
    }
    let current = matched ? (linked.rpe || null) : (detail.rpe || null);

    function paint() {
        Array.prototype.forEach.call(wrap.children, function (b) {
            const on = String(current) === b.dataset.rpe;
            b.classList.toggle("rpe-on", on);
            b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        status.textContent = current
            ? (current === 10
                ? "Rated 10/10 — too hard."
                : "Rated " + current + "/10.")
            : "Not rated yet.";
    }

    async function grade(val) {
        status.textContent = "Saving…";
        try {
            const resp = await fetch(endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ rpe: val }),
            });
            if (!resp.ok) { status.textContent = "Could not save rating."; return; }
            const saved = await resp.json();
            current = saved.rpe;
            paint();
        } catch (e) {
            status.textContent = "Could not save rating: " + e.message;
        }
    }

    for (let i = 1; i <= 10; i++) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "rpe-btn";
        btn.dataset.rpe = String(i);
        btn.textContent = String(i);
        (function (v) {
            btn.addEventListener("click", function () { grade(v); });
        })(i);
        wrap.appendChild(btn);
    }
    paint();
}

async function renderActivityDetail(activityId) {
    const canvas = document.getElementById("detailChart");
    if (!canvas) return;
    const empty = document.getElementById("detailEmpty");
    const data = await fetchJSON("/api/activity/" + activityId);
    if (!data) { if (empty) { empty.style.display = "block"; } return; }

    renderActivityRpe(activityId, data);
    renderZoneSummary("powerZoneSummary", data.zones && data.zones.power, "W");
    renderZoneSummary("hrZoneSummary", data.zones && data.zones.heart_rate, "bpm");

    const t = data.t || [];
    const have = data.have || {};
    const anyLine = have.power || have.heartrate || have.cadence;
    if (!t.length || (!anyLine && !have.altitude)) {
        canvas.style.display = "none";
        if (empty) empty.style.display = "block";
        return;
    }

    const datasets = [];
    // Elevation first + high order so it renders as a background band.
    if (have.altitude) {
        datasets.push({
            label: "Elevation (m)", data: data.altitude,
            borderColor: "rgba(138,148,160,0.5)", backgroundColor: "rgba(138,148,160,0.18)",
            yAxisID: "yAlt", fill: "start", pointRadius: 0, borderWidth: 1,
            tension: 0.3, order: 10, spanGaps: true,
        });
    }
    if (have.power) {
        datasets.push({
            label: "Power (W)", data: data.power, borderColor: DETAIL_COLORS.power,
            yAxisID: "yPower", pointRadius: 0, borderWidth: 1.5, tension: 0.2,
            spanGaps: true, order: 1,
        });
    }
    if (have.heartrate) {
        datasets.push({
            label: "Heart rate (bpm)", data: data.heartrate, borderColor: DETAIL_COLORS.heartrate,
            yAxisID: "yBio", pointRadius: 0, borderWidth: 1.5, tension: 0.2,
            spanGaps: true, order: 2,
        });
    }
    if (have.cadence) {
        datasets.push({
            label: "Cadence (rpm)", data: data.cadence, borderColor: DETAIL_COLORS.cadence,
            yAxisID: "yBio", pointRadius: 0, borderWidth: 1.5, tension: 0.2,
            spanGaps: true, order: 3,
        });
    }

    const scales = {
        x: {
            type: "linear", title: { display: true, text: "Time (min)" },
            ticks: { maxTicksLimit: 12, maxRotation: 0 },
        },
        yPower: {
            position: "left", title: { display: true, text: "Power (W)" },
            beginAtZero: true,
        },
        yBio: {
            position: "right", title: { display: true, text: "HR / cadence" },
            beginAtZero: true, grid: { drawOnChartArea: false },
        },
        yAlt: {
            position: "right", display: false, grid: { drawOnChartArea: false },
        },
    };
    if (!have.power) delete scales.yPower;
    if (!(have.heartrate || have.cadence)) delete scales.yBio;
    if (!have.altitude) delete scales.yAlt;

    // Real data series (power/HR/cadence) are the isolate/restore units; the
    // elevation background band is excluded so legend clicks never hide or
    // expose it. Each real series is its own single-dataset unit.
    const elevationIndex = datasets.findIndex((d) => d.yAxisID === "yAlt");
    const realUnits = datasets
        .map((_d, i) => i)
        .filter((i) => i !== elevationIndex)
        .map((i) => [i]);

    const labelled = t.map((x) => Math.round(x * 10) / 10);
    new Chart(canvas, {
        type: "line",
        data: { labels: labelled, datasets },
        options: {
            responsive: true,
            animation: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: {
                    position: "top",
                    // Match the dashboard legend: plain click isolates (and a
                    // second click on the isolated series restores all);
                    // Ctrl/Cmd-click toggles just that series. Elevation stays
                    // an independent on/off, never part of the isolation.
                    onClick: (e, legendItem, legend) => {
                        const chart = legend.chart;
                        const idx = legendItem.datasetIndex;
                        const additive = !!(e.native && (e.native.ctrlKey || e.native.metaKey));
                        if (idx === elevationIndex) {
                            chart.setDatasetVisibility(idx, !chart.isDatasetVisible(idx));
                            chart.update();
                            return;
                        }
                        const target = realUnits.find((u) => u.indexOf(idx) !== -1);
                        if (target) applyLegendSelection(chart, realUnits, target, additive);
                    },
                },
                tooltip: {
                    callbacks: {
                        title: (items) => (items.length ? items[0].label + " min" : ""),
                    },
                },
            },
            scales,
        },
    });
}
