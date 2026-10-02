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

# A line of its own: `Android: <value>`, optionally after a list marker so it
# can sit in a bulleted summary.
_ANDROID_LINE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?android:[ \t]*(.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# The value either names an issue or PR (`#N`), or is `n/a` and a reason. A
# bare `n/a`, `n/a.` or `n/abc` is not a reason (`n/a, <reason>` is fine), and
# neither is the template's own `<reason>` placeholder.
_ISSUE_REF = re.compile(r"#[0-9]+")
_NOT_APPLICABLE = re.compile(r"n/a[,:;.]?[ \t]+(.+)", re.IGNORECASE)
_WORD = re.compile(r"\S{2,}")
# Text a reader does not see as a statement: HTML comments (the PR template
# explains the format inside one, examples and all) and fenced code blocks.
# An unclosed comment or fence runs to the end of the body, as it renders.
_HIDDEN = re.compile(r"<!--.*?(?:-->|\Z)|^[ \t]*```.*?(?:^[ \t]*```|\Z)", re.DOTALL | re.MULTILINE)

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


def _satisfies(value):
    if _ISSUE_REF.search(value):
        return True
    reason = _NOT_APPLICABLE.fullmatch(value)
    return bool(reason) and reason.group(1) != "<reason>" and bool(_WORD.search(reason.group(1)))


def has_android_line(body):
    visible = _HIDDEN.sub("", (body or "").replace("\r\n", "\n"))
    return any(_satisfies(m.group(1)) for m in _ANDROID_LINE.finditer(visible))


def changed_files(base, head):
    # --no-renames: a file moved out of ios/ must list its old path too, or
    # moving it would be a way past this check.
    result = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", f"{base}...{head}"],
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
