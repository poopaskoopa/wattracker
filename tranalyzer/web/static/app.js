// Chart.js dashboard rendering. Data is pulled from the JSON API endpoints.

async function fetchJSON(url) {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    return await resp.json();
}

async function renderLoadChart() {
    const el = document.getElementById("loadChart");
    if (!el) return;
    const data = await fetchJSON("/api/load");
    if (!data || !data.length) return;
    const labels = data.map((d) => d.date);
    new Chart(el, {
        type: "line",
        data: {
            labels,
            datasets: [
                { label: "CTL (Fitness)", data: data.map((d) => d.ctl),
                  borderColor: "#4caf7d", tension: 0.2, pointRadius: 0 },
                { label: "ATL (Fatigue)", data: data.map((d) => d.atl),
                  borderColor: "#f2a900", tension: 0.2, pointRadius: 0 },
                { label: "TSB (Form)", data: data.map((d) => d.tsb),
                  borderColor: "#5a9bd4", tension: 0.2, pointRadius: 0 },
            ],
        },
        options: { responsive: true, scales: { x: { ticks: { maxTicksLimit: 12 } } } },
    });
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

async function renderFtpChart() {
    const el = document.getElementById("ftpChart");
    if (!el) return;
    const data = await fetchJSON("/api/ftp");
    if (!data || !data.length) return;
    new Chart(el, {
        type: "line",
        data: {
            labels: data.map((d) => d.date),
            datasets: [
                { label: "FTP (W)", data: data.map((d) => d.ftp_watts),
                  borderColor: "#f2a900", tension: 0.1, pointRadius: 4 },
            ],
        },
        options: { responsive: true },
    });
}

function renderDashboard() {
    renderLoadChart();
    renderCurveChart();
    renderFtpChart();
}
