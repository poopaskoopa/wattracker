"""PyInstaller entry point for the connector.

A script of its own rather than the package's ``__main__.py``, and this is not
a style choice. PyInstaller runs whatever file it is given as the top-level
``__main__``, with no package around it, so the relative imports at the head of
``wattracker_connector/__main__.py`` - ``from .client import ...`` - raise
ImportError before a single line of it runs. The result is a binary that cannot
start at all, and a windowed build reports that behind a modal dialog nobody
can see, which is how it survives a smoke run as a hang rather than a failure.

``wattracker.spec`` has always pointed at ``wattracker_entry.py`` for the same
reason. This is that arrangement, for the other half.
"""
from wattracker_connector.__main__ import main

raise SystemExit(main())
