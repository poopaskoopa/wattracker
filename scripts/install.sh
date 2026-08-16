#!/usr/bin/env bash
# Create the local environment and install wattracker from this checkout.
# The application environment stays in the checkout; this never uses sudo or
# performs a global pip install.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${WATTRACKER_PYTHON:-python3}"
VENV="$ROOT/.venv"
VENV_PYTHON="$VENV/bin/python"
MARKER="$VENV/.wattracker-installed"

if [ -x "$VENV_PYTHON" ]; then
    if ! "$VENV_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
        echo "The existing environment needs Python 3.12 or newer: $VENV_PYTHON" >&2
        exit 1
    fi
else
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
        echo "Python was not found: $PYTHON" >&2
        echo "Install Python 3.12 or newer, or set WATTRACKER_PYTHON to its path." >&2
        exit 1
    fi

    if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
        echo "wattracker requires Python 3.12 or newer: $PYTHON" >&2
        exit 1
    fi

    echo "Creating local virtual environment: $VENV"
    "$PYTHON" -m venv "$VENV"
fi

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Virtual environment was not created: $VENV_PYTHON" >&2
    exit 1
fi

echo "Installing wattracker into the local virtual environment..."
"$VENV_PYTHON" -m pip install --disable-pip-version-check -e .

# Write this only after pip exits successfully. start.sh uses the marker to
# avoid a network operation on every launch and invalidates it when the
# project metadata or this installer changes.
printf 'python=%s\ninstalled=%s\n' \
    "$($VENV_PYTHON --version)" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$MARKER"
echo "Ready. Run ./start.sh to launch wattracker."
