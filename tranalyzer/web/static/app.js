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
    FTP: "Estimated FTP — best 20-min power x 0.95 over a rolling 42-day window (plus recorded points).",
};

const COLORS = {
    CTL: "#4caf7d",
    ATL: "#f2a900",
    TSB: "#5a9bd4",
    FTP: "#e05252",
};

let mainChart = null;
let currentMonths = 0; // 0 = all

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
    { label: "FTP", datasets: [3, 4] }, // rolling line + recorded points
];

function buildLegend() {
    const container = document.getElementById("mainLegend");
    if (!container || !mainChart) return;
    container.innerHTML = "";
    LEGEND_GROUPS.forEach((group) => {
        const visible = group.datasets.some((i) => mainChart.isDatasetVisible(i));
        const item = document.createElement("span");
        item.className = "legend-item" + (visible ? "" : " off");
        item.title = SERIES_TIPS[group.label] || group.label;
        item.innerHTML =
            '<span class="legend-swatch" style="background:' +
            (COLORS[group.label] || "#999") + '"></span>' + group.label;
        item.addEventListener("click", (e) => {
            if (e.ctrlKey || e.metaKey) {
                // Ctrl/Cmd-click = toggle just this group; leave others as-is.
                const nowVisible = group.datasets.some((i) => mainChart.isDatasetVisible(i));
                group.datasets.forEach((i) => mainChart.setDatasetVisibility(i, !nowVisible));
            } else {
                // Plain click = isolate this group (others hidden).
                LEGEND_GROUPS.forEach((g) => {
                    const on = g === group;
                    g.datasets.forEach((i) => mainChart.setDatasetVisibility(i, on));
                });
            }
            mainChart.update();
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
        { label: "FTP (est)", data: interpolatedFrom(labels, est, "ftp"), borderColor: COLORS.FTP,
          yAxisID: "yFtp", tension: 0.1, pointRadius: 0, spanGaps: true, borderDash: [4, 3] },
        { label: "FTP (recorded)", data: alignedFrom(labels, recorded, "ftp_watts"),
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
                        maxTicksLimit: 12,
                        maxRotation: 0,
                        autoSkip: true,
                        callback: monthYearTicks(labels),
                    },
                },
                y: { position: "left", title: { display: true, text: "Load (CTL/ATL/TSB)" } },
                yFtp: {
                    position: "right", title: { display: true, text: "FTP (W)" },
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
    new Chart(el, {
        type: "scatter",
        data: {
            datasets: [
                { label: "Measured MMP", data: measured, backgroundColor: "#f2a900",
                  showLine: false, pointRadius: 5 },
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
    loadMainChart(0);
    renderCurveChart();
}
