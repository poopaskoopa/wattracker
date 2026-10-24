"""The wattracker connector: the half that runs where Zwift is installed.

Deliberately a *top-level* package rather than a submodule of ``wattracker``,
because what it must not import matters as much as what it does. It may use
only the dependency-light parts of the main package:

    wattracker.paths           - stdlib only
    wattracker.rpc             - stdlib only
    wattracker.prescribe.zwo   - stdlib only
    wattracker.ble.*           - bleak, and nothing heavier
    wattracker.timeutil        - stdlib only

It must never import ``wattracker.db``, ``wattracker.server``, or anything
that reaches numpy/pandas/scipy/fastapi. That restriction is what keeps the
frozen Windows executable small, and it is enforced by a test rather than left
to good intentions (see tests/test_connector_client.py).
"""
from __future__ import annotations

__all__ = ["__version__"]

try:
    # The frozen connector packages this generated module through the normal
    # import graph, avoiding a dependency on dist-info metadata at runtime.
    from wattracker._scm_version import __version__
except ImportError:
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("wattracker")
    except PackageNotFoundError:
        # Match setuptools-scm's configured fallback without Git or dist-info.
        __version__ = "0.0.0"
