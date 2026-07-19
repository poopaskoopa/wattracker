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

const COLORS = {
    CTL: "#4caf7d",
    ATL: "#f2a900",
    TSB: "#5a9bd4",
    FTP: "#e05252",
    "Training FTP": "#e05252",
};

let mainChart = null;
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

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Compact date ticks: plain month names, with the year only on the first tick
// and at year boundaries ("Jan 2026"); further ticks within a month are hidden.
function monthYearTicks(labels) {
    return function (value, index, ticks) {
        const label = labels[value] || "";
        const y = label.slice(0, 4);
        const m = parseInt(label.slice(5, 7), 10) - 1;
        if (!(m >= 0 && m <= 11)) return label;
        const prev = index > 0 && ticks[index - 1]
            ? (labels[ticks[index - 1].value] || "") : "";
        if (!prev || prev.slice(0, 4) !== y) return MONTH_NAMES[m] + " " + y;
        if (prev.slice(5, 7) === label.slice(5, 7)) return "";
        return MONTH_NAMES[m];
    };
}

function unionDates(...arrays) {
    const set = new Set();
    arrays.forEach((arr) => (arr || []).forEach((p) => set.add(p.date)));
    return Array.from(set).sort();
}

// Legend groups map a label to one or more dataset indices.
const LEGEND_GROUPS = [
    { label: "CTL", datasets: [0] },
    { label: "ATL", datasets: [1] },
    { label: "TSB", datasets: [2] },
    { label: "Training FTP", datasets: [3, 4] }, // estimated line + recorded points
];

// Most recent value across a legend group's datasets (a later dataset in the
// group wins a tie on the same date, e.g. recorded FTP over the estimate).
function latestGroupValue(group) {
    if (!mainChart) return null;
    const datasets = group.datasets.map((i) => mainChart.data.datasets[i]);
    const n = (mainChart.data.labels || []).length;
    for (let i = n - 1; i >= 0; i--) {
        for (let d = datasets.length - 1; d >= 0; d--) {
            const v = ((datasets[d] || {}).data || [])[i];
            if (v != null) return v;
        }
    }
    return null;
}

// Shared legend behaviour for any chart. `units` is the set of selectable
// legend entries, each a list of dataset indices (one index for a simple
// series, several for a group like FTP). `target` must be the same array
// reference as one of `units`.
//   - additive (Ctrl/Cmd-click): toggle just this unit on/off, leave others.
//   - plain click: isolate this unit; clicking the already-isolated unit
//     (the only visible one) restores ALL units.
// Only the datasets listed in `units` are touched, so a background dataset
// (e.g. the ride chart's elevation band) omitted from `units` is never
// hidden or exposed by isolation.
function applyLegendSelection(chart, units, target, additive) {
    const visibleOf = (u) => u.some((i) => chart.isDatasetVisible(i));
    if (additive) {
        const nowVisible = visibleOf(target);
        target.forEach((i) => chart.setDatasetVisibility(i, !nowVisible));
    } else {
        const isIsolated = units.every((u) => (u === target ? visibleOf(u) : !visibleOf(u)));
        units.forEach((u) => {
            const on = isIsolated ? true : u === target;
            u.forEach((i) => chart.setDatasetVisibility(i, on));
        });
    }
    chart.update();
}

function buildLegend() {
    const container = document.getElementById("mainLegend");
    if (!container || !mainChart) return;
    container.innerHTML = "";
    const units = LEGEND_GROUPS.map((g) => g.datasets);
    LEGEND_GROUPS.forEach((group) => {
        const visible = group.datasets.some((i) => mainChart.isDatasetVisible(i));
        const item = document.createElement("span");
        item.className = "legend-item" + (visible ? "" : " off");
        item.title = SERIES_TIPS[group.label] || group.label;
        const latest = latestGroupValue(group);
        const valueHtml = latest == null
            ? ""
            : ' <span class="legend-value">' + (Math.round(latest * 10) / 10) + "</span>";
        item.innerHTML =
            '<span class="legend-swatch" style="background:' +
            (COLORS[group.label] || "#999") + '"></span>' + group.label + valueHtml;
        item.addEventListener("click", (e) => {
            applyLegendSelection(mainChart, units, group.datasets, e.ctrlKey || e.metaKey);
            buildLegend();
        });
        container.appendChild(item);
    });
}

function buildMainChart(load, ftpSeries) {
    const est = (ftpSeries && ftpSeries.estimated) || [];
    const recorded = (ftpSeries && ftpSeries.recorded) || [];
    const labels = unionDates(load, est, recorded);

    const canvas = document.getElementById("mainChart");
    const empty = document.getElementById("mainEmpty");
    if (!canvas) return;

    if (mainChart) { mainChart.destroy(); mainChart = null; }

    if (!labels.length) {
        canvas.style.display = "none";
        if (empty) empty.style.display = "block";
        const legend = document.getElementById("mainLegend");
        if (legend) legend.innerHTML = "";
        return;
    }
    canvas.style.display = "block";
    if (empty) empty.style.display = "none";

    const datasets = [
        { label: "CTL", data: alignedFrom(labels, load, "ctl"), borderColor: COLORS.CTL,
          yAxisID: "y", tension: 0.2, pointRadius: 0, spanGaps: true },
        { label: "ATL", data: alignedFrom(labels, load, "atl"), borderColor: COLORS.ATL,
          yAxisID: "y", tension: 0.2, pointRadius: 0, spanGaps: true },
        { label: "TSB", data: alignedFrom(labels, load, "tsb"), borderColor: COLORS.TSB,
          yAxisID: "y", tension: 0.2, pointRadius: 0, spanGaps: true },
        { label: "Training FTP (est)", data: interpolatedFrom(labels, est, "ftp"), borderColor: COLORS.FTP,
          yAxisID: "yFtp", tension: 0.1, pointRadius: 0, spanGaps: true, borderDash: [4, 3] },
        { label: "Training FTP (recorded)", data: alignedFrom(labels, recorded, "ftp_watts"),
          yAxisID: "yFtp", borderColor: COLORS.FTP, backgroundColor: COLORS.FTP,
          showLine: false, pointRadius: 4, spanGaps: true },
    ];

    mainChart = new Chart(canvas, {
        type: "line",
        data: { labels, datasets },
        options: {
            responsive: true,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: false }, // custom HTML legend instead
                zoom: {
                    zoom: {
                        drag: { enabled: true, backgroundColor: "rgba(242,169,0,0.15)" },
                        mode: "x",
                    },
                },
            },
            scales: {
                x: {
                    ticks: {
                        color: "#c2cad4",
                        maxTicksLimit: 12,
                        maxRotation: 0,
                        autoSkip: true,
                        callback: monthYearTicks(labels),
                    },
                    grid: {
                        color: "rgba(138,148,160,0.10)",   // faint plot gridlines
                        tickColor: "rgba(194,202,212,0.8)", // clearly visible tick marks
                        tickLength: 8,
                    },
                    border: { color: "rgba(138,148,160,0.6)" },
                },
                y: { position: "left", title: { display: true, text: "Load (CTL/ATL/TSB)" } },
                yFtp: {
                    position: "right", title: { display: true, text: "Training FTP (W)" },
                    grid: { drawOnChartArea: false },
                },
            },
        },
    });
    buildLegend();
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
    if (reset) {
        reset.addEventListener("click", () => {
            if (mainChart && mainChart.resetZoom) mainChart.resetZoom();
        });
    }
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

async function renderCurveChart() {
    const el = document.getElementById("curveChart");
    if (!el) return;
    const data = await fetchJSON("/api/curve");
    if (!data) return;
    const measured = (data.measured || []).map((p) => ({ x: p.t, y: p.power }));
    const model = (data.model || []).map((p) => ({ x: p.t, y: p.power }));
    if (!measured.length && !model.length) return;
    // Axis ticks are exactly the sampled durations, so every tick has its
    // measured (yellow) dot and vice versa.
    const tickDurations = (measured.length ? measured : model).map((p) => p.x);
    const SERIES_DESC = {
        "Measured MMP": "Your best average power actually recorded for each duration, across all rides in the last 90 days.",
        "CP/W' model": "The fitted curve estimating your sustainable power at any duration (Critical Power plus your anaerobic W' reserve divided by time).",
    };
    new Chart(el, {
        type: "scatter",
        data: {
            datasets: [
                { label: "Measured MMP", data: measured, borderColor: "#f2a900",
                  backgroundColor: "#f2a900", showLine: true, pointRadius: 0,
                  pointHitRadius: 8, tension: 0.1 },
                { label: "CP/W' model", data: model, borderColor: "#4caf7d",
                  showLine: true, pointRadius: 0, tension: 0.1 },
            ],
        },
        options: {
            responsive: true,
            plugins: {
                tooltip: {
                    callbacks: {
                        title: (items) => (items.length ? fmtDuration(items[0].parsed.x) : ""),
                        label: (item) => item.dataset.label + ": " + item.parsed.y + " W",
                        footer: (items) =>
                            items.length
                                ? wrapText(SERIES_DESC[items[0].dataset.label] || "", 44)
                                : "",
                    },
                },
            },
            scales: {
                x: {
                    type: "logarithmic",
                    title: { display: true, text: "Duration" },
                    afterBuildTicks: (axis) => {
                        axis.ticks = tickDurations.map((t) => ({ value: t }));
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
    });
}

function renderDashboard() {
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

async function renderActivityDetail(activityId) {
    const canvas = document.getElementById("detailChart");
    if (!canvas) return;
    const empty = document.getElementById("detailEmpty");
    const data = await fetchJSON("/api/activity/" + activityId);
    if (!data) { if (empty) { empty.style.display = "block"; } return; }

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
