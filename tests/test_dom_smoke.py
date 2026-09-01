"""Real-browser (DOM-level) smoke tests.

The rest of the suite checks JS by grepping the *source* of the .js files for
substrings. That cannot see a chart that throws on render, an SVG that comes
out empty, or a modal that never opens -- three bugs of exactly that shape
shipped past a green suite. These tests drive the actual app in Chromium and
assert on rendered geometry plus a clean JS console.

Requires the optional `playwright` package AND its chromium binary
(`playwright install chromium`, a ~150MB download outside pip). Both are
checked below; when either is missing the whole module SKIPS, so local
machines without the browser still go green. CI installs Chromium explicitly.

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
from wattracker.server import DEFAULT_AUDIO_CUE_VOLUME, create_app  # noqa: E402

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
        # Start on the next Monday so the plan always has a future workout,
        # including when today is after the eighth or at a month boundary. The
        # calendar request below follows the plan into its month explicitly.
        start = today + dt.timedelta(days=7 - today.weekday())
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
                                  "plan_year": start.year,
                                  "plan_month": start.month,
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
    page.goto(
        f"{live_server.base}/calendar?year={live_server.plan_year}"
        f"&month={live_server.plan_month}"
    )
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
            tick_values: chart.scales.x.ticks.map((tick) => Number(tick.value)),
        };
    }""")
    assert state["labels"] == [
        "Last 90 days MMP", "All-time MMP", "Last ride MMP", "CP/W' model",
    ]
    assert state["visible"] == [True, True, True, True]
    assert len(state["tick_values"]) == len(set(state["tick_values"])), \
        f"curve x-axis ticks contain duplicates: {state['tick_values']}"
    assert all(
        earlier < later
        for earlier, later in zip(state["tick_values"], state["tick_values"][1:])
    ), f"curve x-axis ticks are not strictly increasing: {state['tick_values']}"

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

    page.get_by_role("button", name="CP/W' model", exact=True).click()
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


def test_volume_tiles_select_the_metric_the_hero_chart_plots(
        page, live_server, console_errors):
    """The tile row is the selector and the one chart follows it.

    The page used to stack four full-width bar panels that all drew the same
    shape. Now the four metrics live as sparkline tiles and exactly one of them
    is plotted below, so the test that matters is no longer "four canvases
    painted" but "the tiles are real toggles and pressing one repaints the one
    chart with the metric it names".
    """
    page.goto(f"{live_server.base}/volume")
    _wait_for_charts(page, "volumeChart")

    state = _canvas_is_painted(page, "volumeChart")
    assert state["ok"], f"#volumeChart did not render: {state}"
    # Weekly bars plus the trailing mean, on one y axis.
    assert state["datasets"] == 2, f"expected bars + mean line, got {state}"
    axes = page.evaluate(
        """() => Object.keys(
            Chart.getChart(document.getElementById('volumeChart')).scales)"""
    )
    assert sorted(axes) == ["x", "y"], f"hero chart must be single-y-axis: {axes}"

    # The head above the row: it names the period the tiles quote and says the
    # tiles are the chart's selector. Rendering the tiles must not eat it --
    # renderSummary clears the inner container, never the section.
    assert page.locator("#volumeSummary .summary-head h3").is_visible()
    # .lower(): the pill is uppercased in CSS, like every other .label.
    assert page.locator(
        "#volumeSummary .summary-period"
    ).inner_text().strip().lower() == "latest 4 weeks"
    note = page.locator("#volumeSummary .summary-note").inner_text().lower()
    # The half that is true whatever the history holds. (The comparison half is
    # conditional -- this fixture only seeds ~4 weeks, so there is no preceding
    # window to compare against; see the 12-week test below.)
    assert "totals sum the latest four weekly buckets" in note, note
    assert "plot that metric in the chart below" in note, (
        f"note does not say the tiles are the selector: {note}"
    )

    tiles = page.locator("#volumeSummary .metric-tile")
    assert tiles.count() == 4, f"expected four metric tiles, got {tiles.count()}"
    # And they live inside the cards container, not loose in the section.
    assert page.locator("#volumeSummaryTiles .metric-tile").count() == 4
    pressed = page.locator('#volumeSummary .metric-tile[aria-pressed="true"]')
    assert pressed.count() == 1, "exactly one tile must be pressed at a time"

    # Every tile carries a drawn sparkline, not an empty <svg></svg>.
    sparks = page.evaluate(
        """() => Array.from(
            document.querySelectorAll('#volumeSummary .metric-tile')
        ).map(tile => {
            const svg = tile.querySelector('svg.sparkline');
            if (!svg) return {ok: false, why: 'tile has no sparkline'};
            const poly = svg.querySelector('polyline');
            const path = svg.querySelector('path');
            const box = svg.getBoundingClientRect();
            const pts = poly ? (poly.getAttribute('points') || '') : '';
            const d = path ? (path.getAttribute('d') || '') : '';
            return {
                ok: pts.split(' ').length > 1 && d.length > 20
                    && box.width > 10 && box.height > 5,
                key: tile.dataset.key, points: pts.length, d: d.length,
                w: box.width, h: box.height,
            };
        })"""
    )
    for spark in sparks:
        assert spark["ok"], f"tile sparkline not drawn: {spark}"

    # Pressing an unpressed tile moves the selection AND repaints the chart.
    before = page.evaluate(
        """() => Chart.getChart(
            document.getElementById('volumeChart')).options.scales.y.title.text"""
    )
    # Resolve the target by key before clicking: a locator on
    # [aria-pressed="false"] re-queries after the click and would silently
    # point at a different tile.
    other_key = page.locator(
        '#volumeSummary .metric-tile[aria-pressed="false"]'
    ).first.get_attribute("data-key")
    other = page.locator(f'#volumeSummary .metric-tile[data-key="{other_key}"]')
    other.click()
    page.wait_for_timeout(150)

    assert other.get_attribute("aria-pressed") == "true"
    assert page.locator(
        '#volumeSummary .metric-tile[aria-pressed="true"]'
    ).count() == 1
    after = page.evaluate(
        """() => Chart.getChart(
            document.getElementById('volumeChart')).options.scales.y.title.text"""
    )
    assert after != before, (
        f"clicking the {other_key} tile left the chart on {before}"
    )
    state = _canvas_is_painted(page, "volumeChart")
    assert state["ok"], f"#volumeChart blank after switching metric: {state}"

    _assert_clean(console_errors, "/volume")


def test_volume_hero_chart_ignores_a_near_zero_drag(
        page, live_server, console_errors):
    """A ~0px drag on the chart is a click, not a range selection.

    Below the 2-bucket floor `onDragZoom` must leave winStart/winEnd (and the
    preset highlight) untouched AND cancel the zoom plugin's own transform --
    otherwise the chart sits visually zoomed to a state the module's window
    disagrees with, recoverable only via Reset zoom or a preset.
    """
    page.goto(f"{live_server.base}/volume")
    _wait_for_charts(page, "volumeChart")

    canvas = page.locator("#volumeChart")
    box = canvas.bounding_box()
    mid_y = box["y"] + box["height"] / 2
    start_x = box["x"] + box["width"] / 2

    before = page.evaluate(
        """() => Chart.getChart(document.getElementById('volumeChart'))
            .data.labels.slice()"""
    )
    active_before = page.locator(
        '#volumeControls .range-btn.active'
    ).count()

    # A near-0px drag: down, move 1px, up.
    page.mouse.move(start_x, mid_y)
    page.mouse.down()
    page.mouse.move(start_x + 1, mid_y)
    page.mouse.up()
    page.wait_for_timeout(150)

    after = page.evaluate(
        """() => Chart.getChart(document.getElementById('volumeChart'))
            .data.labels.slice()"""
    )
    assert after == before, "a near-0px drag re-windowed the chart"
    # Not left in a visually-zoomed state either: the x scale should still
    # span the whole label set, not a sliver of it.
    scale_span = page.evaluate(
        """() => {
            const s = Chart.getChart(document.getElementById('volumeChart'))
                .scales.x;
            return s.max - s.min;
        }"""
    )
    assert scale_span >= before.__len__() - 1.5, (
        f"chart left visually zoomed after a near-0px drag: span={scale_span}"
    )
    assert page.locator('#volumeControls .range-btn.active').count() == \
        active_before

    # A genuine drag still re-windows as before.
    page.mouse.move(start_x, mid_y)
    page.mouse.down()
    page.mouse.move(start_x + box["width"] / 3, mid_y)
    page.mouse.up()
    page.wait_for_timeout(150)

    zoomed = page.evaluate(
        """() => Chart.getChart(document.getElementById('volumeChart'))
            .data.labels.slice()"""
    )
    assert len(zoomed) < len(before), (
        f"a genuine drag did not re-window: before={len(before)} "
        f"after={len(zoomed)}"
    )

    _assert_clean(console_errors, "/volume drag-zoom")


def _stub_volume_weeks(page, weeks):
    page.route(
        "**/api/volume",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"weeks": weeks}),
        ),
    )


def test_volume_summary_note_states_the_comparison_when_one_was_made(
        page, live_server, console_errors):
    """With a full preceding window the note owns the comparison the tiles make.

    The tiles quote a period ("vs prev 4") the page otherwise never names, so
    the head has to name it -- but only when it is true.
    """
    weeks = []
    monday = dt.date(2026, 1, 5)
    for i in range(12):
        d = monday + dt.timedelta(weeks=i)
        weeks.append({
            "week_start": d.isoformat(),
            "hours": 5 + i, "tss": 300 + i * 10,
            "distance_km": 120 + i * 5, "calories": 2000 + i * 50,
        })
    _stub_volume_weeks(page, weeks)
    page.goto(f"{live_server.base}/volume")
    _wait_for_charts(page, "volumeChart")

    note = page.locator("#volumeSummary .summary-note").inner_text().lower()
    assert "preceding four" in note, f"note does not state the baseline: {note}"
    assert "plot that metric in the chart below" in note, note
    # ...and the tiles really are quoting that comparison.
    assert "vs prev 4" in page.locator(
        "#volumeSummary .metric-tile"
    ).first.inner_text().lower()

    _assert_clean(console_errors, "/volume 12-week history")


def test_volume_tile_reports_no_baseline_for_all_zero_history(
        page, live_server, console_errors):
    """An all-zero history must not claim '0% vs prev 4' -- that percentage
    (0/0) is undefined, not zero.
    """
    weeks = []
    monday = dt.date(2026, 1, 5)
    for i in range(8):
        d = monday + dt.timedelta(weeks=i)
        weeks.append({
            "week_start": d.isoformat(),
            "hours": 0, "tss": 0, "distance_km": 0, "calories": 0,
        })
    page.route(
        "**/api/volume",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"weeks": weeks}),
        ),
    )
    page.goto(f"{live_server.base}/volume")
    _wait_for_charts(page, "volumeChart")

    tiles = page.locator("#volumeSummary .metric-tile")
    assert tiles.count() == 4
    for i in range(tiles.count()):
        text = tiles.nth(i).inner_text().lower()
        assert "0%" not in text, f"all-zero history reported a percentage: {text}"
        assert "vs prev 4" not in text, (
            f"all-zero history still compared against a zero baseline: {text}"
        )
        assert "no baseline" in text, f"tile did not explain the zero baseline: {text}"

    # ...and the note must not assert the comparison the tiles just denied,
    # while still saying what a tile does.
    note = page.locator("#volumeSummary .summary-note").inner_text().lower()
    assert "preceding four" not in note, (
        f"note claimed a comparison no tile could make: {note}"
    )
    assert "totals sum the latest four weekly buckets." in note, (
        f"suppressing the comparison left an ungrammatical note: {note}"
    )
    assert "plot that metric in the chart below" in note

    _assert_clean(console_errors, "/volume all-zero history")



def test_volume_empty_history_keeps_the_summary_head_hidden(
        page, live_server, console_errors):
    """No weeks at all: the empty hint shows and the summary stays down.

    The section is no longer empty markup -- it now ships a heading, a period
    pill and a note -- so "renderSummary never ran" has to mean an invisible
    section, not a head floating above no tiles.
    """
    _stub_volume_weeks(page, [])
    page.goto(f"{live_server.base}/volume")
    page.wait_for_timeout(800)
    assert page.locator("#volumeEmpty").is_visible()
    assert not page.locator("#volumeSummary").is_visible()
    assert not page.locator("#volumeBlock").is_visible()
    _assert_clean(console_errors, "/volume empty history")


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


def test_ride_ramp_test_accept_dismisses_the_result_dialog(
    page, live_server, console_errors
):
    """Accepting a valid result saves it and dismisses the modal."""
    page.add_init_script(
        """
        (() => {
            const NativeWebSocket = window.WebSocket;
            function Tracked(url, protocols) {
                const socket = protocols === undefined
                    ? new NativeWebSocket(url) : new NativeWebSocket(url, protocols);
                let handler = null;
                Object.defineProperty(socket, 'onmessage', {
                    configurable: true,
                    get() { return handler; },
                    set(fn) { handler = fn; },
                });
                socket.__deliver = (frame) => {
                    if (handler) handler({data: JSON.stringify(frame)});
                };
                window.__rampSocket = socket;
                return socket;
            }
            Tracked.prototype = NativeWebSocket.prototype;
            ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(
                (key) => { Tracked[key] = NativeWebSocket[key]; });
            window.WebSocket = Tracked;

            const nativeFetch = window.fetch;
            window.__rampAcceptRequests = [];
            window.fetch = (input, init) => {
                if (input === '/api/ftp/ramp-test/accept') {
                    window.__rampAcceptRequests.push(JSON.parse(init.body));
                    return Promise.resolve(new Response(JSON.stringify({
                        ftp: 211.5, date: '2026-08-29',
                    }), {status: 200, headers: {'Content-Type': 'application/json'}}));
                }
                return nativeFetch(input, init);
            };
        })();
        """
    )
    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_function("() => window.__rampSocket && window.__rampSocket.__deliver")
    page.evaluate(
        """() => window.__rampSocket.__deliver({
            status: 'finished', elapsed: 120, power: 0, target_watts: 0,
            cadence: null, hr: null, progress: 1, segment_index: 0,
            segment_count: 1,
            ramp_test: {offer: true, ftp: 211.5, activity_id: 42,
                        completed_ramp: true, disagreement: false,
                        message: 'Ramp test complete.'}
        })"""
    )
    page.wait_for_selector("#rampTestDialog[open]")
    page.click("#rampTestAcceptBtn")
    page.wait_for_selector("#rampTestDialog:not([open])", state="attached")
    assert page.evaluate("window.__rampAcceptRequests") == [{"activity_id": 42}]
    assert page.locator("#rampTestStatus").inner_text() == "FTP set to 212 W on 2026-08-29."
    assert page.locator("#rampTestAcceptBtn").is_hidden()
    _assert_clean(console_errors, "/ride ramp test accept")


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


# The simulated ride is the only in-browser ride, and its server side never
# reads the socket, so the server's half of the exchange has to come from here.
# This records every outbound frame and then behaves as the ride handler does:
# it clamps and answers a nudge immediately, and it carries the bias on every
# subsequent state frame -- which matters, because a state frame that still
# said 1.0 would wipe the badge a tick later.
def _record_sends_and_answer_intensity(page):
    page.add_init_script(
        """
        (() => {
            window.__sent = [];
            window.__bias = 1;
            const Native = window.WebSocket;
            function Tracked(url, protocols) {
                const socket = protocols === undefined
                    ? new Native(url) : new Native(url, protocols);
                let handler = null;
                Object.defineProperty(socket, 'onmessage', {
                    configurable: true,
                    get() { return handler; },
                    set(fn) { handler = fn; },
                });
                // The demo ride compresses ~45 minutes into ~1 second, then
                // finishes and closes: the page drops out of full screen (as
                // it should, when a ride really ends) and onclose nulls the
                // socket, so the keys would be inert for reasons that have
                // nothing to do with riding. The two lines below and the
                // dropped 'finished' frame keep the ride live for the length
                // of the test; everything the page renders still came from the
                // real ride frames.
                Object.defineProperty(socket, 'readyState', {
                    configurable: true, get: () => Native.OPEN,
                });
                Object.defineProperty(socket, 'onclose', {
                    configurable: true, get() { return null; }, set(fn) {},
                });
                socket.__deliver = (frame) => {
                    if (handler) handler({data: JSON.stringify(frame)});
                };
                socket.addEventListener('message', (event) => {
                    if (!handler) return;
                    let frame = null;
                    try { frame = JSON.parse(event.data); } catch (e) { frame = null; }
                    if (frame && frame.status === 'finished') return;
                    if (frame && 'intensity_bias' in frame) {
                        frame.intensity_bias = window.__bias;
                        socket.__deliver(frame);
                        return;
                    }
                    handler(event);
                });
                const nativeSend = socket.send.bind(socket);
                socket.send = (data) => {
                    window.__sent.push(data);
                    let frame = null;
                    try { frame = JSON.parse(data); } catch (e) { frame = null; }
                    if (frame && frame.action === 'adjust_intensity') {
                        const next = Math.round(
                            (window.__bias + frame.delta / 100) * 100) / 100;
                        window.__bias = Math.min(1.5, Math.max(0.5, next));
                        const bias = window.__bias;
                        window.setTimeout(
                            () => socket.__deliver({status: 'intensity', bias: bias}), 0);
                        return;
                    }
                    return nativeSend(data);
                };
                return socket;
            }
            Tracked.prototype = Native.prototype;
            ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(
                (k) => { Tracked[k] = Native[k]; });
            window.WebSocket = Tracked;
        })();
        """
    )


# Both badges, as the rider sees them: the text of each, or None when it does
# not render at all. Read in one page-side pass, because a hidden attribute
# toggling under a live ride makes two separate Playwright queries disagree.
_BADGE_STATE_JS = """() => {
    const read = (id) => {
        const el = document.getElementById(id);
        if (!el || el.hidden) return null;
        const style = getComputedStyle(el);
        const box = el.getBoundingClientRect();
        if (style.display === 'none' || style.visibility === 'hidden') return null;
        if (Number(style.opacity) === 0 || box.width <= 0 || box.height <= 0) return null;
        return el.textContent;
    };
    return {chart: read('rIntensityBiasChart'), card: read('rIntensityBias')};
}"""


def _record_sends_and_answer_intensity_isolatable(page):
    """Same harness as `_record_sends_and_answer_intensity`, but exposes the
    live socket as `window.__socket` (for injecting a raw frame directly), a
    `window.__lockIntensity` toggle that answers a nudge the way a ramp test
    does (refused, bias unmoved, `locked: true`), and a
    `window.__suppressRunningFrames` toggle that drops real inbound ride
    ticks entirely -- letting a test isolate the synthesized `status:
    "intensity"` reply from the per-tick state frame, since both otherwise
    render the same badge and mask each other's removal."""
    page.add_init_script(
        """
        (() => {
            window.__sent = [];
            window.__bias = 1;
            window.__lockIntensity = false;
            window.__suppressRunningFrames = false;
            const Native = window.WebSocket;
            function Tracked(url, protocols) {
                const socket = protocols === undefined
                    ? new Native(url) : new Native(url, protocols);
                window.__socket = socket;
                let handler = null;
                Object.defineProperty(socket, 'onmessage', {
                    configurable: true,
                    get() { return handler; },
                    set(fn) { handler = fn; },
                });
                Object.defineProperty(socket, 'readyState', {
                    configurable: true, get: () => Native.OPEN,
                });
                Object.defineProperty(socket, 'onclose', {
                    configurable: true, get() { return null; }, set(fn) {},
                });
                socket.__deliver = (frame) => {
                    if (handler) handler({data: JSON.stringify(frame)});
                };
                socket.addEventListener('message', (event) => {
                    if (!handler) return;
                    let frame = null;
                    try { frame = JSON.parse(event.data); } catch (e) { frame = null; }
                    if (frame && frame.status === 'finished') return;
                    if (window.__suppressRunningFrames && frame &&
                        (frame.status === 'running' || frame.status === 'starting' ||
                         frame.status === 'paused' || frame.status === 'cooldown')) {
                        return;
                    }
                    if (frame && 'intensity_bias' in frame) {
                        frame.intensity_bias = window.__bias;
                        socket.__deliver(frame);
                        return;
                    }
                    handler(event);
                });
                const nativeSend = socket.send.bind(socket);
                socket.send = (data) => {
                    window.__sent.push(data);
                    let frame = null;
                    try { frame = JSON.parse(data); } catch (e) { frame = null; }
                    if (frame && frame.action === 'adjust_intensity') {
                        if (window.__lockIntensity) {
                            window.setTimeout(() => socket.__deliver(
                                {status: 'intensity', bias: window.__bias,
                                 locked: true}), 0);
                            return;
                        }
                        const next = Math.round(
                            (window.__bias + frame.delta / 100) * 100) / 100;
                        window.__bias = Math.min(1.5, Math.max(0.5, next));
                        const bias = window.__bias;
                        window.setTimeout(
                            () => socket.__deliver({status: 'intensity', bias: bias}), 0);
                        return;
                    }
                    return nativeSend(data);
                };
                return socket;
            }
            Tracked.prototype = Native.prototype;
            ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(
                (k) => { Tracked[k] = Native[k]; });
            window.WebSocket = Tracked;
        })();
        """
    )


def _badge_state(page):
    return page.evaluate(_BADGE_STATE_JS)


def _wait_for(page, expression, timeout=10_000):
    # Timer polling, not rAF: the chart panel's animation frames are not a
    # reliable clock while it is full screen.
    page.wait_for_function("() => " + expression, timeout=timeout, polling=100)


def _wait_for_badge(page, text, timeout=10_000):
    """Wait until BOTH badges render exactly `text` and are really visible."""
    page.wait_for_function(
        "(want) => { const s = (" + _BADGE_STATE_JS + ")();"
        " return s.chart === want && s.card === want; }",
        arg=text, timeout=timeout, polling=100)


def test_ride_fullscreen_plus_minus_nudges_intensity_and_badges_it(
    page, live_server, console_errors
):
    """+/- in full screen is the only way to change intensity mid-ride.

    Full screen hides the cards row, so the badge is asserted where the rider
    actually is -- on the chart chip -- and the keys are asserted to be inert
    before full screen, where they would otherwise fight ordinary page use.
    """
    _record_sends_and_answer_intensity(page)
    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_selector("#stopBtn:not([disabled])", timeout=15_000)

    # Not in full screen: the keys do nothing at all.
    page.keyboard.press("+")
    page.keyboard.press("-")
    assert page.evaluate("window.__sent.filter("
                         "(d) => d.indexOf('adjust_intensity') >= 0)") == []
    assert _badge_state(page) == {"chart": None, "card": None}

    page.click("#chartFullscreenBtn")
    _wait_for(page, "document.getElementById('chartFullscreenBtn')"
                    ".getAttribute('aria-pressed') === 'true'")

    for _ in range(3):
        page.keyboard.press("+")
    _wait_for_badge(page, "+3%")
    sent = [json.loads(d) for d in page.evaluate("window.__sent")]
    nudges = [f for f in sent if f.get("action") == "adjust_intensity"]
    assert nudges == [{"action": "adjust_intensity", "delta": 1}] * 3

    # Down through neutral: the badge disappears at 0% and comes back negative.
    for _ in range(3):
        page.keyboard.press("-")
    _wait_for(page, "document.getElementById('rIntensityBiasChart').hidden")
    assert _badge_state(page) == {"chart": None, "card": None}

    page.keyboard.press("-")
    _wait_for_badge(page, "-1%")

    # Back out of full screen: the badge the rider set is still on the card.
    page.keyboard.press("Escape")
    _wait_for(page, "document.getElementById('chartFullscreenBtn')"
                    ".getAttribute('aria-pressed') === 'false'")
    assert _badge_state(page)["card"] == "-1%"

    _assert_clean(console_errors, "/ride intensity nudge")


def test_ride_fullscreen_equals_and_underscore_are_aliases_for_plus_minus(
    page, live_server, console_errors
):
    """'=' and '_' (the unshifted/shifted keys sharing +/- on a US keyboard)
    must drive the same nudge, so a rider does not have to hit Shift."""
    _record_sends_and_answer_intensity(page)
    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_selector("#stopBtn:not([disabled])", timeout=15_000)
    page.click("#chartFullscreenBtn")
    _wait_for(page, "document.getElementById('chartFullscreenBtn')"
                    ".getAttribute('aria-pressed') === 'true'")

    for _ in range(3):
        page.keyboard.press("=")
    _wait_for_badge(page, "+3%")
    sent = [json.loads(d) for d in page.evaluate("window.__sent")]
    nudges = [f for f in sent if f.get("action") == "adjust_intensity"]
    assert nudges == [{"action": "adjust_intensity", "delta": 1}] * 3

    for _ in range(4):
        page.keyboard.press("_")
    _wait_for_badge(page, "-1%")

    _assert_clean(console_errors, "/ride intensity nudge alias keys")


def test_ride_fullscreen_intensity_key_prevents_default_only_for_itself(
    page, live_server, console_errors
):
    _record_sends_and_answer_intensity(page)
    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_selector("#stopBtn:not([disabled])", timeout=15_000)
    page.click("#chartFullscreenBtn")
    _wait_for(page, "document.getElementById('chartFullscreenBtn')"
                    ".getAttribute('aria-pressed') === 'true'")

    page.evaluate(
        """() => {
            window.__lastPrevented = {};
            document.addEventListener('keydown', (event) => {
                window.__lastPrevented[event.key] = event.defaultPrevented;
            }, {capture: false});
        }"""
    )
    page.keyboard.press("+")
    page.keyboard.press("a")
    prevented = page.evaluate("window.__lastPrevented")
    assert prevented["+"] is True
    assert prevented["a"] is False

    _assert_clean(console_errors, "/ride intensity nudge preventDefault")


def test_ride_fullscreen_intensity_keys_are_inert_while_typing(
    page, live_server, console_errors
):
    """A focused form control inside the chart panel absorbs +/- as normal
    text input instead of nudging the ride."""
    _record_sends_and_answer_intensity(page)
    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_selector("#stopBtn:not([disabled])", timeout=15_000)
    page.click("#chartFullscreenBtn")
    _wait_for(page, "document.getElementById('chartFullscreenBtn')"
                    ".getAttribute('aria-pressed') === 'true'")

    page.evaluate(
        """() => {
            const input = document.createElement('input');
            input.type = 'text';
            input.id = 'probeTypingInput';
            document.getElementById('rideChartPanel').appendChild(input);
            input.focus();
        }"""
    )
    page.keyboard.press("+")
    page.keyboard.press("-")
    assert page.evaluate("window.__sent.filter("
                         "(d) => d.indexOf('adjust_intensity') >= 0)") == []
    assert _badge_state(page) == {"chart": None, "card": None}
    assert page.evaluate(
        "document.getElementById('probeTypingInput').value"
    ) == "+-"

    _assert_clean(console_errors, "/ride intensity nudge typing gate")


def test_ride_fullscreen_intensity_badge_renders_from_the_status_reply_alone(
    page, live_server, console_errors
):
    """renderIntensityBias is called from two places (the per-tick state
    frame and the synthesized "intensity" status reply); with real ride
    ticks suppressed, only the reply path can be driving the badge here."""
    _record_sends_and_answer_intensity_isolatable(page)
    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_selector("#stopBtn:not([disabled])", timeout=15_000)
    page.click("#chartFullscreenBtn")
    _wait_for(page, "document.getElementById('chartFullscreenBtn')"
                    ".getAttribute('aria-pressed') === 'true'")

    page.evaluate("window.__suppressRunningFrames = true")
    page.keyboard.press("+")
    page.keyboard.press("+")
    _wait_for_badge(page, "+2%")

    _assert_clean(console_errors, "/ride intensity badge from reply alone")


def test_ride_fullscreen_intensity_badge_renders_from_a_state_frame_alone(
    page, live_server, console_errors
):
    """A raw state frame carrying intensity_bias, delivered without ever
    going through the adjust_intensity action/reply, must still badge it --
    proving the per-tick render() path (not the "intensity" reply) is what
    rendered it."""
    _record_sends_and_answer_intensity_isolatable(page)
    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_selector("#stopBtn:not([disabled])", timeout=15_000)
    page.click("#chartFullscreenBtn")
    _wait_for(page, "document.getElementById('chartFullscreenBtn')"
                    ".getAttribute('aria-pressed') === 'true'")

    # Bypass the send/reply machinery entirely: hand the socket a synthetic
    # running frame with a bias no keypress ever requested.
    page.evaluate(
        """() => {
            window.__socket.__deliver({
                status: 'running', intensity_bias: 1.07, target_watts: 150,
                power: 150, cadence: 90, hr: null, elapsed: 10,
                segment_index: 0, segment_count: 1, progress: 0.5
            });
        }"""
    )
    _wait_for_badge(page, "+7%")

    _assert_clean(console_errors, "/ride intensity badge from state frame alone")


_LOCK_NOTE = "Intensity locked during a ramp test"


def test_ride_fullscreen_intensity_locked_reply_shows_a_note_not_a_percentage(
    page, live_server, console_errors
):
    """A ramp test refuses the nudge, so the chip must explain the dead key.

    A `+0%` badge would be the wrong answer twice over: it reads as a bias the
    rider set, and 0% is precisely the state the chip is meant to be absent
    for. The note is transient -- the 1 Hz state frames must not wipe it the
    instant it appears, and it must not sit there for the rest of the ride.
    """
    _record_sends_and_answer_intensity_isolatable(page)
    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.click("#simBtn")
    page.wait_for_selector("#stopBtn:not([disabled])", timeout=15_000)
    page.click("#chartFullscreenBtn")
    _wait_for(page, "document.getElementById('chartFullscreenBtn')"
                    ".getAttribute('aria-pressed') === 'true'")

    # Nothing is badged on an ordinary ride at 0%, before any key is pressed.
    assert _badge_state(page) == {"chart": None, "card": None}

    page.evaluate("window.__lockIntensity = true")
    page.keyboard.press("+")
    _wait_for_badge(page, _LOCK_NOTE)

    # It survives the state frames arriving underneath it...
    page.wait_for_timeout(1_500)
    shown = _badge_state(page)
    assert shown["chart"] == shown["card"] == _LOCK_NOTE
    assert "%" not in shown["chart"]

    # ...and then clears itself, back to the no-badge state of an unbiased ride.
    _wait_for(page, "document.getElementById('rIntensityBiasChart').hidden",
              timeout=15_000)
    assert _badge_state(page) == {"chart": None, "card": None}

    # The keys are not broken by the refusal: an unlocked ride still nudges.
    page.evaluate("window.__lockIntensity = false")
    page.keyboard.press("+")
    _wait_for_badge(page, "+1%")

    _assert_clean(console_errors, "/ride intensity locked note")


# ------------------------------------------------------- audio cue volume
AUDIO_CUE_VOLUME_KEY = "wattracker.audioCueVolume"

# Replaces WebAudio with a recorder BEFORE any page script runs, so the gain
# the ride page actually programs onto its GainNode is observable.
#
# WHAT THIS PROVES: that the number the volume control stores is the number
# the ride page feeds to `GainNode.gain.setValueAtTime()` for a real cue,
# through the page's real `playCue`/`playTone`/`getCueGain` code, in a real
# browser, after a real navigation. That is the whole chain from the slider to
# the audio graph.
#
# WHAT IT DOES NOT PROVE: that any sound leaves the speakers, that the level is
# perceptually right, or that Chromium's real AudioContext honours the value -
# headless Chromium has no audio device, and no DOM API reports rendered
# loudness. The last hop (a genuine GainNode multiplying a genuine oscillator
# by the value it was handed) is WebAudio's own contract, not ours. Everything
# on our side of that contract is exercised here.
_AUDIO_RECORDER_JS = """
(() => {
  window.__cueGains = [];
  window.__cueRamps = [];
  function FakeAudioContext() {
    this.state = 'running';
    this.currentTime = 0;
    this.destination = {};
  }
  FakeAudioContext.prototype.resume = function () {};
  FakeAudioContext.prototype.createOscillator = function () {
    return {frequency: {}, connect() {}, start() {}, stop() {}};
  };
  FakeAudioContext.prototype.createGain = function () {
    return {
      gain: {
        setValueAtTime(value) { window.__cueGains.push(value); },
        exponentialRampToValueAtTime(value) { window.__cueRamps.push(value); },
      },
      connect() {},
    };
  };
  window.AudioContext = FakeAudioContext;
  window.webkitAudioContext = FakeAudioContext;
})();
"""


def _fire_a_ride_cue(page, live_server):
    """Navigate to /ride and make it play one cue; return the gain it used.

    The Scan button is the shortest real path into `playCue()` that needs no
    hardware and no running ride: its click handler sounds the "scan" cue
    before it does anything else. The button is rendered disabled on a machine
    with no Bluetooth adapter (which is every CI box and this fixture), so it
    is re-enabled first - the handler itself has no availability guard - and
    the scan request is stubbed so nothing here depends on the BLE stack.
    """
    page.route(
        "**/ride/scan",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"available": True, "devices": []}),
        ),
    )
    page.goto(f"{live_server.base}/ride")
    page.wait_for_load_state("networkidle")
    page.evaluate(
        "() => { const b = document.getElementById('scanBtn');"
        " b.disabled = false; b.click(); }"
    )
    _wait_for(page, "window.__cueGains.length > 0")
    # The first programmed value is the cue's level; playTone may follow it
    # with the fade-out's target, which is not the volume under test.
    return page.evaluate("() => window.__cueGains[0]")


def test_settings_persists_the_audio_volume_only_when_the_rider_moves_it(
        page, live_server, console_errors):
    """Opening Settings must write nothing; moving the slider must write.

    Merely rendering the page used to store the default, which silently turned
    "no preference" into a pinned choice: every rider who had ever opened
    Settings stopped tracking the app's default, so raising or lowering that
    default later reached new riders only. An unset value has to stay unset.
    """
    page.goto(f"{live_server.base}/settings")
    page.wait_for_load_state("networkidle")

    stored = page.evaluate(
        "(key) => localStorage.getItem(key)", AUDIO_CUE_VOLUME_KEY)
    assert stored is None, (
        f"opening /settings wrote {stored!r} to localStorage; an untouched "
        "control must leave the rider tracking the server default")
    # The control still shows the default it declined to persist.
    assert float(page.input_value("#audioCueVolume")) == pytest.approx(
        DEFAULT_AUDIO_CUE_VOLUME)

    # A second visit is no different - this is not a first-load-only quirk.
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.evaluate(
        "(key) => localStorage.getItem(key)", AUDIO_CUE_VOLUME_KEY) is None

    # Now actually move it. Arrow keys on a focused range input are a genuine
    # user gesture: they change the value and fire `input`, exactly as dragging
    # does, and they step by the input's own `step`, so the result is exact.
    before = float(page.input_value("#audioCueVolume"))
    page.focus("#audioCueVolume")
    page.keyboard.press("ArrowRight")
    _wait_for(page, f"localStorage.getItem({AUDIO_CUE_VOLUME_KEY!r}) !== null")

    written = float(page.evaluate(
        "(key) => localStorage.getItem(key)", AUDIO_CUE_VOLUME_KEY))
    assert written > before, "moving the slider up must store a higher level"
    assert written == pytest.approx(float(page.input_value("#audioCueVolume")))
    # The readout the rider sees agrees with what was stored.
    assert page.text_content("#audioCueVolumeValue").strip() == \
        f"{round(written * 100)}%"

    _assert_clean(console_errors, "/settings audio volume persistence")


def test_ride_cue_gain_falls_back_to_the_server_default_when_unset(
        page, live_server, console_errors):
    """With nothing stored, a real cue is programmed at the server's default.

    This is the half the old source-string assertions could never see:
    `"var CUE_GAIN = 0.24;" in r.text` stays true even if `getCueGain()` stops
    being called, if `playTone` programs a different value, or if the constant
    is shadowed. Here the number is read back off the audio graph the page
    actually built.
    """
    page.add_init_script(_AUDIO_RECORDER_JS)
    page.goto(f"{live_server.base}/ride")
    page.evaluate("(key) => localStorage.removeItem(key)", AUDIO_CUE_VOLUME_KEY)

    gain = _fire_a_ride_cue(page, live_server)
    assert gain == pytest.approx(DEFAULT_AUDIO_CUE_VOLUME), (
        f"a ride cue was programmed at gain {gain}, not the server default "
        f"{DEFAULT_AUDIO_CUE_VOLUME}")

    _assert_clean(console_errors, "/ride default cue gain")


def test_settings_slider_actually_drives_the_ride_cue_gain(
        page, live_server, console_errors):
    """End to end: move the control on /settings, hear it on /ride.

    Deliberately goes through the real slider rather than writing localStorage
    directly. Poking the storage key would test the ride page against a value
    no UI ever produced; driving the control proves the settings page and the
    ride page agree on the key, on the units (linear gain, not a percentage),
    and on the formatting - the three ways this wiring can silently come apart.
    """
    page.add_init_script(_AUDIO_RECORDER_JS)

    page.goto(f"{live_server.base}/settings")
    page.wait_for_load_state("networkidle")
    page.focus("#audioCueVolume")
    # Several steps, so the result cannot coincide with the default and let a
    # test that is in fact reading the fallback pass.
    for _ in range(12):
        page.keyboard.press("ArrowRight")
    _wait_for(page, f"localStorage.getItem({AUDIO_CUE_VOLUME_KEY!r}) !== null")
    chosen = float(page.evaluate(
        "(key) => localStorage.getItem(key)", AUDIO_CUE_VOLUME_KEY))
    assert chosen != pytest.approx(DEFAULT_AUDIO_CUE_VOLUME)

    gain = _fire_a_ride_cue(page, live_server)
    assert gain == pytest.approx(chosen), (
        f"the rider set the volume to {chosen} but the ride page programmed "
        f"its cue at {gain}")

    _assert_clean(console_errors, "/ride cue gain from the settings slider")


def test_muting_the_slider_programs_silence_without_an_illegal_ramp(
        page, live_server, console_errors):
    """Volume 0 must mute the cue AND skip the exponential fade-out.

    Not a nitpick: `exponentialRampToValueAtTime` is undefined for a curve that
    starts at zero, and a real WebAudio implementation throws on it. The ride
    page therefore branches on `cueGain > 0` and sets a flat 0 instead. That
    branch was previously "covered" by asserting the string `if (cueGain > 0)`
    appeared in the template, which would have stayed green had the branch
    been inverted, dead, or reading a different variable. Here the mute is
    driven from the real control and the audio graph is inspected for whether
    the illegal call was made.
    """
    page.add_init_script(_AUDIO_RECORDER_JS)

    page.goto(f"{live_server.base}/settings")
    page.wait_for_load_state("networkidle")
    page.focus("#audioCueVolume")
    page.keyboard.press("Home")  # a range input's Home key jumps to its min
    _wait_for(page, f"localStorage.getItem({AUDIO_CUE_VOLUME_KEY!r}) !== null")
    assert float(page.evaluate(
        "(key) => localStorage.getItem(key)", AUDIO_CUE_VOLUME_KEY)) == 0.0

    gain = _fire_a_ride_cue(page, live_server)
    assert gain == 0.0, f"a muted rider still got a cue at gain {gain}"
    ramps = page.evaluate("() => window.__cueRamps")
    assert ramps == [], (
        "a muted cue still scheduled an exponential ramp "
        f"({ramps}); WebAudio rejects a ramp from zero, so the cue would "
        "throw rather than be silent")

    _assert_clean(console_errors, "/ride muted cue gain")


# ------------------------------------------------------------- first run
# The one journey in the app that a new user cannot be helped through: they are
# alone with whatever the browser renders. It is also the journey the rest of
# this module cannot exercise, because every other fixture here begins by
# creating an account over HTTP - so a wizard that rendered but did not
# actually advance, or a form that silently failed to sign anyone in, would
# leave the whole suite green. These drive it the way a person would.


@pytest.fixture()
def fresh_server():
    """The app on a real port with an EMPTY database - a just-installed server.

    Deliberately not a variant of `live_server`: that fixture registers an
    account through /register before yielding, which is exactly the state this
    is testing the absence of.
    """
    db.init_db()
    app = create_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture()
def anonymous_page(browser, fresh_server):
    """A browser that has never logged in, pointed at a fresh install."""
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    pg = context.new_page()
    errors = []
    pg._wt_errors = errors
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
          if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("requestfailed",
          lambda r: errors.append(f"requestfailed: {r.url} {r.failure}"))
    try:
        yield pg
    finally:
        context.close()


def test_a_fresh_install_walks_a_real_browser_through_creating_an_account(
    anonymous_page, fresh_server
):
    """Open the app, end up signed in and inside the setup wizard.

    Asserted end to end in the browser because every intermediate step is one
    a person has to survive unaided: the redirect off "/", the form, the
    session that must already exist on the next page, and the wizard picking up
    at step 2 instead of restarting the count.
    """
    page = anonymous_page
    page.goto(fresh_server + "/")
    page.wait_for_load_state("networkidle")

    # Landed in the wizard, not on a login form that cannot work yet.
    assert page.url.endswith("/welcome"), page.url
    assert page.locator("text=Step 1 of 5").is_visible()

    page.fill("input[name=username]", "firstrider")
    page.fill("input[name=password]", "password123")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    # Straight on into the existing wizard, already signed in: no second login,
    # and the nav (which only renders for a session with a username) is there.
    assert page.url.endswith("/setup"), page.url
    assert "/login" not in page.url
    assert page.locator("nav .user").inner_text() == "firstrider"

    # One continuous run: the account was step 1, so weight is step 2 of 5.
    assert page.locator("#setup-progress").inner_text() == "Step 2 of 5"
    assert page.locator('[data-setup-step="1"] h4 span').inner_text() == "2"

    _assert_clean(anonymous_page._wt_errors, "/welcome -> /setup")


def test_the_first_run_wizard_actually_advances_in_the_browser(
    anonymous_page, fresh_server
):
    """The steps really are steps: one shows at a time and Continue moves on.

    Asserted on what the browser does - which section is visible, what the
    progress line says - rather than on the template source, because the step
    JS is the part of this that a source-string test cannot see failing.
    """
    page = anonymous_page
    page.goto(fresh_server + "/welcome")
    page.fill("input[name=username]", "firstrider")
    page.fill("input[name=password]", "password123")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    weight = page.locator('[data-setup-step="1"]')
    folder = page.locator('[data-setup-step="2"]')
    assert weight.is_visible()
    assert folder.is_hidden(), "two wizard steps were on screen at once"

    page.fill("#setup-weight", "72")
    page.locator('[data-setup-step="1"] [data-setup-next]').click()

    assert folder.is_visible()
    assert weight.is_hidden()
    # Numbering stays on the first-run scale after the step change, which is
    # where a naive `index + 1` progress line would silently disagree with the
    # heading beside it.
    assert page.locator("#setup-progress").inner_text() == "Step 3 of 5"
    assert page.locator('[data-setup-step="2"] h4 span').inner_text() == "3"

    _assert_clean(anonymous_page._wt_errors, "first-run wizard step change")


def test_the_wizard_is_gone_once_the_install_has_an_account(
    anonymous_page, fresh_server
):
    """Second visitor, same server: ordinary login, no interception.

    The same browser that was walked through setup above gets the plain login
    page here, because the difference is the state of the install and not
    anything remembered about the client.
    """
    page = anonymous_page
    page.goto(fresh_server + "/welcome")
    page.fill("input[name=username]", "firstrider")
    page.fill("input[name=password]", "password123")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    page.context.clear_cookies()  # the same browser, now with no session
    page.goto(fresh_server + "/")
    page.wait_for_load_state("networkidle")

    assert page.url.endswith("/login"), page.url
    assert page.locator("input[name=password]").is_visible()
    assert "has not been set up yet" not in page.content()
