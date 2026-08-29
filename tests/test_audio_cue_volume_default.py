"""Guard: the audio-cue default volume has exactly ONE source of truth.

The default started life as three independent `0.24` literals - one in
ride.html (the fallback gain the ride page plays cues at), and two in
settings.html (the range input's `value=` attribute and the JS fallback used
when localStorage holds nothing). Three literals is two chances for the
settings page to advertise a level the ride page does not actually play at,
with the whole suite still green, because nothing anywhere compared them.

They are now all rendered from `wattracker.server.DEFAULT_AUDIO_CUE_VOLUME`.
That refactor on its own only moves the problem: nothing stops someone from
typing a literal back into one of the templates tomorrow. So this module is
the actual deliverable, and it checks the two things that together make drift
impossible:

1. Every rendered occurrence agrees with the Python constant TODAY.
2. Changing the Python side moves every rendered occurrence with it. This is
   what catches a re-hardcoded literal: a literal happens to satisfy (1) while
   the constant still reads 0.24, but it cannot satisfy (2).

These are source-level assertions on rendered HTML, which is the right level
for "do these three numbers agree" - the browser-side question of whether that
number reaches an actual audio gain node is covered in tests/test_dom_smoke.py.
"""
import re

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker.ble import devices as bledevices  # noqa: E402
from wattracker.server import (  # noqa: E402
    DEFAULT_AUDIO_CUE_VOLUME,
    create_app,
    templates,
)

_GLOBAL_KEY = "default_audio_cue_volume"

# The three places the default is rendered. Each pattern captures the number.
_RIDE_GAIN_RE = re.compile(r"var CUE_GAIN = ([0-9.]+);")
_SETTINGS_JS_RE = re.compile(r"var DEFAULT_AUDIO_VOLUME = ([0-9.]+);")
_SETTINGS_INPUT_RE = re.compile(
    r'id="audioCueVolume"[^>]*?value="([0-9.]+)"', re.S
)
_SETTINGS_READOUT_RE = re.compile(
    r'<output id="audioCueVolumeValue"[^>]*>(\d+)%</output>'
)


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def _rendered_volumes(client, monkeypatch):
    """The default as it actually reaches the browser, from every render site."""
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))

    ride = client.get("/ride")
    assert ride.status_code == 200, ride.text[:500]
    settings = client.get("/settings")
    assert settings.status_code == 200, settings.text[:500]

    found = {}
    for name, pattern, text in (
        ("ride.html CUE_GAIN", _RIDE_GAIN_RE, ride.text),
        ("settings.html DEFAULT_AUDIO_VOLUME", _SETTINGS_JS_RE, settings.text),
        ("settings.html slider value", _SETTINGS_INPUT_RE, settings.text),
    ):
        match = pattern.search(text)
        assert match, f"could not find the rendered default in {name}"
        found[name] = float(match.group(1))

    readout = _SETTINGS_READOUT_RE.search(settings.text)
    assert readout, "could not find the settings volume readout"
    found["settings.html readout %"] = int(readout.group(1)) / 100.0
    return found


def test_every_rendered_audio_default_matches_the_python_constant(client, monkeypatch):
    _register(client)
    found = _rendered_volumes(client, monkeypatch)
    disagreeing = {
        where: value
        for where, value in found.items()
        if abs(value - DEFAULT_AUDIO_CUE_VOLUME) > 1e-9
    }
    assert not disagreeing, (
        "these rendered audio-cue defaults disagree with "
        f"server.DEFAULT_AUDIO_CUE_VOLUME ({DEFAULT_AUDIO_CUE_VOLUME}): "
        f"{disagreeing}. Render the constant instead of hardcoding a literal."
    )


def test_changing_the_python_constant_moves_every_rendered_default(client, monkeypatch):
    """A re-hardcoded literal passes the equality test above but fails here.

    The templates read the value from the `default_audio_cue_volume` Jinja
    global (published next to `static_url` in server.py, so all dozen-odd
    handlers that re-render settings.html get it for free). Repointing that
    global is the closest a test can get to "someone changed the default"
    without editing source, and every render site must follow it.
    """
    _register(client)
    changed = 0.61  # deliberately unlike 0.24 in every digit
    monkeypatch.setitem(templates.env.globals, _GLOBAL_KEY, changed)

    found = _rendered_volumes(client, monkeypatch)
    stale = {
        where: value for where, value in found.items() if abs(value - changed) > 1e-9
    }
    assert not stale, (
        f"changing the default to {changed} did not reach {stale}; that render "
        "site is still hardcoding its own number and will drift."
    )


def test_no_template_hardcodes_the_audio_default(client, monkeypatch):
    """Belt and braces: the literal must not reappear in the template source.

    Catches the case where a fourth render site is added with a literal - it
    would not be covered by the regexes above, so only a source scan sees it.
    """
    _register(client)
    monkeypatch.setitem(templates.env.globals, _GLOBAL_KEY, 0.61)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))

    for path in ("/ride", "/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert str(DEFAULT_AUDIO_CUE_VOLUME) not in response.text, (
            f"{path} still renders the literal {DEFAULT_AUDIO_CUE_VOLUME} even "
            "though the default was changed - something is hardcoded."
        )
