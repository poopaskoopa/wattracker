"""Tests for .zwo XML rendering."""
import xml.etree.ElementTree as ET

from tranalyzer.analysis.state import TrainingState
from tranalyzer.prescribe.planner import plan_workout
from tranalyzer.prescribe import zwo


def _session():
    return plan_workout(TrainingState(ftp=250.0, tsb=0.0), 60)


def test_parses_as_valid_xml():
    xml = zwo.zwo_string(_session())
    root = ET.fromstring(xml)
    assert root.tag == "workout_file"
    assert root.find("sportType").text == "cycling"
    assert root.find("name") is not None
    assert root.find("author") is not None
    assert root.find("workout") is not None


def test_powers_are_ftp_fractions():
    session = _session()
    root = ET.fromstring(zwo.zwo_string(session))
    workout = root.find("workout")
    for el in workout:
        for attr in ("Power", "PowerLow", "PowerHigh", "OnPower", "OffPower"):
            if attr in el.attrib:
                val = float(el.attrib[attr])
                # Fractions of FTP: strictly between 0 and ~2 (never raw watts).
                assert 0.0 < val < 2.0


def test_durations_round_trip():
    session = _session()
    root = ET.fromstring(zwo.zwo_string(session))
    workout = root.find("workout")
    total = 0
    for el in workout:
        if el.tag == "IntervalsT":
            total += int(el.attrib["Repeat"]) * (
                int(el.attrib["OnDuration"]) + int(el.attrib["OffDuration"])
            )
        elif "Duration" in el.attrib:
            total += int(el.attrib["Duration"])
    assert total == session.total_duration()


def test_intervals_element_shape():
    # A plateau state yields VO2max intervals -> exercise IntervalsT rendering.
    session = plan_workout(TrainingState(ftp=250.0, plateau=True), 75)
    root = ET.fromstring(zwo.zwo_string(session))
    iv = root.find("workout").find("IntervalsT")
    assert iv is not None
    for attr in ("Repeat", "OnDuration", "OffDuration", "OnPower", "OffPower"):
        assert attr in iv.attrib
