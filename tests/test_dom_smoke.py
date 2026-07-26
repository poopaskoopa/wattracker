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
import socket
import threading
import time

import pytest

pytest.importorskip("httpx")
playwright_api = pytest.importorskip("playwright.sync_api")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from wattracker import db  # noqa: E402
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
                                  "uid": uid})()
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


def test_dashboard_charts_render_without_console_errors(page, live_server,
                                                        console_errors):
    """Both dashboard Chart.js canvases actually paint, and the console is clean."""
    page.goto(f"{live_server.base}/")
    _wait_for_charts(page, "mainChart", "curveChart")

    for canvas_id in ("mainChart", "curveChart"):
        state = _canvas_is_painted(page, canvas_id)
        assert state["ok"], f"#{canvas_id} did not render: {state}"

    _assert_clean(console_errors, "dashboard")


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
