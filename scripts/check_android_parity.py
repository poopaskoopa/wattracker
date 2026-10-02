#!/usr/bin/env python3
"""Fail a pull request that changes shared mobile surface without an Android line.

iOS and Android are two native clients of one cloud read plane. Android kept
drifting behind because keeping it in step depended on someone remembering to
leave a note. This makes the note part of the pull request: a PR that touches
ios/, wattracker/cloud/ or tests/vectors/ must say, in its body, either where
the Android note was posted or why none is needed. docs/mobile-parity.md has
the rules this enforces.

Run by .github/workflows/mobile-parity.yml. The PR body arrives through the
PR_BODY environment variable, never through the workflow's `run:` text, and the
changed files come from `git diff` between BASE_SHA and HEAD_SHA.
"""

import os
import re
import subprocess
import sys

SHARED_PREFIXES = ("ios/", "wattracker/cloud/", "tests/vectors/")

# `Android: n/a <reason>` or `Android: <anything> #N`. A bare `Android: n/a`
# does not count: the reason is the point. A leading list marker is allowed so
# the line can sit in a bulleted summary.
_ANDROID_LINE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?android:[ \t]*(?:n/a[ \t]*\S.*|.*#[0-9]+.*)$",
    re.IGNORECASE | re.MULTILINE,
)
# The PR template explains the format inside an HTML comment, examples and
# all. Those must not satisfy the check on a body nobody filled in.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

FAILURE = """\
This pull request changes files that both mobile clients depend on:
{files}
Its description needs an Android line, on a line of its own, in one of these forms:

  Android: posted on #<N>       (the Android issue or PR that has the note)
  Android: n/a <reason>         (for example: "n/a - server-only refactor, no client-visible change")

Post the note on the specific Android screen issue, not the epic. See
docs/mobile-parity.md for what the note should say. Edit the PR description;
this check re-runs on edit."""


def touches_shared_surface(paths):
    """The changed paths that oblige an Android line, in input order."""
    return [p for p in paths if p.startswith(SHARED_PREFIXES)]


def has_android_line(body):
    return bool(_ANDROID_LINE.search(_HTML_COMMENT.sub("", body or "")))


def changed_files(base, head):
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def check(paths, body):
    """Return a failure message, or None when the pull request passes."""
    shared = touches_shared_surface(paths)
    if not shared or has_android_line(body):
        return None
    listed = shared[:10]
    files = "".join(f"  {p}\n" for p in listed)
    if len(shared) > len(listed):
        files += f"  ... and {len(shared) - len(listed)} more\n"
    return FAILURE.format(files=files)


def main(env=None, paths=None):
    env = os.environ if env is None else env
    if paths is None:
        paths = changed_files(env["BASE_SHA"], env["HEAD_SHA"])
    failure = check(paths, env.get("PR_BODY", ""))
    if failure:
        print("::error title=Android parity line missing::"
              "PR description needs an 'Android:' line; see the log.")
        print(failure)
        return 1
    shared = touches_shared_surface(paths)
    print("Android line present." if shared else "No shared mobile files changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
