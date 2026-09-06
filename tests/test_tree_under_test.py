"""The suite must test the checkout it is run from.

`pip install -e .` writes a finder with the *absolute* path of the checkout it
was run in, and that finder answers `import wattracker` from anywhere. So a
suite run in a git worktree -- the normal way two agents work without
corrupting each other's tree -- imported the main checkout's code instead of
the worktree's, and reported its results as though they were about the branch
under test. Every failure it could have caught was invisible, and every pass it
reported was about a different tree.

`pythonpath = ["."]` in `[tool.pytest.ini_options]` is what fixes it: the
editable finder is *appended* to `sys.meta_path`, so the stdlib path finder is
consulted first and the rootdir wins. This test is here because that one line
looks removable to anyone who does not know what it is holding up, and because
the failure it prevents is silent -- a green run against the wrong source is
indistinguishable from a green run against the right one.
"""
from pathlib import Path

import wattracker


def test_wattracker_is_imported_from_this_checkout() -> None:
    rootdir = Path(__file__).resolve().parent.parent
    imported = Path(wattracker.__file__).resolve()
    assert imported.is_relative_to(rootdir), (
        f"tests import {imported}, which is outside this checkout ({rootdir}). "
        "The editable install is answering instead of the rootdir -- check that "
        'pythonpath = ["."] is still set in [tool.pytest.ini_options].'
    )
