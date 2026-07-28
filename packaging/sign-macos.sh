#!/usr/bin/env bash
# Sign and verify a frozen wattracker.app.
#
# Two paths, chosen by WATTRACKER_MACOS_SIGNING_IDENTITY:
#
#   unset  ->  ad-hoc (`codesign -s -`). The bundle gets a valid, verifiable
#              signature bound to nothing. It is enough for the app to run on
#              the machine that built it and to keep the code-signature
#              validation macOS does at exec time happy, but Gatekeeper will
#              still quarantine it after a download. This is what this
#              repository can actually produce today.
#
#   set    ->  Developer ID: hardened runtime, secure timestamp, entitlements,
#              then notarization + stapling when notary credentials are also
#              present. This is the distributable path, and it fails closed:
#              anything set but broken aborts rather than degrading to ad-hoc,
#              in the same spirit as sign-windows.ps1 refusing to emit an
#              unsigned artifact.
#
# Usage: packaging/sign-macos.sh dist/wattracker.app [more paths...]
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "usage: $0 <path-to-sign> [more paths...]" >&2
    exit 2
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
entitlements="$here/macos-entitlements.plist"
identity="${WATTRACKER_MACOS_SIGNING_IDENTITY:-}"

sign_adhoc() {
    # --deep is deprecated for signing, but a PyInstaller bundle is a tree of
    # dozens of nested dylibs and .so files and there is no manifest of them
    # here; re-signing the lot in one pass is the honest option. The Developer
    # ID path below signs nested code first and the bundle last, which is the
    # order Apple documents.
    codesign --force --deep --sign - --timestamp=none "$1"
    codesign --verify --deep --strict --verbose=2 "$1"
    echo "Ad-hoc signed and verified: $1"
    echo "NOTE: ad-hoc signatures do not pass Gatekeeper on a downloaded copy."
}

sign_developer_id() {
    local target="$1"
    if [ ! -f "$entitlements" ]; then
        echo "missing entitlements file: $entitlements" >&2
        exit 1
    fi
    # Inside-out: every nested Mach-O first, the bundle itself last. Signing the
    # outer bundle before its contents produces a signature that verifies today
    # and breaks the moment anything nested is touched.
    while IFS= read -r -d '' nested; do
        codesign --force --sign "$identity" --options runtime --timestamp \
            --entitlements "$entitlements" "$nested"
    done < <(find "$target/Contents" -type f \( -name "*.dylib" -o -name "*.so" \) -print0)
    codesign --force --sign "$identity" --options runtime --timestamp \
        --entitlements "$entitlements" "$target"

    codesign --verify --deep --strict --verbose=2 "$target"
    # A hardened-runtime flag that silently failed to apply is the classic way a
    # notarization submission gets rejected hours later; assert it now.
    if ! codesign --display --verbose=2 "$target" 2>&1 | grep -q "flags=.*runtime"; then
        echo "hardened runtime was not applied to $target" >&2
        exit 1
    fi
    echo "Developer ID signed and verified: $target"
    notarize "$target"
}

# Notary credentials deliberately have no "--apple-id/--team-id/--password"
# form. argv is world-readable to every process on the machine (`ps -ww`), so an
# app-specific password passed that way is disclosed to any other local process
# for the lifetime of the submission - which for `--wait` is minutes. That is
# exactly the bar docs/windows-security.md holds sign-windows.ps1 to when it
# refuses to put the PFX password on a child process command line. Both accepted
# forms keep the secret out of argv: a keychain profile keeps it in the keychain,
# and an App Store Connect API key keeps it in a file whose path is not secret.
notarize() {
    local target="$1"
    local -a creds=()
    if [ -n "${WATTRACKER_MACOS_NOTARY_PROFILE:-}" ]; then
        # Created out of band by `xcrun notarytool store-credentials`, which
        # prompts for the password on a tty. Preferred for local releases.
        creds=(--keychain-profile "$WATTRACKER_MACOS_NOTARY_PROFILE")
    elif [ -n "${WATTRACKER_MACOS_NOTARY_KEY_PATH:-}" ] &&
         [ -n "${WATTRACKER_MACOS_NOTARY_KEY_ID:-}" ] &&
         [ -n "${WATTRACKER_MACOS_NOTARY_ISSUER:-}" ]; then
        # App Store Connect API key. The only secret is the .p8 file itself;
        # the key id and issuer uuid are identifiers, not credentials. This is
        # the form CI uses, because a keychain profile cannot be provisioned
        # non-interactively.
        if [ ! -f "$WATTRACKER_MACOS_NOTARY_KEY_PATH" ]; then
            echo "notary key file not found: $WATTRACKER_MACOS_NOTARY_KEY_PATH" >&2
            exit 1
        fi
        creds=(--key "$WATTRACKER_MACOS_NOTARY_KEY_PATH"
               --key-id "$WATTRACKER_MACOS_NOTARY_KEY_ID"
               --issuer "$WATTRACKER_MACOS_NOTARY_ISSUER")
    else
        echo "NOTE: no notary credentials set; $target is signed but NOT notarized." >&2
        echo "      Gatekeeper will still refuse it on another Mac." >&2
        return 0
    fi
    # notarytool only accepts a zip, dmg or pkg - never a bare .app directory.
    # ditto -c -k --keepParent is the archiver Apple documents for this; plain
    # `zip` mangles the symlinked framework layout and invalidates the
    # signature before the service ever sees it.
    local archive
    archive="$(mktemp -d)/$(basename "$target").zip"
    ditto -c -k --keepParent "$target" "$archive"
    xcrun notarytool submit "$archive" "${creds[@]}" --wait
    # Stapling the .app (not the zip) so the ticket travels inside the bundle
    # and the DMG built afterwards carries it.
    xcrun stapler staple "$target"
    xcrun stapler validate "$target"
    spctl --assess --type execute --verbose=2 "$target"
    rm -rf "$(dirname "$archive")"
    echo "Notarized and stapled: $target"
}

for target in "$@"; do
    if [ ! -e "$target" ]; then
        echo "no such path: $target" >&2
        exit 1
    fi
    if [ -n "$identity" ]; then
        sign_developer_id "$target"
    else
        sign_adhoc "$target"
    fi
done
