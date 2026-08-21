"""Static UI contracts for calendar activities and workout controls."""

import pathlib


CALENDAR = pathlib.Path("wattracker/web/templates/calendar.html")
ACTIVITIES = pathlib.Path("wattracker/web/templates/activities.html")


def test_calendar_actual_activity_is_a_detail_link_without_modal_hooks():
    text = CALENDAR.read_text()
    actual = text.split("{% if w.activity %}", 1)[1].split("{% else %}", 1)[0]

    assert 'href="/activity/{{ w.activity_id }}"' in actual
    assert "Actual activity" in actual
    assert "data-workout-id" not in actual
    assert 'role="button"' not in actual
    assert "{% for a in cell.activities %}" in text
    assert 'href="/activity/{{ a.activity_id or a.id }}"' in text


def test_calendar_completion_control_posts_explicit_boolean_and_supports_both_labels():
    text = CALENDAR.read_text()

    assert "can_toggle_completion" in text
    assert 'detail.completed ? "Mark incomplete" : "Mark complete"' in text
    assert '"/api/plan/workout/" + detail.id + "/completion"' in text
    assert "JSON.stringify({ completed: completed })" in text
    assert "window.location.reload()" in text


def test_activities_drop_control_requires_confirmation_and_explains_links():
    text = ACTIVITIES.read_text()

    assert 'action="/activity/{{ a.id }}/drop"' in text
    assert "confirm('Drop this activity from the calendar?')" in text
    assert "a.linked_workout" in text
    assert "cannot drop" in text
    assert "drop_error" in text
    assert "drop') == 'linked'" in text
