// Shared workout power-curve graph, used by both the calendar (workout modal)
// and the plan page (per-workout graph toggle). Self-contained inline SVG so it
// works offline — no chart library / CDN needed. Exposes two globals:
//   window.fmtDur(sec)                     -> "Nm Ss" / "N min"
//   window.profileSvg(profile, totalS, ftp) -> SVG markup string
(function () {
    function fmtDur(sec) {
        sec = Math.round(sec || 0);
        var m = Math.floor(sec / 60), s = sec % 60;
        return s ? m + "m " + s + "s" : m + " min";
    }

    function zoneClass(wattsStart, wattsEnd, ftp) {
        if (!Number.isFinite(ftp) || ftp <= 0) return "";
        var ratio = ((wattsStart + wattsEnd) / 2) / ftp;
        if (ratio < 0.56) return " pf-zone-1";
        if (ratio <= 0.75) return " pf-zone-2";
        if (ratio <= 0.90) return " pf-zone-3";
        if (ratio <= 1.05) return " pf-zone-4";
        if (ratio <= 1.20) return " pf-zone-5";
        if (ratio <= 1.50) return " pf-zone-6";
        return " pf-zone-7";
    }

    // Target-power LINE graph over the whole workout (x = time into workout,
    // y = target watts; flat steps for constant blocks, slopes for ramps).
    // `opts.bare` draws the SHAPE only - no gridlines, ticks, axis labels or
    // FTP caption. It is for the thumbnail beside a workout choice, where the
    // job is "recognise this shape at a glance", not "read a value off it".
    //
    // Shrinking the full chart instead does not work: label font sizes are in
    // viewBox USER UNITS, so they scale with the box. At card width the watt
    // ticks, the minute ticks and the FTP caption all collapse into each other
    // and read as one run of digits at assorted sizes. Removing them is the
    // fix; there is no size at which they are legible in a 150px-wide box.
    function profileSvg(profile, totalS, ftp, opts) {
        if (!profile || !profile.length || !totalS) return "";
        var bare = !!(opts && opts.bare);
        var W = 560, H = 200, padL = 46, padR = 12, padT = 12, padB = 26;
        if (bare) {
            // Near-zero padding: the shape should fill the thumbnail. A little
            // vertical room stops a max-effort block clipping at the top edge.
            // 2:1 so the viewBox matches the thumbnail box exactly and the
            // shape neither letterboxes nor distorts.
            W = 240; H = 120; padL = 2; padR = 2; padT = 8; padB = 2;
        }
        var plotW = W - padL - padR, plotH = H - padT - padB;
        var ftpW = Number(ftp), hasFtp = Number.isFinite(ftpW) && ftpW > 0;
        var maxW = hasFtp ? ftpW : 0;
        profile.forEach(function (b) {
            maxW = Math.max(maxW, b.watts_start, b.watts_end);
        });
        var yMax = Math.max(100, Math.ceil(maxW / 50) * 50);
        var x = function (t) { return (padL + plotW * (t / totalS)).toFixed(1); };
        var y = function (w) { return (padT + plotH * (1 - w / yMax)).toFixed(1); };

        var yStep = [25, 50, 100, 200, 400].find(function (s) { return yMax / s <= 5; }) || 400;
        var totalMin = totalS / 60;
        var xStep = [5, 10, 15, 30, 60, 120].find(function (s) { return totalMin / s <= 8; }) || 120;

        // Size via ATTRIBUTES, not a stylesheet rule. These nodes are parsed
        // with DOMParser and imported, and the .profile-svg CSS does not reach
        // them - measured: they compute to display:inline/width:auto. The full
        // preview happens to get a box from its container anyway; a fixed-size
        // thumbnail does not, and collapses to 0x0.
        // The xmlns is REQUIRED. Callers parse this with DOMParser as
        // "image/svg+xml", and XML parsing does not infer namespaces the way
        // the HTML parser does: without it the root lands with a null
        // namespace, so the browser builds plain unknown Elements instead of
        // SVGSVGElement. Nothing renders as a graph, no .profile-svg CSS
        // applies, and the <text>/<title> contents get laid out as ordinary
        // inline HTML - which is what made a workout card read as one run of
        // digits and words at assorted sizes.
        var svg = '<svg xmlns="http://www.w3.org/2000/svg" ' +
                  'viewBox="0 0 ' + W + ' ' + H + '" class="profile-svg' +
                  (bare ? ' profile-svg-bare" width="100%" height="100%' : '') +
                  '" role="img" ' +
                  'aria-label="Target power over workout time">';
        if (!bare) {
            // horizontal gridlines + y labels (watts)
            for (var w = 0; w <= yMax; w += yStep) {
                svg += '<line x1="' + padL + '" y1="' + y(w) + '" x2="' + (W - padR) +
                       '" y2="' + y(w) + '" class="pf-grid"/>' +
                       '<text x="' + (padL - 5) + '" y="' + y(w) + '" class="pf-ylab">' + w + '</text>';
            }
            // x ticks + labels (minutes)
            for (var mn = 0; mn <= totalMin + 0.001; mn += xStep) {
                var t = Math.min(mn * 60, totalS);
                svg += '<line x1="' + x(t) + '" y1="' + (H - padB) + '" x2="' + x(t) +
                       '" y2="' + (H - padB + 4) + '" class="pf-grid"/>' +
                       '<text x="' + x(t) + '" y="' + (H - 8) + '" class="pf-xlab">' + Math.round(mn) + 'm</text>';
            }
        }
        // One closed fill per prescribed segment, classified by its average
        // target. A missing FTP retains the neutral base fill.
        profile.forEach(function (b) {
            if (b.free) return;
            var area = 'M ' + x(b.start) + ' ' + y(0) +
                       ' L ' + x(b.start) + ' ' + y(b.watts_start) +
                       ' L ' + x(b.end) + ' ' + y(b.watts_end) +
                       ' L ' + x(b.end) + ' ' + y(0) + ' Z';
            svg += '<path d="' + area + '" class="pf-area' +
                   zoneClass(b.watts_start, b.watts_end, hasFtp ? ftpW : NaN) +
                   '"/>';
        });

        // The target line is BROKEN across any untargeted block. Tracing it
        // through a free block draws a target the rider is told to ignore -
        // it plots the ERG resistance, not a prescription - so each run of
        // targeted blocks becomes its own subpath.
        var runs = [], current = null;
        profile.forEach(function (b) {
            if (b.free) { current = null; return; }
            if (!current) { current = []; runs.push(current); }
            current.push(b);
        });
        var line = '';
        runs.forEach(function (run) {
            var first = run[0];
            line += ' M ' + x(first.start) + ' ' + y(first.watts_start);
            run.forEach(function (b) {
                var step = ' L ' + x(b.start) + ' ' + y(b.watts_start) +
                           ' L ' + x(b.end) + ' ' + y(b.watts_end);
                line += step;
            });
        });
        if (line) svg += '<path d="' + line.trim() + '" class="pf-line"/>';
        // dashed FTP reference. The line survives in bare mode - it is what
        // makes the shape readable as "above/below threshold" - but its caption
        // does not: the number is already stated in the card's own text.
        if (hasFtp && ftpW <= yMax) {
            svg += '<line x1="' + padL + '" y1="' + y(ftpW) + '" x2="' + (W - padR) +
                   '" y2="' + y(ftpW) + '" class="pf-ftp"/>';
            if (!bare) {
                svg += '<text x="' + (W - padR - 2) + '" y="' + (parseFloat(y(ftpW)) - 3) +
                       '" class="pf-ftplab">FTP ' + Math.round(ftpW) + 'W</text>';
            }
        }
        // Untargeted blocks (sprints) are shaded rather than read as a power
        // step: the plotted watts there are only the resistance the trainer
        // holds, so quoting them as a target would be wrong.
        profile.forEach(function (b) {
            if (!b.free) return;
            svg += '<rect x="' + x(b.start) + '" y="' + padT + '" width="' +
                   Math.max(1, (plotW * (b.end - b.start) / totalS)).toFixed(1) +
                   '" height="' + plotH + '" class="pf-free"/>';
        });
        // hover targets: one transparent rect per block with a native tooltip.
        // Not in bare mode. These <title> nodes are real text in the accessible
        // tree, so inside a button they get read out as part of its name and
        // flattened into its text content - they were a large part of the
        // run-together string the card used to show. The card's own label and
        // the full preview below carry this detail.
        if (!bare) profile.forEach(function (b) {
            var label = fmtDur(b.start) + '–' + fmtDur(b.end) + ' · ' +
                (b.free
                    ? 'max effort — no target'
                    : b.watts_start === b.watts_end
                    ? b.watts_start + ' W'
                    : b.watts_start + '→' + b.watts_end + ' W');
            svg += '<rect x="' + x(b.start) + '" y="' + padT + '" width="' +
                   Math.max(1, (plotW * (b.end - b.start) / totalS)).toFixed(1) +
                   '" height="' + plotH + '" fill="transparent"><title>' + label +
                   '</title></rect>';
        });
        svg += '</svg>';
        return svg;
    }

    window.fmtDur = fmtDur;
    window.profileSvg = profileSvg;
})();
