"""A green run must be a run against THIS checkout.

``docs/agent-workflow.md`` lists the trap this file closes: pytest started in a
git worktree without the right ``PYTHONPATH`` silently imports the main
checkout's ``wattracker`` package and passes against code the branch did not
change.  Nothing else in the suite notices, because every assertion is true --
just of the wrong source tree.
"""
from __future__ import annotations

from pathlib import Path

import wattracker


def test_the_imported_package_is_the_one_next_to_this_test_tree():
    expected = Path(__file__).resolve().parents[1] / "wattracker" / "__init__.py"
    imported = Path(wattracker.__file__).resolve()
    assert imported == expected, (
        "pytest is testing a different checkout than the one this test file "
        f"lives in: imported {imported}, expected {expected}. Set PYTHONPATH "
        "to this working tree before believing any result from this run."
    )
