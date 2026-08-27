// Body-weight chart on the profile page. The server renders the log table;
// the chart adds the shape of the same data. Weigh-ins are discrete
// observations, so points are drawn (the theme's dense-series radius 0 is
// for per-second streams) and no spline is fitted - a tension > 0 would
// invent weights the rider never had.
(function () {
    "use strict";

    var dataElement = document.getElementById("bodyWeightData");
    var canvas = document.getElementById("bodyWeightChart");
    if (!dataElement || !canvas || typeof Chart === "undefined") return;

    var entries;
    try {
        entries = JSON.parse(dataElement.textContent);
    } catch (error) {
        return;
    }
    if (!Array.isArray(entries) || !entries.length) return;

    var labels = entries.map(function (entry) { return entry.date; });
    var values = entries.map(function (entry) { return Number(entry.weight_kg); });

    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    // ~2 kg of air above and below the data; a flat week still gets a
    // readable range rather than a hairline at the axis edge.
    var pad = Math.max(2, (max - min) * 0.15);

    new Chart(canvas, {
        type: "line",
        data: {
            labels: labels,
            datasets: [{
                label: "Weight (kg)",
                data: values,
                borderColor: cssVar("--s-1"),
                pointBackgroundColor: cssVar("--s-1"),
                pointRadius: entries.length > 60 ? 1 : 3,
                pointHoverRadius: 5
            }]
        },
        options: {
            responsive: true,
            animation: false,
            layout: { padding: { top: 8 } },
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: function (items) {
                            return items.length ? items[0].label : "";
                        },
                        label: function (context) {
                            var source = entries[context.dataIndex].source;
                            return context.parsed.y + " kg — " + source;
                        }
                    }
                }
            },
            scales: {
                x: {
                    afterBuildTicks: dateAxisTicks(12),
                    ticks: {
                        callback: monthYearTicks(labels),
                        maxRotation: 0,
                        autoSkip: false
                    }
                },
                y: {
                    min: min - pad,
                    max: max + pad,
                    title: { display: true, text: "kg" }
                }
            }
        }
    });
}());
