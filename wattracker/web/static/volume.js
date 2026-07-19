// Weekly training-volume bar charts. Data comes from /api/volume.

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

function makeBarChart(canvasId, label, weeks, key) {
    const el = document.getElementById(canvasId);
    if (!el) return;
    const color = VOLUME_COLORS[key] || "#999";
    new Chart(el, {
        type: "bar",
        data: {
            labels: weeks.map((w) => w.week_start),
            datasets: [{
                label: label,
                data: weeks.map((w) => w[key]),
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
                        label: (item) => label + ": " + item.parsed.y,
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
                    title: { display: true, text: label },
                    ticks: { color: "#c2cad4" },
                    grid: { color: "rgba(138,148,160,0.10)" },
                },
            },
        },
    });
}

// Compact last-4-weeks vs previous-4-weeks summary. Totals per metric with a
// percentage change; operates on the dense (gap-filled) series so absent weeks
// count as zero.
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

function renderSummary(dense) {
    const el = document.getElementById("volumeSummary");
    if (!el) return;
    const last4 = dense.slice(-4);
    const prev4 = dense.slice(-8, -4);
    if (last4.length < 1) return;
    el.style.display = "";
    el.innerHTML = "";
    SUMMARY_METRICS.forEach((m) => {
        const cur = sumOver(last4, m.key);
        const prev = sumOver(prev4, m.key);
        const pct = pctChange(cur, prev);
        let deltaHtml = '<span class="label">last 4 wks vs prev 4</span>';
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

async function renderVolume() {
    const data = await fetchVolume();
    const weeks = fillWeeks(data.weeks || []);
    if (!weeks.length) {
        const empty = document.getElementById("volumeEmpty");
        if (empty) empty.style.display = "block";
        return;
    }
    renderSummary(weeks);
    makeBarChart("hoursChart", "Hours", weeks, "hours");
    makeBarChart("tssChart", "TSS", weeks, "tss");
    makeBarChart("distanceChart", "Distance (km)", weeks, "distance_km");
    makeBarChart("caloriesChart", "Calories", weeks, "calories");
}
