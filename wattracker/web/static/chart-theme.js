// Chart.js theme. Loaded once from base.html, after chart.js and the zoom
// plugin and before any page script, so every chart on every page inherits the
// same chrome instead of re-specifying it by hand (four files did, with three
// different sets of literal colours between them).
//
// Everything here reads the CSS custom properties in style.css - the tokens are
// the single source of truth for colour, and JS must never hold a literal hex.
// Page scripts should therefore only carry what is genuinely per-chart: titles,
// ranges, tick callbacks, tooltip text.

// Read a design token off :root. Returns the trimmed value, or `fallback` if
// the property is unset (a stylesheet that failed to load, or an old cached
// style.css served to a new script). Cheap enough to call at setup time; do not
// call it per-frame - getComputedStyle forces style resolution.
function cssVar(name, fallback) {
    try {
        var v = getComputedStyle(document.documentElement).getPropertyValue(name);
        v = (v || "").trim();
        return v || fallback || "";
    } catch (e) {
        return fallback || "";
    }
}

// A token colour with an alpha channel, for the places Chart.js wants a
// translucent fill (zoom drag boxes, area fills under a line, the elevation
// band). The tokens are opaque #rrggbb, so the alpha has to be added here
// rather than stored as a second token per opacity.
function tokenAlpha(name, alpha, fallback) {
    var hex = cssVar(name, fallback || "#ffffff");
    var m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex; // already rgba(), or a named colour: hand it back as-is
    var n = parseInt(m[1], 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," +
        (n & 255) + "," + alpha + ")";
}

(function () {
    "use strict";

    window.tokenAlpha = tokenAlpha;

    // No module system in this codebase (app.js / volume.js are plain scripts),
    // so the two helpers are globals, like `renderDashboard` and friends.
    // `cssVar` is already global by being a top-level declaration; assigning it
    // here too makes the contract explicit and survives any later wrapping.
    window.cssVar = cssVar;

    if (typeof Chart === "undefined") {
        // chart.js absent: nothing to theme, but callers must still find the
        // helper rather than a ReferenceError, so export an inert handle with
        // the same shape linkedCrosshair returns.
        window.linkedCrosshair = function () { return { destroy: function () {} }; };
        return;
    }

    var d = Chart.defaults;

    var TEXT = cssVar("--text", "#e6e6e6");
    var TEXT_BRIGHT = cssVar("--text-bright", "#f5f7fa");
    var MUTED = cssVar("--muted", "#8a94a0");
    var PANEL = cssVar("--panel", "#1a2028");
    var BORDER = cssVar("--surface-border", "#2a333d");
    var GRID = cssVar("--grid", "rgba(255,255,255,.07)");
    var AXIS = cssVar("--axis", "rgba(255,255,255,.20)");
    // Tooltip corner: same step as the small chrome radius so the tooltip reads
    // as part of the UI, not as a Chart.js artifact. parseInt drops the "px".
    var R_SM = parseInt(cssVar("--r-sm", "4px"), 10) || 4;

    // ---------------------------------------------------------------- type
    // Match the body stack exactly: a chart label in the browser's default
    // Helvetica next to page text in -apple-system is the loudest "this widget
    // is not part of the page" signal there is. 12px is one step under body
    // text - chart labels are reference, not content.
    d.font.family = "-apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif";
    d.font.size = 12;

    // Chart.js ships `color: #666`, chosen for a white page: on --panel it is
    // barely legible. --muted is the default ink for everything Chart.js draws
    // that we do not override below (ticks, radial point labels, legend text
    // before the legend block re-raises it).
    d.color = MUTED;

    // ------------------------------------------------------------- scales
    // Stock grid is rgba(0,0,0,0.1) - invisible on a dark panel, which is why
    // three of the four existing charts had no readable y grid at all. The axis
    // line is deliberately ~3x stronger than the grid: one line frames the
    // plot, the rest sit behind the data.
    function applyScaleDefaults(scale) {
        if (!scale) return;
        scale.grid = Object.assign({}, scale.grid, { color: GRID });
        scale.border = Object.assign({}, scale.border, { color: AXIS });
        scale.ticks = Object.assign({}, scale.ticks, { color: MUTED });
        scale.title = Object.assign({}, scale.title, { color: TEXT });
    }
    // `defaults.scale` is the root every scale type falls back to, but each
    // registered type (linear, logarithmic, category, time, radialLinear, ...)
    // carries its own defaults that shadow it for these keys. Write both so the
    // theme holds whichever scale a chart asks for.
    applyScaleDefaults(d.scale);
    Object.keys(d.scales || {}).forEach(function (type) {
        applyScaleDefaults(d.scales[type]);
    });

    // ----------------------------------------------------------- geometry
    // Stock line width is 3, which at these chart heights (120-200px) is a
    // blobby ribbon that hides its own detail; 2 still reads at a glance from
    // the bike. tension 0 because a spline literally invents values between
    // samples - on daily CTL/ATL that is a fabricated curve.
    d.elements.line.borderWidth = 2;
    d.elements.line.tension = 0;
    // Dense time series: a dot per sample is noise. Radius 0 draws the line
    // only; hoverRadius 4 brings the dot back under the cursor so the hovered
    // sample is still identifiable. A chart that genuinely plots discrete
    // observations (e.g. recorded FTP points) sets pointRadius itself.
    d.elements.point.radius = 0;
    d.elements.point.hoverRadius = 4;
    // Bars get a soft top only: rounding the seated end would lift the bar off
    // its baseline and break the length encoding.
    d.elements.bar.borderRadius = 4;
    d.elements.bar.borderSkipped = "bottom";

    // ------------------------------------------------------------ tooltip
    // The stock tooltip is a white box with black text and 40px colour
    // rectangles - a light-theme widget dropped on a dark page. Rebuild it as a
    // panel: same fill, same hairline and same radius as every other surface.
    d.plugins.tooltip.backgroundColor = PANEL;
    d.plugins.tooltip.borderColor = BORDER;
    d.plugins.tooltip.borderWidth = 1;
    d.plugins.tooltip.titleColor = TEXT_BRIGHT;
    d.plugins.tooltip.bodyColor = TEXT;
    d.plugins.tooltip.footerColor = MUTED;
    d.plugins.tooltip.padding = 10;
    d.plugins.tooltip.cornerRadius = R_SM;
    // Keep the colour key - with several series in one index-mode tooltip you
    // cannot tell the rows apart without it - but as an 8px dot matching the
    // series, not the stock 40x40 slab that dominates the row it labels.
    d.plugins.tooltip.displayColors = true;
    d.plugins.tooltip.usePointStyle = true;
    d.plugins.tooltip.boxWidth = 8;
    d.plugins.tooltip.boxHeight = 8;

    // ------------------------------------------------------------- legend
    // Deliberately NOT `display: false`. Charts that have not yet been migrated
    // to the HTML `.chart-legend` still rely on the built-in legend, and turning
    // it off globally would silently strip their only series key. Style it
    // instead: same 8px dot as the tooltip, and legend text back up to --text
    // (it inherits d.color = --muted otherwise, which is too quiet for a
    // control you are meant to click).
    d.plugins.legend.labels.usePointStyle = true;
    d.plugins.legend.labels.boxWidth = 8;
    d.plugins.legend.labels.boxHeight = 8;
    d.plugins.legend.labels.color = TEXT;

    // Titles that are drawn by Chart.js (not by our HTML) use full-strength ink.
    d.plugins.title.color = TEXT;

    // --------------------------------------------------- linked crosshair
    // Splitting a dual-axis chart into two stacked panels (see docs/ui-refresh
    // 1.2) removes the lie of two y scales, but it also removes the one thing
    // the dual axis bought: reading every series at a single x. The crosshair
    // gives that back - hover either panel and both panels show the same index,
    // with a vertical rule marking it.
    //
    // The rule is drawn by a globally registered plugin that only does anything
    // when a chart carries `$crosshairX`, so it is inert on every other chart.
    var CROSSHAIR_PLUGIN = {
        id: "linkedCrosshair",
        afterDatasetsDraw: function (chart) {
            var x = chart.$crosshairX;
            var area = chart.chartArea;
            if (x == null || !area) return;
            if (x < area.left || x > area.right) return;
            var ctx = chart.ctx;
            ctx.save();
            ctx.beginPath();
            ctx.lineWidth = 1;
            ctx.strokeStyle = AXIS;
            ctx.moveTo(x, area.top);
            ctx.lineTo(x, area.bottom);
            ctx.stroke();
            ctx.restore();
        },
    };
    Chart.register(CROSSHAIR_PLUGIN);

    // A chart is usable only while it still owns a canvas; `destroy()` clears
    // ctx/canvas. Callers rebuild charts on every range change, so every access
    // below is guarded rather than assumed.
    function alive(chart) {
        return !!(chart && chart.ctx && chart.canvas && chart.chartArea);
    }

    // Visible (dataset, index) pairs for one chart at a shared data index.
    function elementsAt(chart, index) {
        var out = [];
        (chart.data.datasets || []).forEach(function (ds, i) {
            if (!chart.isDatasetVisible(i)) return;
            var meta = chart.getDatasetMeta(i);
            if (!meta || meta.hidden || !meta.data || !meta.data[index]) return;
            out.push({ datasetIndex: i, index: index });
        });
        return out;
    }

    // Wire an array of charts that share one x scale (same labels, same order)
    // so hovering any one of them shows the tooltip and crosshair on all of
    // them at the same index.
    //
    // Returns a `{ destroy() }` handle; call it before rebuilding the charts so
    // the listeners do not outlive the canvases they point at. Charts that are
    // destroyed without that are still handled - `alive()` skips them.
    function linkedCrosshair(charts) {
        var group = (charts || []).filter(Boolean);
        if (!group.length) return { destroy: function () {} };
        var wired = [];

        function clearAll() {
            group.forEach(function (c) {
                if (!alive(c)) return;
                c.$crosshairX = null;
                try {
                    c.setActiveElements([]);
                    if (c.tooltip) c.tooltip.setActiveElements([], { x: 0, y: 0 });
                    c.render();
                } catch (e) { /* chart torn down mid-event */ }
            });
        }

        function syncTo(index) {
            group.forEach(function (c) {
                if (!alive(c)) return;
                var els = elementsAt(c, index);
                // The pixel is recomputed per chart: the panels share a data
                // index, not an offset - their y-axis label widths differ, so
                // their plot areas start at different x. The drawn element is
                // the authority; if this panel has nothing drawn at this index
                // (all its series hidden, or a gap) it simply gets no rule
                // rather than a guessed one.
                var first = els.length
                    ? c.getDatasetMeta(els[0].datasetIndex).data[index] : null;
                c.$crosshairX = first ? first.x : null;
                try {
                    c.setActiveElements(els);
                    if (c.tooltip) {
                        c.tooltip.setActiveElements(els, {
                            x: c.$crosshairX == null ? 0 : c.$crosshairX,
                            y: first ? first.y : c.chartArea.top,
                        });
                    }
                    c.render();
                } catch (e) { /* chart torn down mid-event */ }
            });
        }

        group.forEach(function (chart) {
            if (!alive(chart)) return;
            var canvas = chart.canvas;
            var onMove = function (evt) {
                if (!alive(chart)) return;
                // 'index' + intersect:false = "nearest x", the same interaction
                // the tooltips use, so hovering anywhere in the column works.
                var hits = chart.getElementsAtEventForMode(
                    evt, "index", { intersect: false }, false);
                if (!hits || !hits.length) { clearAll(); return; }
                syncTo(hits[0].index, chart);
            };
            var onLeave = function () { clearAll(); };
            canvas.addEventListener("mousemove", onMove);
            canvas.addEventListener("mouseleave", onLeave);
            wired.push({ canvas: canvas, onMove: onMove, onLeave: onLeave });
        });

        return {
            destroy: function () {
                wired.forEach(function (w) {
                    w.canvas.removeEventListener("mousemove", w.onMove);
                    w.canvas.removeEventListener("mouseleave", w.onLeave);
                });
                wired = [];
            },
        };
    }

    window.linkedCrosshair = linkedCrosshair;
}());
