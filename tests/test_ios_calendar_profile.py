from pathlib import Path


CALENDAR_SCREEN = Path(__file__).parents[1] / "ios/WatTracker/WatTracker/Screens/CalendarScreen.swift"


def test_calendar_day_has_an_explicit_back_to_month_toolbar_button():
    source = CALENDAR_SCREEN.read_text(encoding="utf-8")
    assert "@Environment(\\.dismiss) private var dismiss" in source
    assert "ToolbarItem(placement: .topBarLeading)" in source
    assert ".accessibilityLabel(\"Back to calendar\")" in source
