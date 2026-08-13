"""The project version, read from pyproject, for every spec that needs it.

Two specs need this and a third thing might later, so it lives here rather
than being copied. A copy would not stay a copy: the failure mode is a bundle
whose declared version quietly disagrees with the wheel's, which nothing
notices until someone tries to work out which build a bug report came from.

Parsed with a regex rather than tomllib because the build interpreter is only
required to be >=3.10 and tomllib arrived in 3.11.

Loaded **by path**, never by import: this directory is called ``packaging``,
which is also a widely installed PyPI distribution, so ``import packaging``
resolves to that one. See the loader in either spec.
"""
from __future__ import annotations

import re


def project_version(root) -> str:
    """Version string from ``pyproject.toml`` under ``root``."""
    from pathlib import Path

    text = (Path(root) / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("could not read version from pyproject.toml")
    return match.group(1)
