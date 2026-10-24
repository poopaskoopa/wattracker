"""Run git describe using only desktop tags that satisfy the release contract."""
from __future__ import annotations

import re
import subprocess
import sys

TAG = re.compile(
    r"^(?:v|preview-)(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)(?:-(?:a|b|rc)\d+)?$"
)


def main() -> int:
    tags = subprocess.run(
        ["git", "tag", "--merged", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    matches = [tag for tag in tags if TAG.fullmatch(tag)]
    command = ["git", "describe", "--dirty", "--tags", "--long"]
    if not matches:
        matches = ["__no_desktop_version_tag__"]
    for tag in matches:
        command.extend(("--match", tag))
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        sys.stderr.write(result.stderr)
        return result.returncode
    sys.stdout.write(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
