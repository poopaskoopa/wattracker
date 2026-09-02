#!/usr/bin/env python3
"""Stage a self-contained Azure Functions budget-hook project.

Azure Functions publishes the contents of the function project, not its
parent checkout.  The hook therefore gets the small cloud package it imports
copied into the staging project before Core Tools performs its normal remote
dependency build.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FUNCTION_PROJECT = REPOSITORY_ROOT / "infra" / "azure" / "budget-hook"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "build" / "azure-budget-hook"


def _ignore_generated(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name == "__pycache__"
        or name == ".python_packages"
        or name.endswith((".pyc", ".pyo"))
    }


def stage_budget_hook(output: Path = DEFAULT_OUTPUT) -> Path:
    """Create a publishable hook project and return its path.

    The output must not already exist.  Refusing to overwrite it keeps a
    caller from accidentally deleting an arbitrary directory when a path is
    mistyped; remove the generated ``build/azure-budget-hook`` directory
    explicitly before restaging.
    """
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(
            f"output already exists: {output}; remove the generated staging directory first"
        )
    if not FUNCTION_PROJECT.is_dir():
        raise FileNotFoundError(f"Function project is missing: {FUNCTION_PROJECT}")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        shutil.copytree(
            FUNCTION_PROJECT,
            temporary,
            dirs_exist_ok=True,
            ignore=_ignore_generated,
        )
        shutil.copytree(
            REPOSITORY_ROOT / "wattracker",
            temporary / "wattracker",
            ignore=_ignore_generated,
        )
        (temporary / "wattracker" / "cloud").is_dir() or _raise_missing_cloud()
        temporary.rename(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output


def _raise_missing_cloud() -> None:
    raise FileNotFoundError("repository cloud package is missing")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"staging directory (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args(argv)
    staged = stage_budget_hook(args.output)
    print(staged)
    print("Publish from this directory with: func azure functionapp publish APP_NAME")
    return 0


if __name__ == "__main__":
    sys.exit(main())
