"""Opt-in probe for finding tests that will expire as the wall clock advances.

NOT loaded by default -- it is a plugin, not a conftest, so it only runs when
asked for explicitly:

    pytest tests -p tests.clockshift_probe --clock-days=30
    pytest tests -p tests.clockshift_probe --clock-year=2035

Why this exists: several tests seed data at hardcoded absolute dates and then
exercise a code path that resolves "now" for itself (a trailing-90-day FTP
window, a +-30/+180-day calendar window, a 365-day HRmax window). Those tests
pass today and start failing on a date nobody chose, with no code change. This
probe pretends the system clock has moved so that class of rot shows up now.

The guiding rule is: shift everything that *means* "now", in the app and in the
tests alike, and leave written-down dates alone. What survives that is exactly
the defect being hunted -- a hardcoded absolute date meeting a now-relative
computation with no clock freeze. Anything a test derives from "now" moves with
the app and stays self-consistent, so correct-by-construction tests stay green.

Two clocks therefore get shifted:

1. The app's canonical clock (``wattracker.timeutil.utc_now`` / ``utc_today``),
   rebound in EVERY module that imported it by value -- test modules included.
2. The stdlib entry points, ``datetime.now`` / ``utcnow`` / ``date.today``.
   These cannot be patched on the C types themselves, so each module's *alias*
   for the datetime module (the near-universal ``import datetime as dt``) is
   swapped for a shim exposing shifted ``datetime`` and ``date`` classes, and a
   bare ``from datetime import datetime`` binding is swapped for the shifted
   class directly.

Point 2 is what keeps the signal clean. Without it a test that seeds via
``dt.date.today()`` while the app windows via the shifted ``utc_today()`` fails
under the probe for no reason but the split clock -- an artifact, not an
expiry. Those false positives used to have to be triaged by hand; now they do
not arise, because both halves move together.

Reading results: a failure under this probe means the test pins an absolute
date that feeds a now-relative computation and never freezes the clock. Fix it
by freezing the clock (follow whatever convention the file already uses), not
by widening a tolerance -- widening only resets the fuse.
"""
import datetime as _dt
import sys
import types

import pytest

_YEAR = None


_DAYS = 0


# Captured before anything is shimmed, so the shifted clock is always computed
# from the true system clock. Deriving it from the patched entry points instead
# would compound the offset every time one shifted value fed another.
_RAW_NOW = _dt.datetime.now
_RAW_TODAY = _dt.date.today


def pytest_addoption(parser):
    parser.addoption("--clock-year", type=int, default=0)
    parser.addoption("--clock-days", type=int, default=0)


def pytest_configure(config):
    global _YEAR, _DAYS
    _YEAR = config.getoption("--clock-year")
    _DAYS = config.getoption("--clock-days")


def _shift(value):
    """Apply the configured year pin and day offset to a date or datetime."""
    if _YEAR:
        value = value.replace(year=_YEAR)
    if _DAYS:
        value = value + _dt.timedelta(days=_DAYS)
    return value


class _ShiftedDatetime(_dt.datetime):
    """``datetime`` whose notion of *now* is the shifted one.

    Only the three "what time is it" constructors are overridden. Everything
    else -- parsing, arithmetic, comparison -- is inherited unchanged, so a
    written-down date built through this class is still exactly that date.
    """

    @classmethod
    def now(cls, tz=None):
        return _shift(_RAW_NOW(tz))

    @classmethod
    def utcnow(cls):
        return _shift(_RAW_NOW(_dt.timezone.utc).replace(tzinfo=None))

    @classmethod
    def today(cls):
        return _shift(_RAW_NOW())


class _ShiftedDate(_dt.date):
    @classmethod
    def today(cls):
        return _shift(_RAW_TODAY())


def _make_shim():
    """A stand-in for the ``datetime`` module with the two classes swapped.

    The stdlib classes are C types, so ``datetime.datetime.now`` cannot be
    reassigned. Swapping each module's *reference* to the datetime module is
    the way in that does not require touching the originals.
    """
    shim = types.ModuleType("datetime")
    for name in dir(_dt):
        setattr(shim, name, getattr(_dt, name))
    shim.datetime = _ShiftedDatetime
    shim.date = _ShiftedDate
    return shim


@pytest.fixture(autouse=True)
def _shift_clock(monkeypatch):
    if not (_YEAR or _DAYS):
        yield
        return

    import wattracker.timeutil as timeutil

    real_now = timeutil.utc_now
    real_today = timeutil.utc_today

    def fake_now():
        return _shift(_RAW_NOW(_dt.timezone.utc).replace(tzinfo=None))

    def fake_today():
        return fake_now().date()

    shim = _make_shim()

    # Held as locals, not read from this module's globals, because the sweep
    # below would otherwise rewrite the very names it compares against: this
    # plugin's own module name starts with "test", so swapping its `_dt` to the
    # shim mid-loop would leave every later module compared against the shim
    # and silently skipped.
    real_dt = sys.modules["datetime"]
    real_datetime = real_dt.datetime
    real_date = real_dt.date

    monkeypatch.setattr(timeutil, "utc_now", fake_now)
    monkeypatch.setattr(timeutil, "utc_today", fake_today)
    for name, mod in list(sys.modules.items()):
        if mod is None or mod is sys.modules[__name__] or not (
            name.startswith("wattracker") or name.startswith("test")
        ):
            continue
        for attr, real, fake in (
            ("utc_now", real_now, fake_now),
            ("utc_today", real_today, fake_today),
            ("utcnow", real_now, fake_now),
        ):
            try:
                if getattr(mod, attr, None) is real:
                    monkeypatch.setattr(mod, attr, fake)
            except Exception:
                pass
        # `import datetime as dt` / `import datetime`: swap the module alias.
        # `from datetime import datetime, date`: swap the class binding.
        for attr, original, replacement in (
            (None, real_dt, shim),
            ("datetime", real_datetime, _ShiftedDatetime),
            ("date", real_date, _ShiftedDate),
        ):
            try:
                if attr is None:
                    for alias, value in list(vars(mod).items()):
                        if value is original:
                            monkeypatch.setattr(mod, alias, replacement)
                elif getattr(mod, attr, None) is original:
                    monkeypatch.setattr(mod, attr, replacement)
            except Exception:
                pass
    yield
