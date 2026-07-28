#!/usr/bin/env bash
# Clean checkout -> signed, smoke-tested wattracker.app + DMG.
#
#   packaging/build-macos.sh                 # build in a throwaway venv
#   WATTRACKER_BUILD_PYTHON=python3.12 packaging/build-macos.sh
#   WATTRACKER_MACOS_SIGNING_IDENTITY="Developer ID Application: ..." \
#       packaging/build-macos.sh             # Developer ID + notarization
#
# Everything lands in dist/ and release/. The build venv is created under
# build/ so a developer's own .venv is never mutated by a release build.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "this script builds the macOS artifact and must run on macOS" >&2
    exit 1
fi

python_bin="${WATTRACKER_BUILD_PYTHON:-python3}"
# macOS still ships an ancient python3 in /Library/Frameworks on many machines,
# and a `python3 -m venv` that silently builds against 3.8 fails much later with
# an unrecognisable dependency-resolution error. Check it here instead.
if ! "$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "$python_bin is older than 3.10; set WATTRACKER_BUILD_PYTHON to a newer interpreter" >&2
    exit 1
fi

venv="$root/build/macos-build-venv"
arch="$(uname -m)"
name="wattracker-macos-$arch"

echo "==> creating build environment ($python_bin)"
rm -rf "$venv"
"$python_bin" -m venv "$venv"
"$venv/bin/python" -m pip install --disable-pip-version-check --quiet --upgrade pip
# [package] pins PyInstaller; [ble] is what makes the frozen app able to talk to
# a trainer at all, and [dev] provides pytest for the gate below.
"$venv/bin/python" -m pip install --disable-pip-version-check --quiet ".[dev,ble,package]"

echo "==> running the test suite"
"$venv/bin/python" -m pytest -q

echo "==> freezing"
rm -rf "$root/dist/wattracker" "$root/dist/wattracker.app"
"$venv/bin/python" -m PyInstaller --clean --noconfirm packaging/wattracker.spec
test -d "$root/dist/wattracker.app" || { echo "no .app was produced" >&2; exit 1; }

echo "==> signing"
# Signing happens before the smoke test on purpose: macOS validates the code
# signature at exec time, so the artifact that gets smoke-tested must be the
# byte-identical one that ships.
packaging/sign-macos.sh "$root/dist/wattracker.app"

echo "==> smoke-testing the frozen app"
"$venv/bin/python" packaging/smoke_frozen_macos.py "$root/dist/wattracker.app"

echo "==> building the DMG"
rm -rf "$root/release"
mkdir -p "$root/release"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
# ditto, not cp -R: it is the only copy that reliably preserves the symlinks,
# extended attributes and signature of a signed bundle.
ditto "$root/dist/wattracker.app" "$staging/wattracker.app"
ln -s /Applications "$staging/Applications"
hdiutil create -volname wattracker -srcfolder "$staging" -ov -format UDZO \
    -quiet "$root/release/$name.dmg"
# A signed DMG is what the user's browser downloads and Gatekeeper inspects, so
# it is signed too (ad-hoc or Developer ID, same rule as the bundle).
packaging/sign-macos.sh "$root/release/$name.dmg"
shasum -a 256 "$root/release/$name.dmg" | awk -v n="$name.dmg" '{print $1 "  " n}' \
    > "$root/release/$name.dmg.sha256"

echo
echo "artifact: release/$name.dmg"
cat "$root/release/$name.dmg.sha256"
