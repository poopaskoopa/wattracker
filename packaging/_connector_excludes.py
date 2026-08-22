"""What the frozen connector must not contain, defined exactly once.

The connector is deliberately tiny. It runs on the machine where Zwift is
installed and does three things - read `.fit` files, write `.zwo` files, talk
BLE to the trainer - so none of the analysis stack and none of the web stack
belongs in its executable.

Two things enforce that, and they must agree:

* ``tests/test_connector_client.py`` imports the connector in a clean
  interpreter and fails if any of these appear in ``sys.modules``;
* ``packaging/wattracker-connector.spec`` passes this list to PyInstaller's
  ``excludes``.

Kept in one place because two lists that "happen to agree" stop agreeing. The
expensive direction is the test drifting *ahead* of the spec: the test would
keep passing while the exclude list silently stopped matching what the code
actually imports, and the first sign would be a frozen artifact that is either
four times the size it should be or broken at runtime on a rider's machine.

Loaded **by path**, never by import: this directory is called ``packaging``,
which is also a widely installed PyPI distribution.
"""
from __future__ import annotations

FORBIDDEN = [
    "numpy", "pandas", "scipy", "fastapi", "starlette", "uvicorn",
    "anthropic", "jinja2", "matplotlib", "fitdecode", "keyring",
    "wattracker.db", "wattracker.server", "wattracker.ingest",
]
