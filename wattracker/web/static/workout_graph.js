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

    // Target-power LINE graph over the whole workout (x = time into workout,
    // y = target watts; flat steps for constant blocks, slopes for ramps).
    function profileSvg(profile, totalS, ftp) {
        if (!profile || !profile.length || !totalS) return "";
        var W = 560, H = 200, padL = 46, padR = 12, padT = 12, padB = 26;
        var plotW = W - padL - padR, plotH = H - padT - padB;
        var maxW = ftp || 0;
        profile.forEach(function (b) {
            maxW = Math.max(maxW, b.watts_start, b.watts_end);
        });
        var yMax = Math.max(100, Math.ceil(maxW / 50) * 50);
        var x = function (t) { return (padL + plotW * (t / totalS)).toFixed(1); };
        var y = function (w) { return (padT + plotH * (1 - w / yMax)).toFixed(1); };

        var yStep = [25, 50, 100, 200, 400].find(function (s) { return yMax / s <= 5; }) || 400;
        var totalMin = totalS / 60;
        var xStep = [5, 10, 15, 30, 60, 120].find(function (s) { return totalMin / s <= 8; }) || 120;

        var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" class="profile-svg" role="img" ' +
                  'aria-label="Target power over workout time">';
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
        // Target area and line, BROKEN across any untargeted block. Tracing
        // them through a free block draws a target the rider is told to
        // ignore - it plots the ERG resistance, not a prescription - so each
        // run of targeted blocks becomes its own subpath.
        var runs = [], current = null;
        profile.forEach(function (b) {
            if (b.free) { current = null; return; }
            if (!current) { current = []; runs.push(current); }
            current.push(b);
        });
        var area = '', line = '';
        runs.forEach(function (run) {
            var first = run[0], last = run[run.length - 1];
            area += ' M ' + x(first.start) + ' ' + y(0);
            line += ' M ' + x(first.start) + ' ' + y(first.watts_start);
            run.forEach(function (b) {
                var step = ' L ' + x(b.start) + ' ' + y(b.watts_start) +
                           ' L ' + x(b.end) + ' ' + y(b.watts_end);
                area += step;
                line += step;
            });
            area += ' L ' + x(last.end) + ' ' + y(0) + ' Z';
        });
        if (area) svg += '<path d="' + area.trim() + '" class="pf-area"/>';
        if (line) svg += '<path d="' + line.trim() + '" class="pf-line"/>';
        // dashed FTP reference
        if (ftp && ftp <= yMax) {
            svg += '<line x1="' + padL + '" y1="' + y(ftp) + '" x2="' + (W - padR) +
                   '" y2="' + y(ftp) + '" class="pf-ftp"/>' +
                   '<text x="' + (W - padR - 2) + '" y="' + (parseFloat(y(ftp)) - 3) +
                   '" class="pf-ftplab">FTP ' + Math.round(ftp) + 'W</text>';
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
        // hover targets: one transparent rect per block with a native tooltip
        profile.forEach(function (b) {
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
