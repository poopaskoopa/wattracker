"""Route + WebSocket tests for the Ride page (no hardware; availability is
forced via monkeypatch so results don't depend on whether bleak is installed)."""
import asyncio
import datetime as dt
import re

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.ble import devices as bledevices  # noqa: E402
from wattracker.server import create_app  # noqa: E402
from wattracker import server as servermod  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def _receive_after_workout(ws):
    workout = ws.receive_json()
    assert workout["status"] == "workout"
    assert workout["workout"]["name"]
    assert workout["workout"]["duration_s"] > 0
    assert workout["workout"]["profile"]
    return ws.receive_json()


def _force_bt_unavailable(monkeypatch):
    # Force the "no Bluetooth" branch regardless of whether the [ble] extra
    # (bleak) is installed in the test environment, so the suite is deterministic
    # for developers who have installed real-hardware support.
    monkeypatch.setattr(
        bledevices,
        "bluetooth_available",
        lambda: (False, "bleak not installed (ModuleNotFoundError)"),
    )


def test_ride_page_renders_unavailable(client, monkeypatch):
    # Bluetooth unavailable -> page still loads and offers Simulate.
    _force_bt_unavailable(monkeypatch)
    _register(client)
    r = client.get("/ride")
    assert r.status_code == 200
    assert "Bluetooth unavailable" in r.text
    assert "Simulate" in r.text


def _add_plan_workout(client, date, name="Selected workout", username="rider"):
    uid = db.get_user_by_username(username)["id"]
    plan_id = db.create_plan(uid, name, date, 1)
    return db.add_plan_workout(
        plan_id,
        uid,
        date,
        name,
        "endurance",
        3600,
        50.0,
        "<workout_file/>",
    )


def test_ride_page_deep_link_preselects_owned_workout(client):
    _register(client)
    workout_id = _add_plan_workout(client, "2099-01-02")

    r = client.get(f"/ride?workout_id={workout_id}")

    assert r.status_code == 200
    assert re.search(
        rf'<option value="{workout_id}" data-name="Selected workout"\s+selected>',
        r.text,
    )


def test_ride_page_without_deep_link_keeps_default_selection(client):
    _register(client)
    workout_id = _add_plan_workout(client, "2099-01-02")

    r = client.get("/ride")

    assert r.status_code == 200
    assert '<option value="" data-name="Endurance">Endurance (45 min)</option>' in r.text
    assert not re.search(
        rf'<option value="{workout_id}" data-name="Selected workout"\s+selected>',
        r.text,
    )


def test_ride_page_uses_users_local_calendar_day(client, monkeypatch):
    frozen = dt.datetime(2026, 1, 2, 0, 30)
    monkeypatch.setattr(servermod, "utc_now", lambda: frozen)
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"timezone": "America/New_York"})
    _add_plan_workout(client, "2026-01-01", "New York workout")

    assert "New York workout" in client.get("/ride").text

    client.post("/logout")
    _register(client, "utc-rider")
    _add_plan_workout(
        client, "2026-01-01", "UTC workout", username="utc-rider"
    )
    assert "UTC workout" not in client.get("/ride").text


def test_settings_timezone_persists_and_invalid_value_is_safe(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]

    response = client.post("/settings", data={"timezone": "Europe/Paris"})
    assert response.status_code == 200
    assert db.get_user_settings(uid)["timezone"] == "Europe/Paris"
    assert 'value="Europe/Paris"' in client.get("/settings").text

    response = client.post(
        "/settings", data={"timezone": "../../etc/passwd"}
    )
    assert response.status_code == 200
    assert "Invalid IANA time zone" in response.text
    assert db.get_user_settings(uid)["timezone"] == "Europe/Paris"


def test_ride_page_deep_link_includes_past_workout(client):
    _register(client)
    workout_id = _add_plan_workout(client, "2000-01-02", "Past workout")

    r = client.get(f"/ride?workout_id={workout_id}")

    assert r.status_code == 200
    assert re.search(
        rf'<option value="{workout_id}" data-name="Past workout"\s+selected>',
        r.text,
    )


def test_ride_page_deep_link_includes_workout_outside_upcoming_cap(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.create_plan(uid, "Many workouts", "2099-01-01", 1)
    workout_ids = [
        db.add_plan_workout(
            plan_id,
            uid,
            "2099-01-01",
            f"Workout {day}",
            "endurance",
            3600,
            50.0,
            "<workout_file/>",
        )
        for day in range(1, 42)
    ]
    selected_id = workout_ids[-1]

    r = client.get(f"/ride?workout_id={selected_id}")

    assert r.status_code == 200
    assert r.text.count(f'<option value="{selected_id}"') == 1
    assert re.search(
        rf'<option value="{selected_id}" data-name="Workout 41"\s+selected>',
        r.text,
    )


def test_ride_page_deep_link_rejects_missing_and_foreign_workouts(client):
    _register(client, "alice")
    foreign_id = _add_plan_workout(client, "2099-01-02", username="alice")
    client.post("/logout")
    _register(client, "bob")

    assert client.get(f"/ride?workout_id={foreign_id}").status_code == 404
    assert client.get("/ride?workout_id=999999").status_code == 404


@pytest.mark.parametrize("workout_id", ["0", "-1", str(2**63), str(10**100)])
def test_ride_page_deep_link_rejects_out_of_range_ids(client, workout_id):
    _register(client)

    r = client.get(f"/ride?workout_id={workout_id}")

    assert r.status_code == 404
    assert r.text == "Workout not found"


def test_ride_page_renders_available_when_monkeypatched(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))
    r = client.get("/ride")
    assert r.status_code == 200
    assert "Bluetooth available" in r.text
    assert "Connect selected sensors" in r.text
    # The separate "Connect & ride" button is gone: both buttons did the same
    # thing once the prepare/start two-step was removed.
    assert 'id="startBtn"' not in r.text
    assert "Connect &amp; ride" not in r.text
    assert 'input[data-role="power"]:checked' in r.text
    assert "replaceChildren" in r.text
    assert "innerHTML" not in r.text
    assert 'id="connectionStatus"' in r.text
    assert "deviceNames[deviceAddress]" in r.text
    assert 'setLive(document.getElementById("connectionStatus"), message' in r.text
    assert "preferredPower" in r.text
    assert "No HR" in r.text
    assert "No trainer" in r.text
    assert "No cadence sensor" in r.text
    assert 'input[data-role="cadence"]:checked' in r.text
    assert 'q.push("prepare=1")' not in r.text
    assert 'ws.send(JSON.stringify({action: "start"}))' not in r.text
    assert "ready — pedal to start" in r.text
    assert 'id="scanBusy"' in r.text
    assert "localStorage.getItem(cacheKey)" in r.text
    assert "new Chart" in r.text
    assert "MAX_CHART_POINTS" in r.text
    assert "MAX_CHART_POINTS = 30000" in r.text
    assert "appendSmoothed(livePower, smoothPower, powerSmoother" in r.text
    assert "appendSmoothed(liveHr, smoothHr, hrSmoother" in r.text
    assert "normalized: true" not in r.text
    assert "hasWarnings ? \"failure\" : \"success\"" in r.text
    assert "primeAudio();" in r.text
    assert "function resetConnectedRows()" in r.text
    assert "if (ws !== socket) return" in r.text
    assert 'ws = null;' in r.text
    assert '"Disconnected.", null' in r.text
    assert "Bluetooth connection failed. Check the device and try again." in r.text
    assert 'socket.onmessage = function (ev) {\n            if (ws !== socket) return;' in r.text
    assert 'decimation: {enabled: true, algorithm: "lttb", samples: 1000' in r.text
    assert r.text.count("var wid") == 1
    assert 'getElementById("workoutSelect").disabled = controlsLocked' in r.text
    assert 'id="rideChartPanel"' in r.text
    assert 'id="rideChart"' in r.text
    assert 'id="rideHrChart"' not in r.text
    assert 'id="hrChartBlock"' not in r.text
    assert 'id="rideChartSummary"' in r.text
    assert 'aria-label="Target and measured power, cadence, and heart rate over workout time"' in r.text
    assert 'id="chartPowerValue"' in r.text
    assert 'id="chartCadenceValue"' in r.text
    assert 'id="chartHrValue"' in r.text
    assert "<span class=\"label\">Cadence</span>" in r.text
    assert "devices.slice().filter" in r.text
    assert 'device.name === "(unknown)"' in r.text
    assert "unnamed device" in r.text
    assert "playCue(\"scan\")" in r.text
    assert "finally" in r.text
    assert r.text.index('id="scanBusy"') > r.text.index('id="connectBtn"')
    assert 'className = "device-disconnect button-secondary"' in r.text
    assert 'requestDisconnect(deviceAddress);' in r.text
    assert r.text.count('aria-busy="false"') >= 2
    assert '"Connecting…", "Connect selected sensors"' in r.text
    assert '"Disconnecting " + displayName(address) + "…"' in r.text
    assert "device-disconnect-pending" in r.text
    assert "RECONNECT_RELEASE_DELAY_MS = 750" in r.text
    assert "var reconnectTimer = null;" in r.text
    assert "function cancelPendingReconnect()" in r.text
    assert "window.clearTimeout(reconnectTimer);" in r.text
    assert "reconnectTimer = window.setTimeout(function ()" in r.text
    assert r.text.index("reconnectTimer = null;\n                    openRide(nextOpen.sim);") > r.text.index(
        "reconnectTimer = window.setTimeout(function ()"
    )
    assert "var cancelledReconnect = cancelPendingReconnect();" in r.text
    assert '"Pending reconnect cancelled — ready to Scan or Connect."' in r.text
    assert '"Bluetooth released — ready to Scan or Connect."' in r.text
    assert 'state === "connecting" || !bleAvailable' in r.text
    assert "if (!resp.ok)" in r.text
    assert "Wake or spin the device" in r.text
    assert 'text: "Workout time"' in r.text
    assert "min: 0, max: duration" in r.text
    # The axis reads as a clock (mm:ss, h:mm:ss past the hour), not decimal minutes.
    assert "callback: fmtAxisTime" in r.text
    assert "function fmtAxisTime(value)" in r.text
    assert "var minutes = Number(value) / 60;" not in r.text
    # Ending the ride must drop out of the full-screen overlay; otherwise the
    # rider is left on a fixed, never-updating chart covering the whole page.
    assert "if (!active) exitChartFullscreen();" in r.text
    # Both end buttons confirm first, and ending asks the server to finalize
    # rather than silently dropping the socket.
    assert 'document.getElementById("stopBtn").addEventListener("click", requestEndRide)' in r.text
    assert 'document.getElementById("endWorkoutBtn").addEventListener("click", requestEndRide)' in r.text
    assert "function requestEndRide()" in r.text
    assert "window.confirm(" in r.text
    assert 'socket.send(JSON.stringify({action: "stop"}))' in r.text
    assert '{label: "Target power", data: prescribed, yAxisID: "y"' in r.text
    # The prominent measured trace is the rolling mean; the raw samples survive
    # only as a faint point-less ghost behind it.
    assert '{label: "Measured power (" + POWER_SMOOTH_S + "s)", data: smoothPower, yAxisID: "y"' in r.text
    assert '{label: "Measured power (raw)", data: livePower, yAxisID: "y"' in r.text
    assert 'borderColor: tokenAlpha("--s-2", 0.40), pointRadius: 0' in r.text
    assert "var POWER_SMOOTH_S = 3, METRIC_SMOOTH_S = 5;" in r.text
    assert "function smoothedValue(state, x, y)" in r.text
    # The live traces are full-opacity. The 0.7 they used to carry was tuned
    # to take the glare off the old near-white brights; against the darker
    # series tokens it only cost contrast, putting heart rate under the 3:1
    # floor on the panel that gets read from the bike.
    assert 'borderColor: cssVar("--s-2")' in r.text
    assert 'borderColor: cssVar("--s-3")' in r.text
    assert 'borderColor: cssVar("--s-hr")' in r.text
    assert 'tokenAlpha("--s-3"' not in r.text
    assert 'tokenAlpha("--s-hr"' not in r.text
    assert 'id="rideChartTitle"' in r.text
    assert 'workout.name || "Workout metrics"' in r.text
    # In plan mode setupWorkout only runs once the ride connects, so the heading
    # follows the select until then.
    assert "function showSelectedWorkoutTitle()" in r.text
    assert '<option value="" data-name="Endurance">' in r.text
    assert 'document.getElementById("workoutSelect").addEventListener("change", function () {' in r.text
    assert 'id="ergIndicator"' in r.text
    assert 'indicator.classList.toggle("erg-lit", ergEnabled)' in r.text
    assert "function fmtHms(sec)" in r.text
    assert "function blockRemaining(elapsed)" in r.text
    assert 'id="clockElapsed"' in r.text
    assert 'id="clockBlock"' in r.text
    assert 'id="clockTotal"' in r.text
    # Intermediate gridlines exist only as intermediate ticks in Chart.js v4.
    assert "grid: {drawTicks: true, tickLength: 8, tickColor: tickMark, color: gridLine}" in r.text
    assert "function timeStepSize(duration)" in r.text
    assert "stepSize: timeStepSize(duration)" in r.text
    # A fixed watt step would give an easy 110 W ride three gridlines, so the
    # step is chosen from the live range once the data limits are known.
    assert "function powerStepSize(max)" in r.text
    assert "scale.options.ticks.stepSize = powerStepSize(scale.max);" in r.text
    assert "stepSize: 50, maxTicksLimit: 14" in r.text
    # With no cadence or HR sensor the right-hand axis has no data and would
    # otherwise collapse to a 0-1 scale.
    assert 'metrics: {beginAtZero: true, position: "right", suggestedMax: 200,' in r.text
    # setupWorkout is destructive, so a preview response that arrives after the
    # rider changed mode or duration again must be discarded.
    assert "var previewToken = 0;" in r.text
    assert "var token = ++previewToken;" in r.text
    assert "if (token !== previewToken || !justRide()) return;" in r.text
    assert "grid: {drawOnChartArea: false, drawTicks: true, tickLength: 8," in r.text
    assert "borderDash: [8, 4]" in r.text
    assert '{label: "Cadence", data: smoothCadence, yAxisID: "metrics"' in r.text
    assert '{label: "Heart rate", data: smoothHr, yAxisID: "metrics"' in r.text
    assert 'metrics: {beginAtZero: true, position: "right"' in r.text
    assert 'text: "Cadence (rpm) / Heart rate (bpm)"' in r.text
    assert "appendSmoothed(liveHr" in r.text
    assert "if (rideChart) {" in r.text
    assert "rideChart.destroy();" in r.text
    # A dot per sample was the chart's main source of visual noise.
    assert "pointRadius: 2.5" not in r.text
    assert "pointHoverRadius: 4" in r.text
    # The target is a prescription of steps and ramps: it must not be rounded.
    assert "borderDash: [8, 4], tension: 0, order: 0" in r.text
    assert "borderWidth: 3" in r.text
    assert 'id="chartFullscreenBtn"' in r.text
    assert 'aria-controls="rideChartPanel"' in r.text
    assert "panel.requestFullscreen || panel.webkitRequestFullscreen" in r.text
    assert '"ride-chart-fullscreen-fallback"' in r.text
    assert 'event.key === "Escape"' in r.text
    assert 'button.setAttribute("aria-pressed", active ? "true" : "false")' in r.text
    assert "rideChart.resize();" in r.text
    assert 'return number == null ? "—" : String(Math.round(number));' in r.text
    assert 'id="ergBtn"' in r.text
    assert '{action: "set_erg", enabled: !ergEnabled}' in r.text


def test_ride_page_plays_countdown_cues_before_block_and_workout_ends(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))
    r = client.get("/ride")
    assert r.status_code == 200
    # New cue kinds ride on the existing playCue/primeAudio path.
    assert "function playTone(ctx, frequency, delay, duration)" in r.text
    assert "var CUE_GAIN = 0.12;" in r.text
    assert "gain.gain.setValueAtTime(CUE_GAIN, start);" in r.text
    assert "gain.gain.exponentialRampToValueAtTime(0.001, start + duration);" in r.text
    assert 'if (kind === "countdown") { playTone(ctx, 660, 0, 0.1); return; }' in r.text
    assert 'if (kind === "blockChange") { playTone(ctx, 1046, 0, 0.4); return; }' in r.text
    assert 'if (kind === "workoutEnd") {' in r.text
    # The end-of-workout motif is three notes, so it is audibly distinct.
    assert "playTone(ctx, 660, 0, 0.16);" in r.text
    assert "playTone(ctx, 880, 0.2, 0.16);" in r.text
    assert "playTone(ctx, 1320, 0.4, 0.5);" in r.text
    assert "var CUE_LEAD = 3;" in r.text
    assert "var cuesPlayed = {};" in r.text
    # Bookkeeping is keyed on the boundary, not on an elapsed equality test.
    assert "function cueOnce(key, threshold, elapsed, kind)" in r.text
    assert "if (!(threshold > 0) || elapsed < threshold || cuesPlayed[key]) return;" in r.text
    assert "cuesPlayed[key] = true;" in r.text
    assert 'if (elapsed - threshold < 1.5) playCue(kind);' in r.text
    assert "function updateAudioCues(elapsed)" in r.text
    assert "if (!(elapsed > 0)) return;" in r.text
    assert 'cueOnce("block:" + boundary + ":" + n, boundary - n, elapsed, "countdown");' in r.text
    assert 'cueOnce("block:" + boundary, boundary, elapsed, "blockChange");' in r.text
    assert 'cueOnce("end:" + n, workoutDuration - n, elapsed, "countdown");' in r.text
    assert 'cueOnce("end", workoutDuration, elapsed, "workoutEnd");' in r.text
    # The final block ends with the workout; only the workout-end cue fires.
    assert "if (boundary <= 0 || (workoutDuration > 0 && boundary >= workoutDuration)) continue;" in r.text
    assert "updateAudioCues(elapsed);" in r.text
    # A second ride in the same page session starts from a clean slate.
    assert "cuesPlayed = {};" in r.text


def test_ride_page_just_ride_controls_preview_and_telemetry_locking(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))
    text = client.get("/ride").text

    assert '<div class="ride-mode" role="radiogroup" aria-label="Ride mode">' in text
    assert 'class="ride-mode-button" data-ride-mode="plan"' in text
    assert 'class="ride-mode-button" data-ride-mode="just"' in text
    assert 'aria-pressed="true" aria-controls="planMode"' in text
    assert 'aria-pressed="false" aria-controls="justRideMode"' in text
    assert 'type="radio"' not in text
    assert 'id="justRideMode" class="just-ride-layout"' in text
    assert 'id="rideVariantPicker" class="ride-variant-picker"' in text
    assert 'id="rideVariantGrid" class="ride-variant-grid"' in text
    assert 'role="listbox"' in text
    assert 'className = "ride-variant-card"' in text
    assert "variant_profiles" in text
    assert 'card.tabIndex = key === wanted ? 0 : -1;' in text
    assert 'keyName === "ArrowRight"' in text
    assert 'keyName === "Home"' in text
    assert 'q.push("variant=" + encodeURIComponent(selectedVariant || "classic"));' in text
    assert 'id="ridePreview" class="ride-preview"' in text
    assert "workout_graph.js" in text
    assert "window.profileSvg(data.profile, data.duration_s, data.ftp)" in text
    assert "document.importNode(parsed.documentElement, true)" in text
    assert 'className = "ride-preview-graph"' in text
    assert 'className = "table-scroll"' in text

    # Connected/zero-power leaves setup controls available; first positive
    # telemetry frame locks them for the remainder of the active ride.
    assert "var positivePowerSeen = false;" in text
    assert "var controlsLocked = active && positivePowerSeen;" in text
    assert "if (finiteNumber(st.power) != null && finiteNumber(st.power) > 0)" in text
    assert "document.getElementById(\"rideTypeSelect\").disabled = controlsLocked;" in text
    assert "document.getElementById(\"rideDurationSelect\").disabled = controlsLocked;" in text
    assert "modeButtons().forEach(function (button) { button.disabled = controlsLocked; });" in text
    assert "if (!active) {" in text
    assert "positivePowerSeen = false;" in text
    assert "document.getElementById(\"stopBtn\").disabled = !active;" in text
    assert "document.getElementById(\"endWorkoutBtn\").disabled = !active;" in text
    assert "function selectMode(mode)" in text
    assert "button.setAttribute(\"aria-pressed\", button.dataset.rideMode === mode ? \"true\" : \"false\");" in text

    # Each selection invalidates prior preview requests, including a mode
    # switch back to Plan.
    assert "var previewToken = 0;" in text
    assert "var token = ++previewToken;" in text
    assert "if (token !== previewToken || !justRide()) return;" in text
    assert "var CONFIG_RECONNECT_DEBOUNCE_MS = 250;" in text
    assert "function scheduleConfigReconnect()" in text
    assert "if (positivePowerSeen || !ws || rideState === \"idle\") return;" in text
    assert "if (!positivePowerSeen && ws && rideState !== \"idle\") openRide(false);" in text
    assert "scheduleConfigReconnect();" in text
    assert "window.clearTimeout(configReconnectTimer);" in text
    assert "} else if (ws.readyState === WebSocket.CONNECTING) {" in text
    assert "// The replacement is already in queuedOpen." in text
    assert "queuedOpen = {sim: sim};" in text
    assert text.index("if (ws.readyState === WebSocket.OPEN)") < text.index(
        "} else if (ws.readyState === WebSocket.CONNECTING) {")
    assert text.index("} else if (ws.readyState === WebSocket.CONNECTING) {") < text.index(
        "} else if (ws.readyState === WebSocket.CLOSING) {")
    assert text.index("} else if (ws.readyState === WebSocket.CLOSING) {") < text.index(
        "} else if (ws.readyState === WebSocket.CLOSED) {")
    assert "// onclose owns the handoff for a closing socket." in text
    assert "var closedOpen = queuedOpen;" in text
    assert "queuedOpen = null;\n                ws = null;" in text
    assert "openRide(closedOpen.sim);" in text
    assert text.index("} else if (ws.readyState === WebSocket.CLOSED) {") < text.index(
        "} else {\n                cancelQueuedReconnect();")


def test_ride_page_grows_chart_axis_past_the_prescribed_end(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))
    r = client.get("/ride")
    assert r.status_code == 200
    assert "var chartMaxX = 0;" in r.text
    assert "chartMaxX = workoutDuration;" in r.text
    assert "function growChartAxis(elapsed)" in r.text
    assert "if (!rideChart || !(elapsed > chartMaxX)) return;" in r.text
    assert "chartMaxX = Math.ceil(elapsed / 60) * 60;" in r.text
    assert "if (chartMaxX <= elapsed) chartMaxX = elapsed + 60;" in r.text
    assert "rideChart.options.scales.x.max = chartMaxX;" in r.text
    assert "growChartAxis(elapsed);" in r.text
    assert r.text.index("growChartAxis(elapsed);") < r.text.index('rideChart.update("none");')


def test_ride_page_end_workout_button_reuses_the_stop_path(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))
    r = client.get("/ride")
    assert r.status_code == 200
    assert 'id="endWorkoutBtn"' in r.text
    assert 'class="button-secondary ride-chart-end"' in r.text
    assert "End workout now" in r.text
    # Real button, in the heading rather than the aria-hidden canvas overlays.
    assert r.text.index('id="endWorkoutBtn"') < r.text.index('class="ride-chart-canvas"')
    assert '<div class="ride-chart-actions">' in r.text
    # Disabled until a ride is active, exactly like #stopBtn.
    assert 'document.getElementById("endWorkoutBtn").disabled = !active;' in r.text
    # One stop code path, shared with #stopBtn.
    assert "function stopRide() {" in r.text
    # Both buttons share one guarded path: confirm, leave full screen, then stop.
    assert 'document.getElementById("stopBtn").addEventListener("click", requestEndRide);' in r.text
    assert ('document.getElementById("endWorkoutBtn").addEventListener('
            '"click", requestEndRide);') in r.text
    assert "End the ride now?" in r.text
    assert "The rest of the workout is discarded and the ride is saved." in r.text
    assert "stopRide();" in r.text


def test_ride_page_end_confirmation_is_in_page_not_a_browser_dialog(
    client, monkeypatch
):
    """Ending a ride must not depend on a dialog the browser can suppress.

    Both Stop buttons were gated on window.confirm(). Browsers suppress it
    routinely - most often once "Prevent this page from creating additional
    dialogs" has been ticked - and a suppressed confirm() returns false, which
    the handler could not tell from Cancel. It returned silently, leaving both
    buttons indistinguishable from dead with the ride still running.

    tests/test_dom_smoke.py drives this in a real browser; this one pins the
    wiring so a template edit cannot quietly put the browser dialog back.
    """
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))
    r = client.get("/ride")
    assert r.status_code == 200
    # The confirmation is part of the page, so nothing outside it can take it
    # away - and it offers both answers explicitly.
    assert '<dialog id="endRideDialog"' in r.text
    assert 'id="endRideConfirmBtn"' in r.text
    assert 'id="endRideCancelBtn"' in r.text
    assert "endRideDialog.showModal();" in r.text
    # No browser dialog stands between the rider and stopping.
    assert "if (!window.confirm(" not in r.text
    # window.confirm survives only as a fallback for browsers without <dialog>,
    # and even then it stops the ride rather than returning silently.
    assert 'typeof endRideDialog.showModal !== "function"' in r.text


def test_ride_page_shows_the_servers_erg_message_over_the_raw_error(client,
                                                                    monkeypatch):
    """An `erg` frame carrying both fields is one the server wrote prose for.

    The give-up frame sends the raw FTMS error *and* a rider-facing message;
    rendering only the error would repeat the mistake of computing a diagnostic
    no template ever shows.
    """
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))
    r = client.get("/ride")
    assert r.status_code == 200
    assert 'st.message || st.error || "Unable to change ERG mode."' in r.text


def test_ride_chart_end_button_styles(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert ".ride-chart-actions { display: flex;" in r.text
    assert ".ride-chart-heading .ride-chart-end { border-color: var(--alert); color: var(--alert); }" in r.text
    assert ".ride-chart-heading .ride-chart-end:disabled" in r.text


def test_ride_chart_styles_support_live_metrics_and_fullscreen(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert ".ride-chart-metrics strong" in r.text
    assert "font-size: 28px" in r.text
    assert ".ride-chart-metrics .metric-power { color: var(--s-2); }" in r.text
    assert ".ride-chart-metrics .metric-cadence { color: var(--s-3); }" in r.text
    assert ".ride-chart-metrics .metric-heart-rate { color: var(--s-hr); }" in r.text
    assert ".ride-chart-block:fullscreen" in r.text
    assert ".ride-chart-fullscreen-fallback" in r.text
    assert "body.ride-chart-fullscreen-open" in r.text
    assert ".ride-chart-clocks" in r.text
    # Keep the clocks readable in a constrained surface without creating a
    # broad high-priority layer over the chart traces.
    clocks = re.search(r"\.ride-chart-clocks \{(?P<rule>.*?)\n\}", r.text, re.S)
    assert clocks
    clock_rule = clocks.group("rule")
    assert "z-index: 2" in clock_rule
    assert "top: 72%" in clock_rule
    assert "background: color-mix(" in clock_rule
    assert "border: 1px solid" in clock_rule
    assert "width: fit-content" in clock_rule
    assert "max-width: 96%" in clock_rule
    assert "pointer-events: none; color: var(--text-bright);" in r.text
    assert "text-shadow: 0 1px 5px var(--surface-inset), 0 0 12px var(--surface-inset);" in r.text
    assert "letter-spacing: -0.02em; opacity: 0.82;" in r.text
    assert "text-transform: uppercase; opacity: 0.62;" in r.text
    assert ".ride-chart-erg.erg-lit .erg-led" in r.text
    # Fullscreen sizing follows the available panel space, including short
    # viewports, instead of subtracting a fixed heading height from 100vh, and
    # the flex column keeps the chart from being clipped below the fold.
    assert "height: clamp(260px, 28vw, 400px)" in r.text
    assert "display: flex; flex-direction: column; overflow: hidden;" in r.text
    assert "width: 100%; height: 100%; min-height: 0; max-width: none;" in r.text
    assert "flex: 1 1 auto; height: auto; min-height: 0;" in r.text
    assert "@media (max-height: 520px)" in r.text
    assert "top: 68%; gap: 0.35rem; width: fit-content; max-width: 98%;" in r.text
    assert "font-size: clamp(16px, 7vh, 42px);" in r.text
    assert "font-size: clamp(18px, 5vh, 32px);" in r.text
    assert ".ride-chart-clocks { gap: 0.2rem; padding: 0.2rem 0.3rem; max-width: 98%; }" in r.text
    assert ".ride-chart-clocks strong { font-size: clamp(12px, 5vw, 28px); }" in r.text
    assert ".ride-chart-heading { align-items: flex-start; flex-wrap: wrap; }" in r.text
    assert ".ride-chart-actions button { flex: 1 1 0; min-width: 0; white-space: normal; line-height: 1.2; }" in r.text
    assert "height: calc(100vh - 5.5rem)" not in r.text


def test_ride_chart_device_indicator_styles(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert ".ride-chart-devices { position: absolute; z-index: 2; right: 0.35rem;" in r.text
    assert "pointer-events: none" in r.text
    assert '.ride-chart-devices .device-chip[data-role="power"] { --device-color: var(--s-2);' in r.text
    assert '.ride-chart-devices .device-chip[data-role="hr"] { --device-color: var(--s-hr);' in r.text
    assert '.ride-chart-devices .device-chip[data-role="trainer"] { --device-color: var(--ok);' in r.text
    assert '.ride-chart-devices .device-chip[data-role="cadence"] { --device-color: var(--s-3);' in r.text
    assert ".ride-chart-devices .device-chip.device-lit" in r.text
    assert ".ride-chart-chip.erg-dark, .ride-chart-chip.device-dark" in r.text
    assert ".ride-chart-devices .device-chip[hidden] { display: none; }" in r.text


def test_ride_page_shows_sensor_indicators_on_the_chart(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))
    r = client.get("/ride")
    assert r.status_code == 200
    assert 'id="deviceIndicators"' in r.text
    for role in ("power", "hr", "trainer"):
        assert 'data-role="' + role + '"' in r.text
    assert 'data-role="cadence"' in r.text
    # The chips live inside the chart canvas, opposite the ERG chip.
    assert r.text.index('class="ride-chart-canvas"') < r.text.index('id="deviceIndicators"')
    assert r.text.index('id="deviceIndicators"') < r.text.index('id="rideChart"')
    # A role is only ever added to seenRoles, so a dropped sensor goes dark
    # rather than vanishing from the corner.
    assert "var seenRoles = {};" in r.text
    assert "function updateDeviceIndicators()" in r.text
    assert "updateDeviceIndicators();" in r.text
    assert "chip.hidden = !seenRoles[role];" in r.text
    assert 'chip.classList.toggle("device-lit", !!live[role]);' in r.text
    assert "seenRoles = {}" not in r.text.split("var seenRoles = {};", 1)[1]


def test_ride_status_endpoint(client, monkeypatch):
    _force_bt_unavailable(monkeypatch)
    _register(client)
    data = client.get("/ride/status").json()
    assert data["available"] is False
    assert "bleak" in data["reason"]


def test_ride_scan_unavailable(client, monkeypatch):
    _force_bt_unavailable(monkeypatch)
    _register(client)
    r = client.post("/ride/scan")
    data = r.json()
    assert data["available"] is False
    assert data["devices"] == []


def test_ride_scan_returns_role_and_rssi_contract(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))

    async def fake_scan():
        return [{
            "address": "OPAQUE-UUID",
            "name": "<unknown & untrusted>",
            "services": [],
            "roles": ["power"],
            "rssi": -55,
        }]

    monkeypatch.setattr(bledevices, "scan", fake_scan)
    data = client.post("/ride/scan").json()

    assert data["available"] is True
    assert data["devices"][0]["roles"] == ["power"]
    assert data["devices"][0]["rssi"] == -55


def test_ride_scan_empty_result_is_actionable_http_failure(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))

    async def fake_scan():
        return []

    monkeypatch.setattr(bledevices, "scan", fake_scan)
    response = client.post("/ride/scan")

    assert response.status_code == 404
    assert response.json()["available"] is True
    assert "Wake or spin" in response.json()["reason"]


def test_ride_requires_auth(client):
    assert client.get("/ride", follow_redirects=False).status_code == 303


def test_ride_ws_simulation_streams_and_saves(client):
    _register(client)
    frames = []
    with client.websocket_connect("/ride/ws?sim=1&type=endurance&minutes=30") as ws:
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass
    assert frames, "expected streamed frames"
    assert frames[-1]["status"] == "finished"
    assert any(f.get("target_watts", 0) > 0 for f in frames)
    # A ride activity was recorded for the user.
    uid = db.get_user_by_username("rider")["id"]
    assert len(db.list_activities(uid)) == 1


def test_ride_ws_selected_plan_workout_links_saved_activity(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.create_plan(uid, "Ride selection", "2026-07-10", 1)
    workout_id = db.add_plan_workout(
        plan_id,
        uid,
        "2026-07-10",
        "Selected endurance",
        "endurance",
        60,
        1.0,
        "<workout_file/>",
    )

    frames = []
    with client.websocket_connect(
        f"/ride/ws?sim=1&workout_id={workout_id}"
    ) as ws:
        first = ws.receive_json()
        assert first["status"] == "workout"
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass

    assert frames[-1]["status"] == "finished"
    assert frames[-1]["workout_id"] == workout_id
    assert frames[-1]["activity_id"] is not None
    linked = db.get_plan_workout(uid, workout_id)
    assert linked["completed_activity_id"] == frames[-1]["activity_id"]


def test_ride_ws_unauthenticated_closes(client):
    # No login -> WS should report an auth error and close, not crash.
    with client.websocket_connect("/ride/ws?sim=1") as ws:
        msg = ws.receive_json()
        assert msg["status"] == "error"


def test_ride_ws_unavailable_without_sim(client, monkeypatch):
    _force_bt_unavailable(monkeypatch)
    _register(client)
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        msg = _receive_after_workout(ws)
        assert msg["status"] == "unavailable"


# ------------------------------------------------ real-hardware path (mocked)
def _patch_real_ride(monkeypatch, trainer, power_script):
    from wattracker import server as servermod
    from wattracker.ble.devices import (
        SimulatedHeartRateSource,
        SimulatedPowerSource,
    )

    ps = SimulatedPowerSource(power_script)
    hr = SimulatedHeartRateSource(fixed=142)
    names = {"power": "FakePM"}
    if trainer is not None:
        names["trainer"] = "FakeKickr"

    async def fake_connect(timeout=6.0):
        return {
            "trainer": trainer, "power_source": ps, "hr_source": hr,
            "clients": [], "names": names,
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)


class _AckingFtmsClient:
    def __init__(self, results=None, delay=0.001):
        self.results = results or {}
        self.delay = delay
        self.callback = None
        self.active_procedure = False
        self.events = []

    async def start_notify(self, _char, callback):
        self.callback = callback
        self.events.append("notify")

    async def write_gatt_char(self, char, data, response=False):
        assert response is True
        assert self.active_procedure is False, "FTMS procedures overlapped"
        self.active_procedure = True
        op = data[0]
        self.events.append(("write", op))

        def acknowledge():
            self.active_procedure = False
            self.callback(
                char, bytearray([0x80, op, self.results.get(op, 0x01)])
            )

        asyncio.get_running_loop().call_later(self.delay, acknowledge)

    async def disconnect(self):
        assert self.active_procedure is False
        self.events.append("disconnect")


def test_ride_ws_real_path_erg_drives_trainer(client, monkeypatch):
    from wattracker.ble.devices import SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()
    # connect_sensors prepares a real FTMS trainer before handing it to the
    # route; represent that state so initial targeting must not re-request
    # control/start.
    trainer.start_erg()
    # Pedal through the 3s start gate plus 1s of ride time, then stop until the
    # shortened inactivity timeout finalizes.
    _patch_real_ride(monkeypatch, trainer, [150, 150, 150, 150] + [0] * 10)

    frames = []
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        first = _receive_after_workout(ws)
        assert first["status"] == "connected"
        assert first["erg"] is True
        assert first["devices"]["trainer"] == "FakeKickr"
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass
    assert frames and frames[-1]["status"] == "finished"
    assert any(f.get("status") == "inactivity_timeout" and f["saved"] for f in frames)
    # ERG lifecycle: Request Control + Start at ride start, Stop at the end,
    # with real workout targets in between and a zeroed target on finish.
    assert trainer.commands[:2] == ["request_control", "start"]
    assert trainer.commands.count("request_control") == 1
    assert trainer.commands[-1] == "stop"
    assert any(t > 0 for t in trainer.targets)
    assert trainer.targets[0] > 0  # prescribed target applied before pedal gate
    assert trainer.targets[-1] == 0
    # The ride was saved for the user.
    uid = db.get_user_by_username("rider")["id"]
    assert len(db.list_activities(uid)) == 1


def test_ride_ws_awaits_ftms_commands_and_stops_before_disconnect(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import BleakTrainer, SimulatedPowerSource

    _register(client)
    hardware = _AckingFtmsClient()
    connected_trainers = []
    clock_calls = 0
    requested_sleeps = []
    real_sleep = asyncio.sleep

    def fake_ride_time():
        nonlocal clock_calls
        tick = clock_calls // 2
        processing = 0.02 if clock_calls % 2 else 0.0
        clock_calls += 1
        return float(tick) + processing

    async def capture_ride_sleep(delay):
        requested_sleeps.append(delay)
        await real_sleep(0)

    async def fake_connect(timeout=6.0, selected=None):
        trainer = BleakTrainer(hardware, response_timeout_s=0.1)
        await trainer.prepare()
        connected_trainers.append(trainer)
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource(
                [150, 150, 150, 150] + [0] * 10
            ),
            "hr_source": None,
            "clients": [hardware],
            "clients_by_address": {"TRAINER": hardware},
            "bindings": {
                "TRAINER": {"name": "Kickr", "roles": {"trainer": trainer}}
            },
            "names": {"trainer": "Kickr", "power": "Pedals"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)
    monkeypatch.setattr(servermod, "_ride_loop_time", fake_ride_time)
    monkeypatch.setattr(servermod, "_ride_sleep", capture_ride_sleep)

    with client.websocket_connect("/ride/ws") as ws:
        assert _receive_after_workout(ws)["status"] == "connected"
        try:
            while True:
                ws.receive_json()
        except Exception:
            pass

    trainer = connected_trainers[0]
    stop_index = max(
        i for i, event in enumerate(hardware.events)
        if event == ("write", 0x08)
    )
    assert stop_index < hardware.events.index("disconnect")
    assert trainer._pending_response is None
    assert trainer._tasks == set()
    # Request Control and Start happened once during prepare; initial/per-tick
    # target procedures never queued another start sequence.
    assert hardware.events.count(("write", 0x00)) == 1
    assert hardware.events.count(("write", 0x07)) == 1
    # Deterministic clock models 20 ms of command/processing time per tick.
    # The server requests only the remaining 30 ms of its 50 ms cadence.
    assert requested_sleeps
    assert requested_sleeps == pytest.approx([0.03] * len(requested_sleeps))


def test_ride_ws_target_rejection_reports_erg_off_without_aborting(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import BleakTrainer, SimulatedPowerSource

    _register(client)
    hardware = _AckingFtmsClient(results={0x05: 0x04})
    connected_trainers = []

    async def fake_connect(timeout=6.0, selected=None):
        trainer = BleakTrainer(hardware, response_timeout_s=0.1)
        await trainer.prepare()
        connected_trainers.append(trainer)
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource(
                [150, 150, 150, 150] + [0] * 10
            ),
            "hr_source": None,
            "clients": [hardware],
            "clients_by_address": {"TRAINER": hardware},
            "bindings": {
                "TRAINER": {"name": "Kickr", "roles": {"trainer": trainer}}
            },
            "names": {"trainer": "Kickr", "power": "Pedals"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)

    frames = []
    with client.websocket_connect("/ride/ws") as ws:
        assert _receive_after_workout(ws)["status"] == "connected"
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass

    failure = next(frame for frame in frames if frame.get("status") == "erg")
    assert failure["enabled"] is False
    assert "operation failed" in failure["error"]
    assert connected_trainers[0].erg_enabled is False
    assert any(frame.get("status") == "inactivity_timeout" for frame in frames)
    assert frames[-1]["status"] == "finished"
    assert hardware.events[-1] == "disconnect"


class _TransientFtmsClient(_AckingFtmsClient):
    """Rejects the first ``failures`` writes of ``op``, then acknowledges."""

    def __init__(self, op=0x05, failures=1, **kwargs):
        super().__init__(**kwargs)
        self.op = op
        self.failures = failures
        self.attempts = 0

    async def write_gatt_char(self, char, data, response=False):
        if data[0] == self.op:
            self.attempts += 1
            self.results = dict(self.results)
            # 0x01 is success, 0x04 "operation failed".
            self.results[self.op] = (
                0x04 if self.attempts <= self.failures else 0x01
            )
        await super().write_gatt_char(char, data, response=response)


def _run_erg_failure_ride(client, monkeypatch, hardware, powers):
    """Drive a real-path ride against ``hardware`` and return its frames."""
    from wattracker import server as servermod
    from wattracker.ble.devices import BleakTrainer, SimulatedPowerSource

    async def fake_connect(timeout=6.0, selected=None):
        trainer = BleakTrainer(hardware, response_timeout_s=0.1)
        await trainer.prepare()
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource(list(powers)),
            "hr_source": None,
            "clients": [hardware],
            "clients_by_address": {"TRAINER": hardware},
            "bindings": {
                "TRAINER": {"name": "Kickr", "roles": {"trainer": trainer}}
            },
            "names": {"trainer": "Kickr", "power": "Pedals"},
            "errors": [],
        }

    monkeypatch.setattr(
        servermod.bledevices, "bluetooth_available", lambda: (True, "ok")
    )
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)

    frames = []
    with client.websocket_connect("/ride/ws") as ws:
        assert _receive_after_workout(ws)["status"] == "connected"
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass
    return frames


def test_ride_ws_transient_erg_failure_does_not_disable_erg_for_the_ride(
    client, monkeypatch
):
    """One failed ERG command must not latch ERG off for the rest of the ride.

    The per-tick ERG block is gated on ``controller.erg_enabled`` and the only
    line that could set it back True lives *inside* that block, so mirroring a
    single command failure into it used to be permanent: no retry, no re-arm,
    and no target sent again until the rider knew to toggle ERG by hand. A
    dropped characteristic write is not evidence that the trainer refuses
    targets, so it is retried instead.
    """
    _register(client)
    # Only the very first set-target-power fails - the one the ride issues while
    # arming. Everything after it is acknowledged normally.
    hardware = _TransientFtmsClient(op=0x05, failures=1)
    frames = _run_erg_failure_ride(
        client, monkeypatch, hardware, [150] * 8 + [0] * 10
    )

    # It retried rather than giving up on the first failure: one target write
    # per running tick, not a single failed one and then silence.
    assert hardware.attempts > 1
    assert hardware.events.count(("write", 0x05)) >= 5
    # ERG stayed on for every running tick, and the rider was never told it had
    # been switched off. (The frames after the ride finishes report False,
    # because _finish() clears the flag on the way out - that is the ride
    # ending, not the latch.)
    running = [
        f for f in frames if f.get("status") == "running" and "erg_enabled" in f
    ]
    assert running, "expected per-tick state frames while running"
    assert all(f["erg_enabled"] is True for f in running)
    assert not any(
        "switched off" in (f.get("message") or "")
        for f in frames
        if f.get("status") == "erg"
    )
    assert frames[-1]["status"] == "finished"


def test_ride_ws_sustained_erg_failure_disables_erg_and_says_so(
    client, monkeypatch
):
    """A trainer that keeps refusing targets does get ERG switched off - once
    the failures are sustained, and with a message rather than in silence."""
    from wattracker import server as servermod

    _register(client)
    monkeypatch.setattr(servermod, "ERG_COMMAND_FAILURE_LIMIT", 3)
    # Every set-target-power is rejected, for the whole ride.
    hardware = _TransientFtmsClient(op=0x05, failures=10_000)
    frames = _run_erg_failure_ride(
        client, monkeypatch, hardware, [150] * 8 + [0] * 10
    )

    # Each retry re-arms (Request Control + Start + target), so the 0x00 writes
    # count the ride loop's ERG retries exactly - unlike the raw 0x05 count,
    # which also picks up the best-effort target-0 of the teardown. One 0x00
    # from the connect-time prepare, then exactly two retries before the limit
    # is reached: it retried rather than latching off on the first failure, and
    # it stopped rather than hammering a trainer that will not take targets.
    assert hardware.events.count(("write", 0x00)) == 3
    giveup = next(
        f
        for f in frames
        if f.get("status") == "erg" and "switched off" in (f.get("message") or "")
    )
    assert giveup["enabled"] is False
    assert "operation failed" in giveup["error"]
    assert "Re-enable it to try again" in giveup["message"]
    # And having given up, ERG reads as off for the rest of the ride.
    running = [
        f for f in frames if f.get("status") == "running" and "erg_enabled" in f
    ]
    assert running and running[-1]["erg_enabled"] is False
    assert frames[-1]["status"] == "finished"


def test_ride_ws_real_path_degrades_without_trainer(client, monkeypatch):
    _register(client)
    _patch_real_ride(monkeypatch, None, [150, 150, 150, 150] + [0] * 10)

    frames = []
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        first = _receive_after_workout(ws)
        assert first["status"] == "connected"
        assert first["erg"] is False  # no FTMS trainer: read-only ride
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass
    # Power display still works without a controllable trainer.
    assert any(f["power"] == 150 for f in frames)
    assert frames[-1]["status"] == "finished"


def test_ride_ws_real_path_no_devices(client, monkeypatch):
    from wattracker import server as servermod

    _register(client)

    async def fake_connect(timeout=6.0):
        return {"trainer": None, "power_source": None, "hr_source": None,
                "clients": [], "names": {}}

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        msg = _receive_after_workout(ws)
        assert msg["status"] == "error"
        assert "No power meter" in msg["error"]


def test_ride_ws_propagates_exact_explicit_sensor_selection(client, monkeypatch):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource

    _register(client)
    captured = []

    async def fake_connect(timeout=6.0, selected=None):
        captured.append(selected)
        return {
            "trainer": None,
            "power_source": SimulatedPowerSource([0, 0, 0, 0, 0, 0]),
            "hr_source": None,
            "clients": [],
            "names": {"power": ["LEFT", "RIGHT"]},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)

    url = (
        "/ride/ws?selected=1&power=LEFT-UUID&power=RIGHT-UUID"
        "&power=LEFT-UUID&hr=HR-UUID&trainer=TRAINER-UUID&cadence=CADENCE-UUID"
    )
    with client.websocket_connect(url) as ws:
        assert _receive_after_workout(ws)["status"] == "connected"

    assert captured == [{
        "power": ["LEFT-UUID", "RIGHT-UUID"],
        "hr": ["HR-UUID"],
        "trainer": ["TRAINER-UUID"],
        "cadence": ["CADENCE-UUID"],
    }]


def test_ride_ws_rejects_unbounded_power_selection(client, monkeypatch):
    from wattracker import server as servermod

    _register(client)
    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    query = "&".join("power=P" + str(i) for i in range(9))
    with client.websocket_connect("/ride/ws?selected=1&" + query) as ws:
        msg = ws.receive_json()
    assert msg["status"] == "error"
    assert "at most 8 power sensors" in msg["error"]


def test_ride_ws_prepare_streams_and_auto_starts_on_power(client, monkeypatch):
    # The "Connect selected sensors" (prepare=1) button now behaves identically
    # to a fresh connect: it streams telemetry immediately and the controller's
    # power-gated start begins the workout clock once the rider pedals. No
    # explicit "start" action is required.
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource, SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource([150, 150, 150, 150, 0, 0, 0, 0]),
            "hr_source": None,
            "clients": [],
            "names": {"power": "Pedals", "trainer": "Trainer"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)

    uid = db.get_user_by_username("rider")["id"]
    frames = []
    with client.websocket_connect("/ride/ws?prepare=1") as ws:
        connected = _receive_after_workout(ws)
        assert connected["status"] == "connected"
        # Both connect buttons stream immediately; nothing is "prepared" anymore.
        assert connected["prepared"] is False
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass

    # Auto-started on power (3s gate) without any explicit start action.
    assert any(f.get("status") == "running" for f in frames)
    assert frames[-1]["status"] == "finished"
    assert len(db.list_activities(uid)) == 1


def test_ride_ws_prepared_stop_cleans_hardware_before_server_close(
    client, monkeypatch
):
    from starlette.datastructures import QueryParams
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    events = []

    class AwaitedTrainer:
        erg_available = True
        erg_enabled = True

        async def async_set_target_power(self, watts):
            events.append(("target", watts))

        async def async_stop(self):
            events.append("stop")
            self.erg_enabled = False

    class AwaitedClient:
        async def disconnect(self):
            events.append("disconnect")

    trainer = AwaitedTrainer()
    ble_client = AwaitedClient()

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource([0]),
            "hr_source": None,
            "clients": [ble_client],
            "clients_by_address": {"TRAINER": ble_client},
            "bindings": {
                "TRAINER": {"name": "Kickr", "roles": {"trainer": trainer}}
            },
            "names": {"trainer": "Kickr", "power": "Pedals"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    # Zero poll interval: an idle prepared ride otherwise sleeps 1s per tick.
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.0)

    class FakeWebSocket:
        headers = {}
        session = {"user_id": uid}
        query_params = QueryParams("prepare=1")

        def __init__(self):
            self.receive_count = 0
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, message):
            self.messages.append(message)

        async def receive_json(self):
            self.receive_count += 1
            if self.receive_count == 1:
                return {"action": "stop"}
            await asyncio.Future()

        async def close(self, code=None):
            events.append("close")

    endpoint = next(
        route.endpoint
        for route in client.app.routes
        if getattr(route, "path", None) == "/ride/ws"
    )
    websocket = FakeWebSocket()
    asyncio.run(endpoint(websocket))

    assert websocket.messages[0]["status"] == "workout"
    assert websocket.messages[1]["status"] == "connected"
    # A stop before pedaling still releases ERG (target 0 + stop) and cleans the
    # hardware in order before the server closes; no activity is saved.
    assert events[-3:] == ["stop", "disconnect", "close"]
    assert ("target", 0) in events
    assert db.list_activities(uid) == []


def test_ride_ws_prepared_actions_toggle_erg_and_disconnect_one_device(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import (
        AggregatePowerSource,
        SimulatedPowerSource,
        SimulatedTrainer,
    )

    _register(client)
    trainer = SimulatedTrainer()
    trainer.start_erg()
    # Zero power keeps the controller idle so its live state frames don't drown
    # the action responses we assert on (both connect buttons now stream).
    left = SimulatedPowerSource([0])
    right = SimulatedPowerSource([0])

    def _next(ws, status):
        message = ws.receive_json()
        while message.get("status") != status:
            message = ws.receive_json()
        return message

    class FakeClient:
        def __init__(self, address):
            self.address = address
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    left_client = FakeClient("LEFT")
    right_client = FakeClient("RIGHT")
    trainer_client = FakeClient("TRAINER")
    conn = {
        "trainer": trainer,
        "power_source": AggregatePowerSource([left, right]),
        "hr_source": None,
        "clients": [left_client, right_client, trainer_client],
        "clients_by_address": {
            "LEFT": left_client, "RIGHT": right_client, "TRAINER": trainer_client,
        },
        "bindings": {
            "LEFT": {"name": "Left", "roles": {"power": left}},
            "RIGHT": {"name": "Right", "roles": {"power": right}},
            "TRAINER": {"name": "Kickr", "roles": {"trainer": trainer}},
        },
        "names": {"power": ["Left", "Right"], "trainer": "Kickr"},
        "errors": [],
    }

    async def fake_connect(timeout=6.0, selected=None):
        return conn

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    # Not 0: this test drives the ride loop by sending actions into it, and a
    # zero interval makes _ride_sleep(0) a bare yield with no wall-clock delay.
    # The loop then spins through the whole 300-second inactivity budget - power
    # is pinned at 0, so every iteration adds a simulated second for free - in
    # the few milliseconds before the client's first action can be handed across
    # the TestClient's thread boundary into the action queue. The server closes
    # on the inactivity timeout before it ever sees an action. A real delay
    # keeps that budget in wall-clock terms and the loop still runs fast enough
    # for the test to be quick.
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.01)

    with client.websocket_connect("/ride/ws?prepare=1") as ws:
        connected = _receive_after_workout(ws)
        assert connected["erg_available"] is True
        assert connected["erg_enabled"] is True

        ws.send_json({"action": "set_erg", "enabled": "false"})
        invalid = _next(ws, "erg")
        assert invalid == {
            "status": "erg",
            "available": True,
            "enabled": True,
            "error": "ERG enabled must be a boolean.",
        }

        ws.send_json({"action": "set_erg", "enabled": False})
        disabled = _next(ws, "erg")
        assert disabled["available"] is True
        assert disabled["enabled"] is False
        assert disabled["error"] is None

        ws.send_json({"action": "disconnect", "address": "LEFT"})
        disconnected = _next(ws, "device_disconnected")
        assert disconnected["address"] == "LEFT"
        assert disconnected["devices"]["power"] == "Right"
        assert disconnected["erg_available"] is True
        assert left_client.disconnected is True

    assert right_client.disconnected is True
    assert trainer_client.disconnected is True


def test_ride_ws_last_ride_device_disconnect_ends_session_and_releases_clients(
    client, monkeypatch
):
    from starlette.datastructures import QueryParams
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedHeartRateSource, SimulatedPowerSource

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    events = []

    class FakeClient:
        def __init__(self, address):
            self.address = address

        async def disconnect(self):
            events.append("disconnect:" + self.address)

    power = SimulatedPowerSource([0])
    heart = SimulatedHeartRateSource()
    power_client = FakeClient("POWER")
    heart_client = FakeClient("HR")
    conn = {
        "trainer": None,
        "power_source": power,
        "hr_source": heart,
        "clients": [power_client, heart_client],
        "clients_by_address": {"POWER": power_client, "HR": heart_client},
        "bindings": {
            "POWER": {"name": "Pedals", "roles": {"power": power}},
            "HR": {"name": "Heart", "roles": {"hr": heart}},
        },
        "names": {"power": "Pedals", "hr": "Heart"},
        "errors": [],
    }

    async def fake_connect(timeout=6.0, selected=None):
        return conn

    monkeypatch.setattr(
        servermod.bledevices, "bluetooth_available", lambda: (True, "ok")
    )
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    # Zero poll interval: an idle prepared ride otherwise sleeps 1s per tick.
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.0)

    class FakeWebSocket:
        headers = {}
        session = {"user_id": uid}
        query_params = QueryParams("prepare=1")

        def __init__(self):
            self.messages = []
            self.received = 0

        async def accept(self):
            pass

        async def send_json(self, message):
            self.messages.append(message)

        async def receive_json(self):
            self.received += 1
            if self.received == 1:
                return {"action": "disconnect", "address": "POWER"}
            await asyncio.Future()

        async def close(self, code=None):
            events.append("close")

    endpoint = next(
        route.endpoint
        for route in client.app.routes
        if getattr(route, "path", None) == "/ride/ws"
    )
    websocket = FakeWebSocket()
    asyncio.run(endpoint(websocket))

    disconnected = next(
        message
        for message in websocket.messages
        if message.get("status") == "device_disconnected"
    )
    assert disconnected["ending_session"] is True
    assert disconnected["devices"] == {"hr": "Heart"}
    assert "Releasing Bluetooth" in disconnected["message"]
    assert events == ["disconnect:POWER", "disconnect:HR", "close"]
    assert db.list_activities(uid) == []


def test_ride_ws_active_actions_toggle_erg_and_validate_disconnect(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource, SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource([150] * 100),
            "hr_source": None,
            "clients": [],
            "clients_by_address": {},
            "bindings": {},
            "names": {"power": "Pedals", "trainer": "Kickr"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.001)

    with client.websocket_connect("/ride/ws") as ws:
        assert _receive_after_workout(ws)["status"] == "connected"
        ws.send_json({"action": "set_erg", "enabled": False})
        messages = []
        while not any(message.get("status") == "erg" for message in messages):
            messages.append(ws.receive_json())
        result = next(message for message in messages if message.get("status") == "erg")
        assert result["enabled"] is False

        ws.send_json({"action": "disconnect", "address": 123})
        messages = []
        while not any(
            message.get("status") == "error"
            and message.get("action") == "disconnect"
            for message in messages
        ):
            messages.append(ws.receive_json())
        error = next(
            message for message in messages
            if message.get("status") == "error"
            and message.get("action") == "disconnect"
        )
        assert error["error"] == "Invalid device address."


def test_ride_ws_erg_action_reports_unavailable_without_trainer(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource

    _register(client)

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": None,
            "power_source": SimulatedPowerSource([0]),
            "hr_source": None,
            "clients": [],
            "clients_by_address": {},
            "bindings": {},
            "names": {"power": "Pedals"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    # Zero poll interval: an idle prepared ride otherwise sleeps 1s per tick.
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.0)

    with client.websocket_connect("/ride/ws?prepare=1") as ws:
        connected = _receive_after_workout(ws)
        assert connected["erg_available"] is False
        assert connected["erg_enabled"] is False
        ws.send_json({"action": "set_erg", "enabled": True})
        response = ws.receive_json()
        while response.get("status") != "erg":
            response = ws.receive_json()
        assert response["available"] is False
        assert response["enabled"] is False
        assert "No controllable FTMS trainer" in response["error"]
        ws.send_json({"action": "stop"})


@pytest.mark.parametrize("prepare", [True, False])
def test_ride_ws_close_before_pedaling_does_not_save_activity(
    client, monkeypatch, prepare
):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource, SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()

    class FakeClient:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    ble_client = FakeClient()

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource([0]),
            "hr_source": None,
            "clients": [ble_client],
            "names": {"power": "Pedals", "trainer": "Trainer"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)
    url = "/ride/ws?prepare=1" if prepare else "/ride/ws"

    with client.websocket_connect(url) as ws:
        connected = _receive_after_workout(ws)
        assert connected["status"] == "connected"
        # Both connect buttons stream now; the prepared/blocking path is gone.
        assert connected["prepared"] is False

    uid = db.get_user_by_username("rider")["id"]
    assert db.list_activities(uid) == []
    assert trainer.commands[-1] == "stop"
    assert ble_client.disconnected is True


@pytest.mark.parametrize("prepare", [True, False])
def test_ride_ws_inactivity_disconnects_without_saving_never_started_ride(
    client, monkeypatch, prepare
):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource, SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()

    class FakeClient:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    ble_client = FakeClient()

    async def fake_connect(timeout=6.0, selected=None):
        return {"trainer": trainer, "power_source": SimulatedPowerSource([0]),
                "hr_source": None, "clients": [ble_client],
                "names": {"power": "Pedals"}, "errors": []}

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 0.01 if prepare else 2)

    with client.websocket_connect("/ride/ws" + ("?prepare=1" if prepare else "")) as ws:
        assert _receive_after_workout(ws)["status"] == "connected"
        timeout = ws.receive_json()
        while timeout["status"] != "inactivity_timeout":
            timeout = ws.receive_json()

    assert timeout["status"] == "inactivity_timeout"
    assert timeout["saved"] is False
    assert "No activity was saved" in timeout["message"]
    uid = db.get_user_by_username("rider")["id"]
    assert db.list_activities(uid) == []
    assert trainer.commands[-1] == "stop"
    assert ble_client.disconnected is True


def test_ride_ws_base_exception_finalizes_active_ride_and_cleans_every_client(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedTrainer
    from starlette.datastructures import QueryParams

    _register(client)

    class RideCancelled(BaseException):
        pass

    class CleanupCancelled(BaseException):
        pass

    class CancellingPower:
        calls = 0

        def advance(self):
            self.calls += 1
            if self.calls == 5:
                raise RideCancelled("ride task cancelled")

        def latest_power(self):
            return 150

        def latest_cadence(self):
            return 90

    disconnects = []

    class FakeClient:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        async def disconnect(self):
            disconnects.append(self.name)
            if self.fail:
                raise CleanupCancelled("cleanup cancelled")

    trainer = SimulatedTrainer()

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": trainer,
            "power_source": CancellingPower(),
            "hr_source": None,
            "clients": [FakeClient("first", fail=True), FakeClient("second")],
            "names": {"power": "Pedals", "trainer": "Trainer"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)

    uid = db.get_user_by_username("rider")["id"]

    class FakeWebSocket:
        headers = {}
        session = {"user_id": uid}
        query_params = QueryParams("")

        def __init__(self):
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, message):
            self.messages.append(message)

        async def close(self, code=None):
            pass

    endpoint = next(
        route.endpoint
        for route in client.app.routes
        if getattr(route, "path", None) == "/ride/ws"
    )
    websocket = FakeWebSocket()
    with pytest.raises(RideCancelled):
        asyncio.run(endpoint(websocket))

    assert websocket.messages[0]["status"] == "workout"
    assert websocket.messages[1]["status"] == "connected"
    assert websocket.messages[-1]["status"] == "running"
    assert len(db.list_activities(uid)) == 1
    assert "stop" in trainer.commands
    assert disconnects == ["first", "second"]


def test_ride_ws_close_during_start_countdown_never_saves_activity(
    client, monkeypatch
):
    from starlette.datastructures import QueryParams
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource, SimulatedTrainer

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    trainer = SimulatedTrainer()

    class FakeClient:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    ble_client = FakeClient()

    async def fake_connect(timeout=6.0, selected=None):
        return {"trainer": trainer, "power_source": SimulatedPowerSource([150]),
                "hr_source": None, "clients": [ble_client],
                "names": {"power": "Pedals"}, "errors": []}

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)

    class FakeWebSocket:
        headers = {}
        session = {"user_id": uid}
        query_params = QueryParams("")

        def __init__(self):
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, message):
            self.messages.append(message)
            if sum(item.get("status") == "starting" for item in self.messages) == 2:
                raise RuntimeError("client closed")

        async def close(self, code=None):
            pass

    endpoint = next(
        route.endpoint for route in client.app.routes
        if getattr(route, "path", None) == "/ride/ws"
    )
    websocket = FakeWebSocket()
    asyncio.run(endpoint(websocket))

    assert [message["status"] for message in websocket.messages[-2:]] == [
        "starting", "starting"
    ]
    assert db.list_activities(uid) == []
    assert trainer.commands[-1] == "stop"
    assert ble_client.disconnected is True


def test_ride_ws_keeps_streaming_after_the_workout_then_finishes_on_stop_pedalling(
    client, monkeypatch
):
    """The prescribed workout ending starts a cooldown, not a finish: telemetry
    keeps flowing (and ERG keeps its target) until the rider stops pedalling."""
    from wattracker.ble.devices import SimulatedTrainer

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.create_plan(uid, "Short ride", "2026-07-10", 1)
    workout_id = db.add_plan_workout(
        plan_id, uid, "2026-07-10", "One minute", "endurance", 60, 1.0, "<x/>"
    )
    trainer = SimulatedTrainer()
    trainer.start_erg()
    # 3s start gate, then long enough to finish the 60s workout and spin down.
    _patch_real_ride(monkeypatch, trainer, [150] * 90 + [0] * 5)

    frames = []
    with client.websocket_connect(f"/ride/ws?workout_id={workout_id}") as ws:
        assert _receive_after_workout(ws)["status"] == "connected"
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass

    states = [f for f in frames if "status" in f and "elapsed" in f]
    cooldown = [f for f in states if f["status"] == "cooldown"]
    assert cooldown, "expected a cooldown phase past the prescribed end"
    total = cooldown[0]["total"]
    assert cooldown[0]["elapsed"] >= total
    # Readings past the end keep streaming, charting past the workout duration.
    assert cooldown[-1]["elapsed"] > total
    assert all(f["progress"] == 1.0 for f in cooldown)
    # ERG kept its (final prescribed) target during the cooldown, and the ride
    # ended only once power read zero for the grace period.
    assert all(f["target_watts"] > 0 for f in cooldown)
    assert cooldown[-1]["finish_countdown"] > 0
    assert states[-1]["status"] == "finished"
    assert states[-1]["elapsed"] > total
    assert trainer.targets[-1] == 0
    # The server keeps commanding the trainer through the cooldown, so it does
    # not drop out of ERG: more positive writes than prescribed workout seconds.
    assert len([t for t in trainer.targets if t > 0]) >= total + 20
    acts = db.list_activities(uid)
    assert len(acts) == 1
    assert acts[0]["duration_s"] > total  # the spin-down is part of the ride


def test_ride_page_treats_the_cooldown_as_an_active_ride(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))
    r = client.get("/ride")
    assert r.status_code == 200
    assert ('["starting", "running", "paused", "cooldown"].indexOf(st.status) !== -1'
            in r.text)
    assert "function statusText(st)" in r.text
    assert 'if (st.status === "cooldown") {' in r.text
    assert '"cooldown — stop pedalling to finish"' in r.text
    assert '"cooldown — finishing in " + left + "s"' in r.text
    assert 'document.getElementById("rStatus").textContent = statusText(st);' in r.text


