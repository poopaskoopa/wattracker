#!/usr/bin/env bash
#
# install.sh — install this repo's git hooks into the current clone.
#
# Hooks live in .git/hooks/, which is not version-controlled, so a fresh clone
# starts with none. Run this once per clone or worktree:
#
#   scripts/hooks/install.sh
#
# Existing hooks are backed up rather than overwritten.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$ROOT/scripts/hooks"
DEST="$(git -C "$ROOT" rev-parse --git-path hooks)"

mkdir -p "$DEST"

for hook in pre-push; do
    src="$SRC/$hook"
    dest="$DEST/$hook"
    [ -f "$src" ] || continue

    if [ -e "$dest" ] && ! cmp -s "$src" "$dest"; then
        backup="$dest.bak-$(date +%s)"
        cp "$dest" "$backup"
        echo "backed up existing $hook -> $backup"
    fi

    cp "$src" "$dest"
    chmod +x "$dest"
    echo "installed $hook -> $dest"
done
