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

It shifts the app's canonical clock (wattracker.timeutil.utc_now / utc_today)
and rebinds it in EVERY module that imported it by value -- including test
modules, so tests that seed data relative to "now" stay self-consistent and
only tests pinned to hardcoded absolute dates change behaviour.

Caveat when reading results: code or tests that call datetime.now() directly
cannot be patched here, so a handful of failures under this probe are artifacts
of the split clock rather than real expiries. Confirm a hit by checking that the
test really does pin an absolute date.
"""
import sys

import pytest

_YEAR = None


_DAYS = 0


def pytest_addoption(parser):
    parser.addoption("--clock-year", type=int, default=0)
    parser.addoption("--clock-days", type=int, default=0)


def pytest_configure(config):
    global _YEAR, _DAYS
    _YEAR = config.getoption("--clock-year")
    _DAYS = config.getoption("--clock-days")


@pytest.fixture(autouse=True)
def _shift_clock(monkeypatch):
    import wattracker.timeutil as timeutil

    real_now = timeutil.utc_now
    real_today = timeutil.utc_today

    def fake_now():
        import datetime as _d

        base = real_now()
        if _YEAR:
            base = base.replace(year=_YEAR)
        return base + _d.timedelta(days=_DAYS)

    def fake_today():
        return fake_now().date()

    monkeypatch.setattr(timeutil, "utc_now", fake_now)
    monkeypatch.setattr(timeutil, "utc_today", fake_today)
    for name, mod in list(sys.modules.items()):
        if mod is None or not (
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
    yield
