from pathlib import Path


CALENDAR_SCREEN = Path(__file__).parents[1] / "ios/WatTracker/WatTracker/Screens/CalendarScreen.swift"


def test_calendar_day_has_an_explicit_back_to_month_toolbar_button():
    source = CALENDAR_SCREEN.read_text(encoding="utf-8")
    detail = source.split("private struct CalendarDayDetail", 1)[1].split(
        "private struct CalendarWorkoutPanel", 1
    )[0]
    assert "@Environment(\\.dismiss) private var dismiss" in detail
    assert "NavigationStack" in detail
    assert "ToolbarItem(placement: .topBarLeading)" in detail
    assert "dismiss()" in detail
    assert ".accessibilityLabel(\"Back to calendar\")" in detail


def test_calendar_chart_uses_area_fill_and_percent_axis_without_ftp():
    source = CALENDAR_SCREEN.read_text(encoding="utf-8")
    chart = source.split("private struct CalendarWorkoutProfileChart", 1)[1].split(
        "private struct CalendarActivityRow", 1
    )[0]
    assert "AreaMark(" in chart
    assert ".foregroundStyle(Palette.accent.opacity(opacity))" in chart
    assert ".foregroundStyle(Palette.accent)" in chart
    assert '.chartYAxisLabel(hasFTP ? "Power (W)" : "Target (% FTP)")' in chart


def test_calendar_workout_shows_the_ftp_used_for_watts():
    source = CALENDAR_SCREEN.read_text(encoding="utf-8")
    panel = source.split("private struct CalendarWorkoutPanel", 1)[1].split(
        "private struct CalendarWorkoutProfileChart", 1
    )[0]
    assert '"at FTP \\(Int(ftp.rounded(.toNearestOrEven))) W"' in panel
    assert '"calendar-workout-ftp"' in panel
