"""Real-browser (DOM-level) smoke tests.

The rest of the suite checks JS by grepping the *source* of the .js files for
substrings. That cannot see a chart that throws on render, an SVG that comes
out empty, or a modal that never opens -- three bugs of exactly that shape
shipped past a green suite. These tests drive the actual app in Chromium and
assert on rendered geometry plus a clean JS console.

Requires the optional `playwright` package AND its chromium binary
(`playwright install chromium`, a ~150MB download outside pip). Both are
checked below; when either is missing the whole module SKIPS, so CI and
machines without the browser still go green.

Run just these:      pytest -m browser
Skip them:           pytest -m "not browser"
"""
import datetime as dt
import json
import re
import socket
import threading
import time

import pytest

pytest.importorskip("httpx")
playwright_api = pytest.importorskip("playwright.sync_api")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.prescribe.planner import VARIANTS, WORKOUT_TYPE_KEYS  # noqa: E402
from wattracker.server import create_app  # noqa: E402

pytestmark = pytest.mark.browser

sync_playwright = playwright_api.sync_playwright

PASSWORD = "password123"
USERNAME = "domtester"

# Top-level nav destinations (matches the <nav> in web/templates/base.html).
NAV_PAGES = [
    "/",
    "/activities",
    "/volume",
    "/plan",
    "/calendar",
    "/races",
    "/profile",
    "/settings",
]


# --------------------------------------------------------------- browser
@pytest.fixture()
def browser():
    """A Chromium for one test. Skips (never errors) if the binary is absent.

    Deliberately function-scoped, not session-scoped: playwright's sync API
    keeps an asyncio loop marked *running* in the main thread for as long as
    its context manager is open, which makes every later `asyncio.run()` in
    the suite (tests/test_ride_routes.py) blow up with "cannot be called from
    a running event loop". Entering and exiting per test keeps the leak
    contained; a launch costs a few hundred ms.
    """
    with sync_playwright() as pw:
        try:
            launched = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure = skip
            pytest.skip(f"chromium unavailable for playwright: {exc}")
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture()
def console_errors(page):
    """Everything the browser complained about while the test ran."""
    return page._wt_errors


@pytest.fixture()
def page(browser, live_server):
    """A logged-in page that records console errors and uncaught exceptions."""
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    pg = context.new_page()
    errors = []
    pg._wt_errors = errors
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
          if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("requestfailed",
          lambda r: errors.append(f"requestfailed: {r.url} {r.failure}"))

    pg.goto(f"{live_server}/login")
    pg.fill("input[name=username]", USERNAME)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")
    assert "/login" not in pg.url, "login through the real form failed"
    # Login lands on the dashboard; don't let its noise be attributed to
    # whatever page the test itself visits.
    del errors[:]
    try:
        yield pg
    finally:
        context.close()


# ----------------------------------------------------------- app + data
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed_activities(uid, today):
    """A few weeks of rides so the dashboard/volume charts have real series."""
    for i in range(1, 25):
        day = today - dt.timedelta(days=i)
        watts = 180.0 + (i % 7) * 15
        secs = 1800 + (i % 5) * 300
        db.insert_activity(
            uid,
            {
                "dedup_hash": f"dom-{i}",
                "filename": f"dom-{i}.fit",
                "start_time": f"{day.isoformat()}T09:00:00",
                "duration_s": secs,
                "distance_m": secs * 8.0,
                "avg_power": watts,
                "avg_hr": 140.0,
                "np": watts + 10,
                "if_": 0.75,
                "tss": 60.0 + (i % 4) * 10,
                "calories": secs // 4,
                "streams": {"power": [watts] * secs},
            },
        )


@pytest.fixture()
def live_server(tmp_path):
    """The real FastAPI app on uvicorn, background thread, ephemeral port.

    Seeded through the app's own HTTP endpoints wherever one exists, so the
    fixture exercises the same paths a user would.
    """
    db.init_db()
    app = create_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)

    today = dt.date.today()
    with httpx.Client(base_url=base, follow_redirects=True, timeout=30) as c:
        r = c.post("/register", data={"username": USERNAME, "password": PASSWORD})
        assert r.status_code == 200, r.text
        uid = db.get_user_by_username(USERNAME)["id"]
        _seed_activities(uid, today)
        detail_activity_id = db.insert_activity(
            uid,
            {
                "dedup_hash": "dom-detail-series",
                "filename": "dom-detail-series.fit",
                "start_time": f"{today.isoformat()}T12:00:00",
                "duration_s": 120,
                "distance_m": 1000.0,
                "avg_power": 205.0,
                "avg_hr": 145.0,
                "np": 210.0,
                "if_": 0.8,
                "tss": 10.0,
                "streams": {
                    "power": [200.0 + i % 20 for i in range(120)],
                    "heartrate": [140.0 + i % 10 for i in range(120)],
                    "cadence": [85.0 + i % 8 for i in range(120)],
                    "altitude": [100.0 + i % 15 for i in range(120)],
                },
            },
        )
        # Anchor the plan inside the currently displayed calendar month: the
        # Monday on/before the 8th always falls in this month.
        start = dt.date(today.year, today.month, 8)
        r = c.post(
            "/generate/plan",
            data={
                "name": "DOM Plan",
                "weeks": "4",
                "hours_per_week": "8",
                "hit_days_per_week": "2",
                "start_date": start.isoformat(),
                "days": ["0", "2", "4", "5"],
            },
        )
        assert r.status_code == 200, r.text
    plan_id = db.list_plans(uid)[0]["id"]

    try:
        yield type("Server", (), {"__str__": lambda self: base,
                                  "base": base, "plan_id": plan_id,
                                  "uid": uid,
                                  "detail_activity_id": detail_activity_id})()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _assert_clean(errors, where):
    assert not errors, f"JS errors on {where}:\n" + "\n".join(errors)


def _svg_has_geometry(page, selector):
    """An <svg></svg> with no drawn geometry must NOT pass."""
    return page.evaluate(
        """(sel) => {
            const svg = document.querySelector(sel);
            if (!svg) return {ok: false, why: 'no svg matched ' + sel};
            const paths = svg.querySelectorAll('path');
            const lines = svg.querySelectorAll('line');
            let dLen = 0;
            paths.forEach(p => { dLen = Math.max(dLen, (p.getAttribute('d') || '').length); });
            const box = svg.getBoundingClientRect();
            return {
                ok: paths.length > 0 && dLen > 20 && lines.length > 0
                    && box.width > 10 && box.height > 10,
                paths: paths.length, lines: lines.length,
                dLen: dLen, w: box.width, h: box.height,
            };
        }""",
        selector,
    )


def _wait_for_charts(page, *canvas_ids):
    """Block until Chart.js has instantiated and painted each canvas."""
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        """(ids) => ids.every(id => {
            const c = document.getElementById(id);
            return c && window.Chart && window.Chart.getChart(c);
        })""",
        arg=list(canvas_ids),
        timeout=10_000,
    )
    # One frame past instantiation so the first paint has landed.
    page.wait_for_timeout(150)


def _canvas_is_painted(page, canvas_id):
    """True only if the canvas has real, non-blank pixels drawn on it."""
    return page.evaluate(
        """(id) => {
            const c = document.getElementById(id);
            if (!c) return {ok: false, why: 'no canvas #' + id};
            const box = c.getBoundingClientRect();
            const ctx = c.getContext('2d');
            const px = ctx.getImageData(0, 0, c.width, c.height).data;
            let painted = 0;
            for (let i = 3; i < px.length; i += 4) if (px[i] > 0) painted++;
            const chart = window.Chart && window.Chart.getChart(c);
            return {
                ok: box.width > 50 && box.height > 20 && painted > 500
                    && !!chart && chart.data.datasets.length > 0,
                painted: painted, w: box.width, h: box.height,
                datasets: chart ? chart.data.datasets.length : null,
            };
        }""",
        canvas_id,
    )


# ------------------------------------------------------------------ tests
def test_activity_detail_combines_and_independently_toggles_series(
        page, live_server, console_errors):
    page.goto(f"{live_server.base}/activity/{live_server.detail_activity_id}")
    _wait_for_charts(page, "detailChart")

    before = page.evaluate(
        """() => {
            const chart = Chart.getChart(document.getElementById("detailChart"));
            return {
                labels: chart.data.datasets.map(dataset => dataset.label),
                visible: chart.data.datasets.map((_, i) => chart.isDatasetVisible(i)),
            };
        }"""
    )
    assert before["labels"] == [
        "Elevation (m)", "Power (W)", "Heart rate (bpm)", "Cadence (rpm)",
    ]
    assert before["visible"] == [True, True, True, True]

    page.get_by_text("Heart rate (bpm)", exact=True).click()
    after = page.evaluate(
        """() => {
            const chart = Chart.getChart(document.getElementById("detailChart"));
            return chart.data.datasets.map((_, i) => chart.isDatasetVisible(i));
        }"""
    )
    assert after == [True, True, False, True]
    _assert_clean(console_errors, "/activity/{id}")


def test_calendar_workout_click_opens_modal_with_drawn_profile(page, live_server,
                                                               console_errors):
    """Calendar renders clickable workout cells; the modal draws a real SVG."""
    page.goto(f"{live_server.base}/calendar")
    page.wait_for_load_state("networkidle")

    cells = page.locator(".cal-workout[data-workout-id]")
    assert cells.count() > 0, "calendar rendered no clickable workout cells"

    cells.first.click()
    page.wait_for_selector("#workoutModal:not([hidden])", timeout=10_000)
    page.wait_for_selector("#wmProfile svg.profile-svg", timeout=10_000)

    assert page.locator("#wmError").is_hidden(), page.locator("#wmError").inner_text()
    assert page.locator("#wmSegments tr").count() > 0, "modal listed no segments"

    geom = _svg_has_geometry(page, "#wmProfile svg.profile-svg")
    assert geom["ok"], f"modal power profile has no drawn geometry: {geom}"
    _assert_clean(console_errors, "/calendar + workout modal")


def test_just_ride_variant_cards_render_and_select(page, live_server,
                                                   console_errors):
    """Every Just Ride focus renders all of its accessible, drawn variants."""
    page.goto(f"{live_server.base}/ride")
    page.get_by_role("button", name="Just ride", exact=True).click()
    page.wait_for_selector("#rideVariantPicker:not([hidden])", timeout=10_000)

    # The reported failure was a 60-minute preview, so exercise that exact
    # duration across every focus rather than only the long-ride boundary.
    page.select_option("#rideDurationSelect", "60")
    for kind in WORKOUT_TYPE_KEYS:
        expected = VARIANTS[kind]
        page.select_option("#rideTypeSelect", kind)
        page.wait_for_function(
            """(expected) => {
                const grid = document.querySelector('#rideVariantGrid');
                if (!grid || grid.closest('[hidden]')) return false;
                const actual = Array.from(grid.querySelectorAll(
                    '.ride-variant-card')).map(card => card.dataset.variant);
                return JSON.stringify(actual) === JSON.stringify(expected);
            }""",
            arg=expected, timeout=10_000,
        )

        cards = page.locator("#rideVariantGrid .ride-variant-card")
        variants = [cards.nth(i).get_attribute("data-variant")
                    for i in range(cards.count())]
        assert variants == expected, f"{kind}: unexpected variant cards {variants}"
        assert len(set(variants)) == len(expected), f"{kind}: duplicate variants"
        assert page.locator("#rideVariantPicker").is_visible()
        assert page.locator("#rideVariantGrid").is_visible()

        states = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                '#rideVariantGrid .ride-variant-card')).map(card => {
                const svg = card.querySelector('svg.profile-svg');
                const paths = svg ? svg.querySelectorAll('path') : [];
                const lines = svg ? svg.querySelectorAll('line') : [];
                let dLen = 0;
                paths.forEach(path => {
                    dLen = Math.max(dLen, (path.getAttribute('d') || '').length);
                });
                const box = svg ? svg.getBoundingClientRect() : {width: 0, height: 0};
                return {
                    visible: !!(svg && card.getBoundingClientRect().width > 0 &&
                        svg.getBoundingClientRect().width > 0),
                    label: card.getAttribute('aria-label') || '',
                    content: (card.innerText || '').trim(),
                    geometry: !!(svg && paths.length && dLen > 20 && lines.length &&
                        box.width > 10 && box.height > 10),
                };
            })"""
        )
        assert all(state["visible"] for state in states), f"{kind}: hidden card"
        assert all(state["label"].strip() and state["content"]
                   for state in states), f"{kind}: unreadable card"
        assert all(state["geometry"] for state in states), f"{kind}: undrawn card"
        selected_cards = page.locator(
            '#rideVariantGrid .ride-variant-card[aria-selected="true"]'
        )
        assert selected_cards.count() == 1

        for index, variant in enumerate(variants):
            card = page.locator(
                f'#rideVariantGrid .ride-variant-card[data-variant="{variant}"]'
            )
            card.click()
            page.wait_for_selector(
                f'#rideVariantGrid .ride-variant-card[data-variant="{variant}"]'
                '[aria-selected="true"]', timeout=10_000,
            )
            next_variant = variants[(index + 1) % len(variants)]
            card.press("ArrowRight")
            page.wait_for_selector(
                f'#rideVariantGrid .ride-variant-card[data-variant="{next_variant}"]'
                '[aria-selected="true"]', timeout=10_000,
            )
    _assert_clean(console_errors, "/ride Just Ride variants")


def test_just_ride_incomplete_variant_payload_does_not_make_a_card(
    page, live_server, console_errors
):
    """A stale/partial preview never becomes one card of flattened payload text.

    A server predating the variant payload fields can still return a valid
    selected workout. The safe result is its normal preview with no shape
    picker, not a fabricated card whose text is the SVG's concatenated labels.
    """
    def strip_variant_fields(route):
        response = route.fetch()
        payload = response.json()
        payload.pop("variant_options", None)
        payload.pop("variant_profiles", None)
        route.fulfill(response=response, json=payload)

    page.route("**/ride/workout/preview*", strip_variant_fields)
    page.goto(f"{live_server.base}/ride")
    page.get_by_role("button", name="Just ride", exact=True).click()
    page.wait_for_selector("#ridePreview:not([hidden])", timeout=10_000)

    assert page.locator("#rideVariantPicker").is_hidden()
    assert page.locator("#rideVariantGrid .ride-variant-card").count() == 0
    geom = _svg_has_geometry(page, "#ridePreview svg.profile-svg")
    assert geom["ok"], f"selected preview lost its curve: {geom}"
    _assert_clean(console_errors, "/ride incomplete variant payload")


def test_dashboard_charts_render_without_console_errors(page, live_server,
                                                        console_errors):
    """Both dashboard Chart.js canvases actually paint, and the console is clean."""
    page.goto(f"{live_server.base}/")
    _wait_for_charts(page, "mainChart", "curveChart")

    for canvas_id in ("mainChart", "curveChart"):
        state = _canvas_is_painted(page, canvas_id)
        assert state["ok"], f"#{canvas_id} did not render: {state}"

    _assert_clean(console_errors, "dashboard")


def test_dashboard_curve_series_toggle_independently(page, live_server,
                                                     console_errors):
    page.goto(f"{live_server.base}/")
    _wait_for_charts(page, "mainChart", "curveChart")

    state = page.evaluate("""() => {
        const chart = Chart.getChart(document.getElementById('curveChart'));
        return {
            labels: chart.data.datasets.map((dataset) => dataset.label),
            visible: chart.data.datasets.map((_, index) => chart.isDatasetVisible(index)),
        };
    }""")
    assert state["labels"] == [
        "Last 90 days MMP", "All-time MMP", "Last ride MMP", "CP/W' model",
    ]
    assert state["visible"] == [True, True, True, True]

    page.get_by_role("button", name=re.compile("Last 90 days MMP")).click()
    state = page.evaluate("""() => {
        const chart = Chart.getChart(document.getElementById('curveChart'));
        return {
            visible: chart.data.datasets.map((_, index) => chart.isDatasetVisible(index)),
            pressed: [...document.querySelectorAll('#curveLegend .legend-item')]
                .map((item) => item.getAttribute('aria-pressed')),
        };
    }""")
    assert state["visible"] == [False, True, True, True]
    assert state["pressed"] == ["false", "true", "true", "true"]

    page.get_by_role("button", name=re.compile("All-time MMP")).click()
    state = page.evaluate("""() => {
        const chart = Chart.getChart(document.getElementById('curveChart'));
        return chart.data.datasets.map((_, index) => chart.isDatasetVisible(index));
    }""")
    assert state == [False, False, True, True]

    page.get_by_role("button", name=re.compile("Last ride MMP")).click()
    state = page.evaluate("""() => {
        const chart = Chart.getChart(document.getElementById('curveChart'));
        return chart.data.datasets.map((_, index) => chart.isDatasetVisible(index));
    }""")
    assert state == [False, False, False, True]

    page.get_by_role("button", name=re.compile("CP/W' model")).click()
    state = page.evaluate("""() => {
        const chart = Chart.getChart(document.getElementById('curveChart'));
        return chart.data.datasets.map((_, index) => chart.isDatasetVisible(index));
    }""")
    assert state == [False, False, False, False]
    _assert_clean(console_errors, "dashboard curve series toggle")


def test_plan_graph_button_expands_row_with_drawn_svg(page, live_server,
                                                      console_errors):
    """The per-workout 'graph' toggle expands its row and draws a real SVG."""
    page.goto(f"{live_server.base}/plan?plan_id={live_server.plan_id}")
    page.wait_for_load_state("networkidle")

    buttons = page.locator(".wk-graph-btn")
    assert buttons.count() > 0, "plan page listed no workouts with a graph button"
    workout_id = buttons.first.get_attribute("data-workout-id")
    buttons.first.click()

    row = page.locator(f"#wk-graph-{workout_id}")
    page.wait_for_selector(f"#wk-graph-{workout_id}:not([hidden])", timeout=10_000)
    page.wait_for_selector(f"#wk-graph-{workout_id} svg.profile-svg", timeout=10_000)
    assert buttons.first.get_attribute("aria-expanded") == "true"
    assert row.locator(".wk-graph-title").inner_text().strip(), "graph row has no title"

    geom = _svg_has_geometry(page, f"#wk-graph-{workout_id} svg.profile-svg")
    assert geom["ok"], f"plan workout graph has no drawn geometry: {geom}"
    _assert_clean(console_errors, "/plan graph toggle")


def test_volume_page_renders_all_four_charts(page, live_server, console_errors):
    """All four weekly-volume canvases paint with data."""
    page.goto(f"{live_server.base}/volume")
    _wait_for_charts(page, "hoursChart", "tssChart", "distanceChart", "caloriesChart")

    for canvas_id in ("hoursChart", "tssChart", "distanceChart", "caloriesChart"):
        state = _canvas_is_painted(page, canvas_id)
        assert state["ok"], f"#{canvas_id} did not render: {state}"

    _assert_clean(console_errors, "/volume")


def test_every_nav_page_loads_clean(page, live_server, console_errors):
    """Every top-level nav page: HTTP 200, expected heading, zero JS errors."""
    failures = []
    for path in NAV_PAGES:
        del console_errors[:]
        response = page.goto(f"{live_server.base}{path}")
        page.wait_for_load_state("networkidle")
        if response.status >= 400:
            failures.append(f"{path}: HTTP {response.status}")
        if page.locator("main h2").count() == 0:
            failures.append(f"{path}: page rendered no <h2> heading")
        for err in console_errors:
            failures.append(f"{path}: {err}")
    assert not failures, "nav pages reported problems:\n" + "\n".join(failures)


def test_ride_stop_ends_the_ride_through_an_in_page_dialog(
    page, live_server, console_errors
):
    """A rider must always be able to end a ride from the page.

    Both Stop buttons used to be gated on ``window.confirm()``. Browsers
    suppress that dialog routinely -- most often once "Prevent this page from
    creating additional dialogs" has been ticked on an earlier one -- and a
    suppressed confirm() returns false, which the handler could not tell from
    the rider pressing Cancel. It returned silently, so both buttons were
    indistinguishable from dead while the ride stayed live and the trainer kept
    holding its target.

    Playwright dismisses browser dialogs, so this test runs in exactly that
    environment: against the old confirm()-gated code the ride never ends here.
    """
    browser_dialogs = []
    page.on(
        "dialog",
        lambda d: (browser_dialogs.append(d.type), d.dismiss()),
    )

    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_selector("#stopBtn:not([disabled])", timeout=15_000)

    # Cancelling leaves the ride alone.
    page.click("#stopBtn")
    page.wait_for_selector("#endRideDialog[open]", timeout=10_000)
    assert page.locator("#endRideCancelBtn").is_visible()
    page.click("#endRideCancelBtn")
    page.wait_for_selector(
        "#endRideDialog:not([open])", state="attached", timeout=10_000
    )
    assert page.locator("#stopBtn").is_enabled(), "cancel ended the ride"

    # Confirming actually ends it: back to idle, which is what re-disables the
    # button. No browser-level dialog was involved at any point.
    page.click("#stopBtn")
    page.wait_for_selector("#endRideDialog[open]", timeout=10_000)
    page.click("#endRideConfirmBtn")
    page.wait_for_selector(
        "#endRideDialog:not([open])", state="attached", timeout=10_000
    )
    page.wait_for_selector("#stopBtn[disabled]", timeout=15_000)

    assert browser_dialogs == [], f"a browser dialog was used: {browser_dialogs}"
    _assert_clean(console_errors, "/ride stop")


# The ride socket is the only way a "connected" frame reaches the page, and a
# simulated ride never produces one. This wraps the real WebSocket so the real
# ride handler receives a real "connected" frame -- carrying the real workout
# the server just sent -- with the warnings list under test. Injection is
# scheduled from the workout frame rather than driven from the test, so it can
# never land after the simulated ride has closed the socket.
def _inject_connected_frame(page, warnings):
    page.add_init_script(
        """
        (() => {
            const WARNINGS = %s;
            const Native = window.WebSocket;
            function Tracked(url, protocols) {
                const socket = protocols === undefined
                    ? new Native(url) : new Native(url, protocols);
                socket.addEventListener('message', (event) => {
                    let frame;
                    try { frame = JSON.parse(event.data); } catch (e) { return; }
                    if (frame.status !== 'workout' || window.__injected) return;
                    window.__injected = true;
                    window.setTimeout(() => {
                        socket.onmessage({data: JSON.stringify({
                            status: 'connected',
                            devices: {power: 'KICKR CORE', hr: 'TICKR'},
                            erg: false,
                            erg_available: false,
                            erg_enabled: false,
                            prepared: false,
                            warnings: WARNINGS,
                            workout: frame.workout,
                        })});
                    }, 0);
                });
                return socket;
            }
            Tracked.prototype = Native.prototype;
            ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(
                (k) => { Tracked[k] = Native[k]; });
            window.WebSocket = Tracked;
        })();
        """
        % json.dumps(warnings)
    )


# How the notice actually looks to the eye, asked of the rendered page rather
# than of the stylesheet. Playwright's is_visible() answers a layout question
# (display / visibility / a box with area) and says nothing about whether a
# rider could see anything: an element at opacity 0, or one whose styling was
# dropped so it renders as bare text merged into the page, passes it happily.
# Both have shipped here before, so this measures perceptibility instead -
# effective opacity down the ancestor chain, the composited colour behind the
# element versus the colour behind its parent, its border, and the contrast of
# its own text. Deliberately no exact colours or lengths: a palette or padding
# change must not fail, an invisible notice must.
_PERCEPTIBILITY_JS = """(selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    // Computed colours come back as rgb()/rgba(), and - for anything that went
    // through color-mix() - as "color(srgb r g b / a)" with 0..1 channels.
    // Reading only the first form silently scores every mixed colour as fully
    // transparent, which is how an element with a real background can look
    // like it has none.
    const parse = (value) => {
        const text = value || '';
        let m = text.match(/rgba?\\(([^)]+)\\)/);
        if (m) {
            const p = m[1].split(/[\\s,\\/]+/).filter((s) => s !== '')
                .map((v) => parseFloat(v));
            return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
        }
        m = text.match(/color\\(srgb ([^)]+)\\)/);
        if (m) {
            const p = m[1].split(/[\\s\\/]+/).filter((s) => s !== '')
                .map((v) => parseFloat(v));
            return [p[0] * 255, p[1] * 255, p[2] * 255,
                    p.length > 3 ? p[3] : 1];
        }
        return [0, 0, 0, 0];
    };
    const over = (top, bottom) => {
        const a = top[3];
        return [
            top[0] * a + bottom[0] * (1 - a),
            top[1] * a + bottom[1] * (1 - a),
            top[2] * a + bottom[2] * (1 - a),
            1,
        ];
    };
    // Everything painted behind (and including) an element, composited onto
    // the page's own backdrop - the colour an eye actually receives.
    const painted = (start) => {
        const chain = [];
        for (let e = start; e; e = e.parentElement) chain.push(e);
        chain.reverse();
        let acc = parse(getComputedStyle(document.documentElement).backgroundColor);
        if (acc[3] < 1) acc = over(acc, [255, 255, 255, 1]);
        for (const e of chain) acc = over(parse(getComputedStyle(e).backgroundColor), acc);
        return acc;
    };
    const luminance = (c) => {
        const channel = (v) => {
            const s = v / 255;
            return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
        };
        return 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2]);
    };
    const contrast = (a, b) => {
        const la = luminance(a), lb = luminance(b);
        return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
    };
    const style = getComputedStyle(el);
    let opacity = 1;
    for (let e = el; e; e = e.parentElement) {
        opacity *= parseFloat(getComputedStyle(e).opacity || '1');
    }
    const behind = painted(el.parentElement);
    const own = painted(el);
    const border = ['Top', 'Right', 'Bottom', 'Left'].reduce((best, side) => {
        const width = parseFloat(style['border' + side + 'Width']) || 0;
        const colour = parse(style['border' + side + 'Color']);
        const strength = width >= 2 ? colour[3] * contrast(
            over(colour, behind), behind) : 0;
        return Math.max(best, strength);
    }, 0);
    const box = el.getBoundingClientRect();
    return {
        opacity: opacity,
        area: [box.width, box.height],
        // How far the element's own surface separates it from the page behind
        // it, and how strong its most visible border is. Either one is enough
        // to set a notice apart; neither means it is not there to look at.
        // The surface is measured as a plain channel distance rather than a
        // luminance ratio: an alert tint over a dark panel is obvious to the
        // eye at almost the same luminance.
        surfaceDelta: Math.max(
            Math.abs(own[0] - behind[0]),
            Math.abs(own[1] - behind[1]),
            Math.abs(own[2] - behind[2])),
        borderStrength: border,
        textContrast: contrast(parse(style.color), own),
    };
}"""


def _perceptibility(page, selector):
    measured = page.evaluate(_PERCEPTIBILITY_JS, selector)
    assert measured is not None, f"{selector} is not in the DOM"
    return measured


def _assert_perceptible(measured, what):
    assert measured["opacity"] > 0.9, f"{what} is painted see-through: {measured}"
    assert measured["area"][0] > 50 and measured["area"][1] > 20, (
        f"{what} has no rendered box: {measured}")
    assert measured["textContrast"] >= 4.5, (
        f"{what}'s text is unreadable on its own background: {measured}")
    assert (
        measured["surfaceDelta"] >= 8 or measured["borderStrength"] >= 1.5
    ), f"{what} is not set apart from the page behind it: {measured}"


def test_ride_sensor_setup_failures_render_a_dismissible_notice(
    page, live_server, console_errors
):
    """A sensor that fails to bind has to be visible ON THE RIDE PAGE.

    The incident: a KICKR selected in the cadence role failed to set up. The
    reason went to the server log only; the page showed an empty cadence field
    and no explanation, so the ride was retried six times. The notice must
    appear next to the readouts, in rider language, and must not take the ride
    away -- the roles that did bind keep running behind it.
    """
    warning = ("Cadence sensor KICKR CORE couldn't be used — it doesn't report "
               "cadence in a way wattracker can read.")
    _inject_connected_frame(page, [warning])

    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_selector("#sensorNotice", timeout=15_000)

    notice = page.locator("#sensorNotice")
    assert notice.is_visible()
    assert warning in notice.inner_text()

    # Visible to the eye, not merely present in the layout.
    _assert_perceptible(
        _perceptibility(page, "#sensorNotice"), "the sensor notice"
    )

    # Rendered, next to the readouts, and covering none of them.
    geometry = page.evaluate(
        """() => {
            const notice = document.getElementById('sensorNotice');
            const cards = document.querySelector('.ride-live .cards');
            // Hit-testing only answers for points inside the viewport.
            notice.scrollIntoView({block: 'center'});
            const n = notice.getBoundingClientRect();
            const c = cards.getBoundingClientRect();
            const readouts = ['rPower', 'rCad', 'rHr'].map((id) => {
                const el = document.getElementById(id);
                const box = el.getBoundingClientRect();
                // Whatever the browser hit-tests at the middle of the number
                // must be the number or something containing it -- never the
                // notice sitting on top of it.
                const hit = document.elementFromPoint(
                    box.left + box.width / 2, box.top + box.height / 2);
                return {
                    id: id,
                    visible: box.width > 0 && box.height > 0,
                    hit: hit ? (hit.id || hit.className || hit.tagName) : null,
                    covered: !hit || !(hit === el || hit.contains(el)),
                };
            });
            return {
                drawn: n.width > 50 && n.height > 20,
                above: n.bottom <= c.top + 1,
                gap: Math.round(c.top - n.bottom),
                readouts: readouts,
            };
        }"""
    )
    assert geometry["drawn"], f"notice has no rendered box: {geometry}"
    assert geometry["above"], f"notice is not beside the readouts: {geometry}"
    assert geometry["gap"] < 60, f"notice is nowhere near the readouts: {geometry}"
    for readout in geometry["readouts"]:
        assert readout["visible"] and not readout["covered"], geometry

    # The ride is live behind the notice: it is a notice, not a blocker.
    assert page.locator("#stopBtn").is_enabled()

    # Dismissible, and dismissing leaves nothing behind.
    page.click("#sensorNoticeDismiss")
    page.wait_for_selector("#sensorNotice", state="detached", timeout=10_000)
    assert page.locator(".sensor-notice").count() == 0
    assert page.locator("#rCad").is_visible()
    assert page.locator("#stopBtn").is_enabled()
    _assert_clean(console_errors, "/ride sensor failure notice")


def test_ride_without_sensor_failures_renders_no_notice_at_all(
    page, live_server, console_errors
):
    """No failures must leave no container: an empty box above the readouts
    reads as "something is wrong" every ride."""
    _inject_connected_frame(page, [])

    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_function(
        """() => (document.getElementById('connectionStatus').textContent || '')
                 .indexOf('Connected') !== -1""",
        timeout=15_000,
    )

    leftovers = page.evaluate(
        """() => Array.from(
            document.querySelectorAll('#sensorNotice, .sensor-notice, #sensorNoticeDismiss')
        ).map((el) => el.outerHTML)"""
    )
    assert leftovers == [], f"an empty notice was left in the DOM: {leftovers}"
    assert page.locator("#rCad").is_visible()
    _assert_clean(console_errors, "/ride without sensor failures")


def test_workout_graphs_are_real_svg_elements(page, live_server, console_errors):
    """The graph markup must parse into SVG, not namespace-less unknown nodes.

    profileSvg's output is handed to DOMParser as "image/svg+xml", and XML
    parsing does NOT infer namespaces the way the HTML parser does. Without an
    explicit xmlns the root parses with a null namespace, so the browser builds
    plain Elements instead of SVGSVGElement: nothing renders as a graph, no
    .profile-svg CSS applies, and the <text>/<title> contents are laid out as
    ordinary inline HTML. That is what made a Just Ride card read as one run of
    digits and words at assorted sizes (#72).

    The suite stayed green throughout, because a null-namespace element still
    has a bounding box and still holds path `d` attributes - so every existing
    geometry check passed while nothing was drawn. Identity is the assertion
    that would have caught it.
    """
    page.goto(f"{live_server.base}/ride")
    page.get_by_role("button", name="Just ride", exact=True).click()
    page.wait_for_selector("#rideVariantGrid .ride-variant-card", timeout=10_000)

    report = page.evaluate(
        """() => Array.from(document.querySelectorAll('svg.profile-svg')).map(el => ({
            isSVG: el instanceof SVGSVGElement,
            ns: el.namespaceURI,
            drawn: el.getBoundingClientRect().width > 0
                && el.getBoundingClientRect().height > 0,
        }))"""
    )
    assert report, "no profile graphs on the page"
    assert all(g["isSVG"] for g in report), f"not real SVG elements: {report}"
    assert all(g["ns"] == "http://www.w3.org/2000/svg" for g in report), report
    assert all(g["drawn"] for g in report), f"a graph has no box: {report}"
    _assert_clean(console_errors, "/ride graph namespaces")


def test_just_ride_cards_are_one_per_row_and_legible(page, live_server,
                                                     console_errors):
    """Each shape is its own row, with a thumbnail and short readable fields.

    Side-by-side cards at a 148px minimum squeezed the title to an ellipsis and
    the description to nothing, and the thumbnail's own axis labels collapsed
    into the card text. The card graph is drawn bare for that reason, so it
    must contribute no text at all.
    """
    page.goto(f"{live_server.base}/ride")
    page.get_by_role("button", name="Just ride", exact=True).click()
    page.wait_for_selector("#rideVariantGrid .ride-variant-card", timeout=10_000)

    cards = page.evaluate(
        """() => Array.from(
            document.querySelectorAll('#rideVariantGrid .ride-variant-card')
        ).map(c => {
            const r = c.getBoundingClientRect();
            const svg = c.querySelector('svg.profile-svg');
            const box = svg ? svg.getBoundingClientRect() : null;
            return {
                top: Math.round(r.top), left: Math.round(r.left),
                width: Math.round(r.width),
                thumb: box ? [Math.round(box.width), Math.round(box.height)] : null,
                svgText: svg ? (svg.textContent || '').trim() : '',
                text: (c.innerText || '').trim(),
            };
        })"""
    )
    assert len(cards) >= 2, cards
    # One per row: every card starts at the same x and a distinct y.
    assert len({c["left"] for c in cards}) == 1, f"cards sit side by side: {cards}"
    assert len({c["top"] for c in cards}) == len(cards), f"cards overlap: {cards}"
    for card in cards:
        assert card["thumb"] and card["thumb"][0] > 20 and card["thumb"][1] > 20, card
        # The bare thumbnail contributes no text, so nothing can run together.
        assert card["svgText"] == "", f"thumbnail leaked text: {card['svgText']!r}"
        assert not re.search(r"\d{6,}", card["text"].replace(" ", "")), card["text"]
    _assert_clean(console_errors, "/ride card layout")
