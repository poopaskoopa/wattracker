(function () {
    "use strict";

    var dataElement = document.getElementById("powerProfileData");
    var canvas = document.getElementById("powerProfileChart");
    if (!dataElement || !canvas || typeof Chart === "undefined") return;

    var profileChart;
    try {
        profileChart = JSON.parse(dataElement.textContent);
    } catch (error) {
        return;
    }

    function values(array) {
        return Array.isArray(array) ? array : [];
    }

    function chartValues(array) {
        return values(array).map(function (value) {
            return value === null || value === undefined ? 0 : value;
        });
    }

    function dataset(label, percentages, watts, wkg, color) {
        return {
            label: label,
            data: chartValues(percentages),
            percentages: values(percentages),
            actualWatts: values(watts),
            actualWkg: values(wkg),
            backgroundColor: color,
            borderColor: color,
            borderWidth: 2,
            barPercentage: 0.78,
            categoryPercentage: 0.76
        };
    }

    var directPercentageLabels = {
        id: "powerProfilePercentageLabels",
        afterDatasetsDraw: function (chart) {
            var context = chart.ctx;
            context.save();
            context.fillStyle = cssVar("--text");
            context.strokeStyle = cssVar("--panel");
            context.lineWidth = 3;
            context.lineJoin = "round";
            context.font = "600 12px " + Chart.defaults.font.family;
            context.textAlign = "left";
            context.textBaseline = "middle";

            chart.data.datasets.forEach(function (series, datasetIndex) {
                if (!chart.isDatasetVisible(datasetIndex)) return;
                var meta = chart.getDatasetMeta(datasetIndex);
                meta.data.forEach(function (bar, index) {
                    var percentage = series.percentages[index];
                    if (percentage === null || percentage === undefined) return;
                    var label = percentage + "%";
                    var labelWidth = context.measureText(label).width;
                    var left = chart.chartArea.left + 2;
                    var right = chart.chartArea.right - labelWidth - 2;
                    var labelX = Math.max(left, Math.min(bar.x + 5, right));
                    context.strokeText(label, labelX, bar.y);
                    context.fillText(label, labelX, bar.y);
                });
            });
            context.restore();
        }
    };

    var datasets = [
        dataset(
            "All time",
            profileChart.all_time,
            profileChart.all_time_watts,
            profileChart.all_time_wkg,
            cssVar("--s-2")
        ),
        dataset(
            "Last 60 days",
            profileChart.recent_60d,
            profileChart.recent_60d_watts,
            profileChart.recent_60d_wkg,
            cssVar("--s-3")
        )
    ];

    var chart = new Chart(canvas, {
        type: "bar",
        data: {
            labels: values(profileChart.labels),
            datasets: datasets
        },
        plugins: [directPercentageLabels],
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            layout: {
                padding: {top: 36}
            },
            interaction: {
                mode: "index",
                axis: "y",
                intersect: false
            },
            scales: {
                x: {
                    beginAtZero: true,
                    max: 120,
                    ticks: {
                        callback: function (value) {
                            return value <= 100 ? value + "%" : "";
                        }
                    }
                },
                y: {
                    grid: {display: false}
                }
            },
            plugins: {
                legend: {display: false},
                tooltip: {
                    callbacks: {
                        label: function (context) {
                            var percentage = context.dataset.percentages[context.dataIndex];
                            var watts = context.dataset.actualWatts[context.dataIndex];
                            if (percentage === null || percentage === undefined ||
                                    watts === null || watts === undefined) {
                                return context.dataset.label + ": unavailable";
                            }
                            var wkg = context.dataset.actualWkg[context.dataIndex];
                            return context.dataset.label + ": " + watts + " W" +
                                (wkg === null || wkg === undefined
                                    ? " (W/kg unavailable)" : " (" + wkg + " W/kg)") +
                                " — " + percentage + "%";
                        }
                    }
                }
            }
        }
    });

    function buildLegend() {
        var container = document.getElementById("powerProfileLegend");
        if (!container) return;
        container.replaceChildren();

        chart.data.datasets.forEach(function (series, index) {
            var item = document.createElement("span");
            var visible = chart.isDatasetVisible(index);
            item.className = "legend-item" + (visible ? "" : " off");
            item.setAttribute("role", "button");
            item.setAttribute("tabindex", "0");
            item.setAttribute("aria-pressed", visible ? "true" : "false");
            item.setAttribute("aria-label", "Toggle " + series.label + " series");

            var swatch = document.createElement("span");
            swatch.className = "legend-swatch";
            swatch.style.backgroundColor = series.backgroundColor;
            swatch.setAttribute("aria-hidden", "true");
            item.appendChild(swatch);
            item.appendChild(document.createTextNode(series.label));

            function toggle() {
                chart.setDatasetVisibility(index, !chart.isDatasetVisible(index));
                chart.update();
                buildLegend();
            }

            item.addEventListener("click", toggle);
            item.addEventListener("keydown", function (event) {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                toggle();
            });
            container.appendChild(item);
        });
    }

    buildLegend();
}());
