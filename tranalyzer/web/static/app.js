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
        { label: "FTP (est)", data: alignedFrom(labels, est, "ftp"), borderColor: COLORS.FTP,
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
                x: { ticks: { maxTicksLimit: 12 } },
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

async function renderCurveChart() {
    const el = document.getElementById("curveChart");
    if (!el) return;
    const data = await fetchJSON("/api/curve");
    if (!data) return;
    const measured = (data.measured || []).map((p) => ({ x: p.t, y: p.power }));
    const model = (data.model || []).map((p) => ({ x: p.t, y: p.power }));
    if (!measured.length && !model.length) return;
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
            scales: {
                x: { type: "logarithmic", title: { display: true, text: "Duration (s)" } },
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
