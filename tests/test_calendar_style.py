"""Focused regressions for the calendar's token-driven visual contract."""

import pathlib
import re


CSS = pathlib.Path("wattracker/web/static/style.css")
TEMPLATE = pathlib.Path("wattracker/web/templates/calendar.html")


def _calendar_css():
    css = CSS.read_text()
    return css.split("/* Calendar */", 1)[1].split(
        "/* Adaptive training status banner */", 1
    )[0]


def _rule(css, selector):
    return css.split(selector + " {", 1)[1].split("}", 1)[0]


def test_calendar_status_colors_are_derived_from_tokens():
    css = _calendar_css()

    assert "rgba(" not in css
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)
    for token in ("accent", "ok", "alert"):
        assert f"color-mix(in srgb, var(--{token})" in css
    assert re.search(
        r"repeating-linear-gradient\(45deg,\s*"
        r"color-mix\(in srgb, var\(--alert\) 18%, transparent\)",
        css,
    )


def test_calendar_rhythm_uses_tokens_without_changing_footprint():
    css = _calendar_css()

    assert "height: 110px" in _rule(css, ".cal-cell")
    assert "grid-template-columns: minmax(0, 1fr) 280px" in css
    assert ".cal-main { min-width: 0; overflow-x: auto; }" in css
    assert "padding: calc(var(--sp-1) * .75)" in _rule(css, ".cal-cell")
    assert "margin-top: calc(var(--sp-1) / 2)" in _rule(css, ".cal-workout")
    assert "calc(var(--sp-1) * 1.5)" in _rule(css, ".cal-cell.cal-ooto-day")
    assert "calc(var(--sp-1) * 3)" in _rule(css, ".cal-cell.cal-ooto-day")
    shared = _rule(
        css, ".cal-ooto-tag, .cal-race-tag, .cal-phase-tag, .cal-rpe"
    )
    assert "border-radius: var(--r-sm)" in shared
    assert "padding: 0 var(--sp-1)" in shared


def test_calendar_keeps_every_specialized_state_selector():
    css = _calendar_css()
    template = TEMPLATE.read_text()

    selectors = (
        ".cal-cell.other-month",
        ".cal-workout .cal-type",
        ".cal-vo2max, .cal-threshold",
        ".cal-endurance, .cal-recovery",
        ".cal-workout.cal-completed",
        ".cal-check",
        ".cal-workout.cal-adapted",
        ".cal-adapt-mark",
        ".cal-workout.cal-skipped",
        ".cal-skip-mark",
        ".cal-workout.cal-missed",
        ".cal-miss-mark",
        ".cal-cell.cal-ooto-day",
        ".cal-ooto-tag",
        ".cal-cell.cal-race-day",
        ".cal-race-tag",
        ".cal-race-tag.cal-race-A",
        ".cal-race-tag.cal-race-demoted",
        ".cal-race-tag.cal-race-result",
        ".cal-phase-tag",
        ".cal-rpe",
    )
    for selector in selectors:
        assert selector in css
    assert css.index(".cal-workout.cal-completed {") < css.index(
        ".cal-workout.cal-adapted {"
    ) < css.index(".cal-workout.cal-skipped {") < css.index(
        ".cal-workout.cal-missed {"
    )
    assert "color-mix(in srgb, var(--ok) 28%, transparent)" in _rule(
        css, ".cal-workout.cal-completed"
    )
    assert "border-left-style: dashed" in _rule(
        css, ".cal-workout.cal-adapted"
    )
    skipped = _rule(css, ".cal-workout.cal-skipped")
    assert "opacity: 0.6" in skipped
    assert "text-decoration: line-through" in skipped
    missed = _rule(css, ".cal-workout.cal-missed")
    assert "color-mix(in srgb, var(--alert) 10%, transparent)" in missed
    assert "border-left-style: dashed" in missed
    for class_name in (
        "cal-ooto-day",
        "cal-race-day",
        "cal-ooto-tag",
        "cal-race-tag",
        "cal-race-demoted",
        "cal-race-result",
        "cal-phase-tag",
        "cal-completed",
        "cal-adapted",
        "cal-skipped",
        "cal-missed",
        "cal-check",
        "cal-adapt-mark",
        "cal-skip-mark",
        "cal-miss-mark",
        "cal-rpe",
    ):
        assert class_name in template


def test_unknown_workout_type_keeps_accent_fallback():
    css = _calendar_css()
    base = _rule(css, ".cal-workout")

    assert "color-mix(in srgb, var(--accent) 15%, transparent)" in base
    assert "border-left: 3px solid var(--accent)" in base


def test_ooto_race_cascade_keeps_hatch_and_race_treatment():
    css = _calendar_css()
    ooto = _rule(css, ".cal-cell.cal-ooto-day")
    race = _rule(css, ".cal-cell.cal-race-day")

    assert css.index(".cal-cell.cal-ooto-day {") < css.index(
        ".cal-cell.cal-race-day {"
    )
    assert "background-image:" in ooto
    assert "background-color: color-mix(in srgb, var(--ok)" in race
    assert "box-shadow: inset 0 3px 0 var(--ok)" in race
    assert "background:" not in race
    assert "background-image" not in race


def test_calendar_does_not_add_display_or_hidden_contracts():
    css = _calendar_css()
    template = TEMPLATE.read_text()
    uncommented = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

    # These are the pre-existing, load-bearing flex/grid/type-label rules.
    display_selectors = {
        selector.strip()
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", uncommented)
        if re.search(r"\bdisplay\s*:", body)
    }
    assert display_selectors == {
        ".cal-head",
        ".cal-workout .cal-type",
        # The adapted-day swap: old name and arrow each own a line.
        ".cal-was",
        ".cal-was-arrow",
        ".cal-rpe",
        ".rpe-buttons",
        ".cal-layout",
        ".ooto-form",
        ".ooto-form label",
        ".ooto-list li",
    }
    assert "[hidden]" not in css
    for modal_id in (
        "workoutModal",
        "wmError",
        "wmRideWrap",
        "wmComplete",
        "wmProfile",
        "wmRpe",
        "wmTable",
    ):
        assert re.search(rf'id="{modal_id}"[^>]*\bhidden\b', template)
