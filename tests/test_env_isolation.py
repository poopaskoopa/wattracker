"""The suite must not inherit the developer's environment.

`WATTRACKER_COOKIE_SECURE` was read by the app and never cleared by
`conftest.isolated_env`. Exported, it made every authenticated TestClient
request look signed out: 85 failures in two files alone, none of which named
the cause. It was fixed in PR #261 by adding one name to a hand-maintained
tuple -- and a hand-maintained tuple is exactly why it was missing, since
`WATTRACKER_PUBLIC_HOST` had been added there while its plural sibling
`WATTRACKER_PUBLIC_HOSTS`, feeding the same allowlist, had not.

This test is the thing that keeps the list honest: every environment variable
the application reads must be either set or cleared by the fixture, or listed
below with a reason. Adding a new `os.environ.get` to the app and forgetting
the fixture now fails here rather than surfacing later as an unrelated-looking
flake on one machine.
"""

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APP = _ROOT / "wattracker"
_CONFTEST = _ROOT / "tests" / "conftest.py"

# Read by the app, deliberately NOT isolated. Each entry needs a reason: the
# point of this test is that "not isolated" is a decision someone made, not an
# oversight nobody noticed.
_NOT_ISOLATED = {
    "USERNAME": (
        "Supplied by the OS, not by this project, and on Windows it is the "
        "real account name that config.py falls back to. Clearing it would "
        "change behaviour on the Windows runner rather than isolate it."
    ),
    "WATTRACKER_OPEN_BROWSER": (
        "Read only by __main__ when launching the app, which no test path "
        "reaches. Nothing under test opens a browser."
    ),
}

# The app reads its environment exactly one way: os.environ.get("NAME", ...)
# with a double-quoted literal. Verified at the time of writing -- there is no
# os.getenv(, no os.environ[, and no single-quoted form anywhere in
# wattracker/. If one appears, this pattern will not see it and this guard
# will quietly under-report, so widen it rather than adding the exception.
_ENV_READ = re.compile(r'os\.environ\.get\(\s*"([A-Z_][A-Z0-9_]*)"')


def _vars_read_by_app():
    found = {}
    for path in sorted(_APP.rglob("*.py")):
        for name in _ENV_READ.findall(path.read_text()):
            found.setdefault(name, path.relative_to(_ROOT))
    return found


def _names_handled_by_conftest():
    """Every string literal in conftest.py.

    Deliberately broad: a name that appears anywhere in that file has been
    considered by someone. Parsing the AST rather than the raw text means a
    name mentioned only in a comment does not count as handled.
    """
    tree = ast.parse(_CONFTEST.read_text())
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_every_env_var_the_app_reads_is_isolated_or_exempt():
    handled = _names_handled_by_conftest()
    unguarded = {
        name: path
        for name, path in _vars_read_by_app().items()
        if name not in handled and name not in _NOT_ISOLATED
    }
    assert not unguarded, (
        "these environment variables are read by the app but neither handled "
        "by conftest.isolated_env nor listed in _NOT_ISOLATED:\n"
        + "\n".join(f"  {name}  ({path})" for name, path in sorted(unguarded.items()))
        + "\n\nExported by a developer, each one silently changes behaviour for "
        "every test in the run. Add it to the delenv tuple in "
        "tests/conftest.py, or to _NOT_ISOLATED here with the reason it is "
        "safe to inherit."
    )


def test_exemptions_are_still_read_by_the_app():
    """A stale exemption is a claim nobody is checking any more."""
    read = _vars_read_by_app()
    stale = sorted(name for name in _NOT_ISOLATED if name not in read)
    assert not stale, (
        f"_NOT_ISOLATED names no longer read by the app: {stale}. "
        "Remove them so the list keeps meaning what it says."
    )


def test_exemptions_carry_a_reason():
    missing = sorted(k for k, v in _NOT_ISOLATED.items() if not v or not v.strip())
    assert not missing, f"exemptions without a reason: {missing}"
