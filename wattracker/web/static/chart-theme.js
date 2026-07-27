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

var MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Compact date ticks for a category axis whose labels are ISO dates. Pass the
// label array, get back a Chart.js `ticks.callback`.
//
// The format follows the window, because one rule cannot serve both ends of the
// range this app spans:
//
//   long window  (> ~10 weeks) - month names, the year only on the first tick
//                 and at year boundaries ("Jan 2026"); further ticks inside the
//                 same month are blanked, so a two-year view is not a wall of
//                 repeated words.
//   short window - day-of-month, with the month named on the first tick and
//                 whenever it changes ("22 Jun", "29", "6 Jul"). The month rule
//                 applied here labelled only the month boundaries, so the
//                 dashboard's default 1-month view showed two ticks for thirty
//                 days of daily data, and the volume page's 1m preset showed
//                 two for five weekly buckets.
//
// Lives here rather than in a page script because every dated category axis in
// the app wants the same treatment (the dashboard panels and the volume small
// multiples both do) and a second copy would drift.
var SHORT_WINDOW_DAYS = 70;

function monthYearTicks(labels) {
    var first = labels[0] || "";
    var last = labels[labels.length - 1] || "";
    var spanDays = (Date.parse(last) - Date.parse(first)) / 86400000;
    var byDay = isFinite(spanDays) && spanDays <= SHORT_WINDOW_DAYS;
    return function (value, index, ticks) {
        var label = labels[value] || "";
        var y = label.slice(0, 4);
        var m = parseInt(label.slice(5, 7), 10) - 1;
        if (!(m >= 0 && m <= 11)) return label;
        var prev = index > 0 && ticks[index - 1]
            ? (labels[ticks[index - 1].value] || "") : "";
        var sameMonth = prev && prev.slice(0, 7) === label.slice(0, 7);
        if (byDay) {
            var day = parseInt(label.slice(8, 10), 10);
            return sameMonth ? String(day) : day + " " + MONTH_NAMES[m];
        }
        if (!prev || prev.slice(0, 4) !== y) return MONTH_NAMES[m] + " " + y;
        if (sameMonth) return "";
        return MONTH_NAMES[m];
    };
}

// Choose the ticks for a dated category axis. Use as `afterBuildTicks`, paired
// with `monthYearTicks` as the `ticks.callback`.
//
// Chart.js' own `autoSkip` cannot be used on stacked panels, for two reasons
// that only appear once several charts have to share one x axis:
//
//   * it is gated on `ticks.display`, so the panels that draw no labels never
//     skip at all. The dashboard's two fitness panels were building 29 ticks on
//     top against 10 on the bottom - i.e. their gridlines were never in the
//     same columns, which is the whole point of stacking them.
//   * it thins to an evenly spaced subset chosen by pixel budget, with no idea
//     that on a long window the interesting dates are the month boundaries. On
//     a 62-week volume window it kept 16 ticks of which only 4 landed on a
//     month start, so `monthYearTicks` had nothing left to name.
//
// Choosing explicitly fixes both: every panel runs the same selection and lands
// on the same dates, and the dates kept are the ones that carry a label.
function dateAxisTicks(maxTicks) {
    var max = maxTicks || 12;
    return function (scale) {
        var labels = scale.chart.data.labels || [];
        var ticks = scale.ticks || [];
        if (ticks.length <= max) return; // everything fits; keep the lot
        var first = labels[0] || "";
        var last = labels[labels.length - 1] || "";
        var spanDays = (Date.parse(last) - Date.parse(first)) / 86400000;
        if (!(spanDays > SHORT_WINDOW_DAYS)) {
            // Short window, too many samples to label every one (a month of
            // daily data). `monthYearTicks` is naming days here, so any evenly
            // spaced subset labels fine - just make it the SAME subset on every
            // panel, which a stride does and autoSkip does not.
            var step = Math.ceil(ticks.length / max);
            scale.ticks = ticks.filter(function (_t, i) { return i % step === 0; });
            return;
        }
        // Long window: keep the first sample of each month.
        var firsts = ticks.filter(function (t) {
            var cur = labels[t.value] || "";
            var prev = labels[t.value - 1] || "";
            return !prev || prev.slice(0, 7) !== cur.slice(0, 7);
        });
        // Tick 0 is kept unconditionally (it has no predecessor to compare
        // with), but a series starting late in a month puts it within a sample
        // or two of the real first boundary and the two labels overprint -
        // "May 2025Jun".
        if (firsts.length > 1 && firsts[1].value - firsts[0].value < 3) firsts.shift();
        // More months than will fit: drop to every k-th MONTH rather than to
        // arbitrary dates, so every survivor still carries a label.
        var stride = Math.ceil(firsts.length / max);
        scale.ticks = stride > 1
            ? firsts.filter(function (_t, i) { return i % stride === 0; })
            : firsts;
    };
}

// The same tick problem for an axis whose labels carry no calendar meaning
// (the activity-detail panels are minutes into the ride). No month logic to
// apply, but stacked panels still have to thin identically, and `autoSkip`
// still will not do it for a panel that draws no labels.
function strideTicks(maxTicks) {
    var max = maxTicks || 12;
    return function (scale) {
        var ticks = scale.ticks || [];
        if (ticks.length <= max) return;
        var step = Math.ceil(ticks.length / max);
        scale.ticks = ticks.filter(function (_t, i) { return i % step === 0; });
    };
}

// ------------------------------------------------------- panel alignment
// N charts stacked as one plot with one shared x domain (the dashboard's two
// fitness panels, the volume page's four small multiples) have to line up on
// both edges, and Chart.js will not do it for them:
//
//   left  - each y axis is sized to its own tick text, so panels with
//           different value ranges start their plot areas at different x and
//           read as unrelated charts.
//   right - only the bottom panel draws x tick labels, so only it reserves
//           room for the last label to overhang the axis (~5px). Left
//           unmatched, every gridline walks out of column between panels.
//
// Both are layout *output*, not scale properties, so both are measured after a
// render rather than guessed, and both are corrected the same way: explicit
// layout padding. One measure pass, one entry point.
//
// Two things the callers must do:
//   1. Declare `layout: { padding: { left: 0, right: 0 } }` at construction.
//      On Chart.js v4 `chart.options` is a resolver proxy and assigning a
//      nested OBJECT into it after construction recurses into a stack
//      overflow; only leaf assignment is safe, and a leaf needs its object to
//      already exist. (Chart.js's own default is the scalar `padding: 0`,
//      which would silently swallow `.left = 12`.)
//   2. Re-run this whenever the tick text can change - new data window, new
//      zoom range, resize - via `schedulePanelAlign`.
function alignPanels(charts) {
    var live = (charts || []).filter(function (c) {
        return c && c.ctx && c.chartArea && c.options && c.options.layout &&
            c.options.layout.padding && typeof c.options.layout.padding === "object";
    });
    if (live.length < 2) return;
    // Zero the existing reservation before measuring. Measuring with it still
    // applied folds the previous correction into the new one, so the insets
    // could only ever grow - a window whose labels got narrower would keep the
    // old, too-wide gutter forever.
    live.forEach(function (c) {
        c.options.layout.padding.left = 0;
        c.options.layout.padding.right = 0;
        c.update("none");
    });
    var natural = live.map(function (c) {
        return { left: c.chartArea.left, right: c.width - c.chartArea.right };
    });
    var maxLeft = 0;
    var maxRight = 0;
    natural.forEach(function (n) {
        if (n.left > maxLeft) maxLeft = n.left;
        if (n.right > maxRight) maxRight = n.right;
    });
    live.forEach(function (c, i) {
        // Left is a per-panel shim to the widest y axis; right is one shared
        // reservation (Chart.js takes the larger of the padding and the
        // scale's own overhang, so handing every panel the widest overhang
        // lands them all on the same right edge).
        c.options.layout.padding.left = Math.max(0, maxLeft - natural[i].left);
        c.options.layout.padding.right = Math.ceil(maxRight);
        c.update("none");
    });
}

// rAF-coalesced `alignPanels`. A range change touches every panel and every
// touch would otherwise trigger its own two-pass measure; this collapses a
// burst into one alignment on the next frame. Groups are de-duplicated by
// their member chart ids, so callers can pass a fresh array each time.
var alignQueue = [];

function schedulePanelAlign(charts) {
    var group = (charts || []).filter(Boolean);
    if (group.length < 2) return;
    var sig = group.map(function (c) { return c.id; }).join(",");
    if (alignQueue.some(function (q) { return q.sig === sig; })) return;
    alignQueue.push({ sig: sig, charts: group });
    if (alignQueue.length > 1) return;
    requestAnimationFrame(function () {
        var due = alignQueue;
        alignQueue = [];
        due.forEach(function (q) { alignPanels(q.charts); });
    });
}

(function () {
    "use strict";

    window.tokenAlpha = tokenAlpha;
    window.monthYearTicks = monthYearTicks;
    window.dateAxisTicks = dateAxisTicks;
    window.strideTicks = strideTicks;
    window.alignPanels = alignPanels;
    window.schedulePanelAlign = schedulePanelAlign;

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
