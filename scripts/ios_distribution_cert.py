#!/usr/bin/env python3
"""Create the Apple Distribution certificate the iOS release job signs with.

Run this once, and again roughly once a year when the certificate expires.
`docs/ios-testflight.md` is the runbook; this file is the mechanism.

WHY THIS EXISTS. The release workflow passes `-allowProvisioningUpdates` and
an App Store Connect API key to `xcodebuild`, and it is tempting to assume that
covers everything Apple needs. It does not: that combination registers App IDs
and creates and downloads provisioning profiles, but it will not issue a
distribution certificate. A certificate is a private key that Apple never sees,
so somebody has to generate the key and the CSR. Without one, automatic signing
finds only the account's development certificates and an archive fails with
either "conflicting provisioning settings" or "has entitlements that require
signing with a development certificate".

WHAT IT DOES, in order:

  1. Generates an RSA 2048 private key, mode 0600, in --out-dir.
  2. Generates a CSR from it.
  3. POSTs the CSR to /v1/certificates as certificateType DISTRIBUTION, signed
     with an ES256 JWT made from the App Store Connect API key.
  4. Converts the returned certificate to PEM and bundles key + certificate +
     the Apple WWDR intermediate into a .p12 under a random 256-bit passphrase.
  5. Optionally creates the App Store provisioning profile the workflow names.

It prints the two `gh secret set` commands to run and never prints the
passphrase, the private key, or the certificate.

WHAT IT DELIBERATELY DOES NOT DO. It does not revoke anything, and it refuses
to create a second certificate unless you pass --force. Apple caps distribution
certificates at a small number per account (two or three), and a spare burned
here is one you cannot create when you need it. Rotation is: create the new
one, put it in the secrets, ship a build with it, and only then revoke the old
one from the developer portal.

USAGE

    export ASC_KEY_ID=...           # the 10-character API key id
    export ASC_ISSUER_ID=...        # the account's issuer UUID
    export ASC_KEY_PATH=~/.appstoreconnect/private_keys/AuthKey_<id>.p8
    python scripts/ios_distribution_cert.py --create-profile

Requires `cryptography` (`pip install cryptography`, or the project's `cloud`
extra) and `openssl` on PATH.

--out-dir must be outside any git work tree; the script checks and refuses.
This repository is public and a distribution private key committed to it is a
key you have to revoke, not a mistake you can amend away.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.appstoreconnect.apple.com"

# The bundle identifier is not an account identifier, so unlike the team id it
# is safe in a public tree - it is already in Config/Base.xcconfig.
BUNDLE_ID = "com.wattracker.ios"
# Must match PROVISIONING_PROFILE_SPECIFIER in .github/workflows/ios-release.yml
# and the provisioningProfiles entry in ios/WatTracker/ExportOptions.plist.
PROFILE_NAME = "WatTracker App Store"


def die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------
# App Store Connect
# --------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_token(key_id: str, issuer_id: str, key_path: pathlib.Path) -> str:
    """An ES256 JWT for the App Store Connect API.

    The one thing that is easy to get wrong: the signature must be the raw
    r||s pair, 32 bytes each for P-256. `cryptography` returns a DER-encoded
    ECDSA signature, and handing that to Apple produces a 401 that says
    nothing about why.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric import utils as asym

    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    now = int(time.time())
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 20 * 60,  # Apple rejects anything over 20 minutes.
        "aud": "appstoreconnect-v1",
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    ).encode("ascii")
    r, s = asym.decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing_input.decode("ascii") + "." + _b64url(raw)


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(API + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        try:
            errors = json.loads(detail)["errors"]
            detail = "; ".join(
                f"{e.get('title')}: {e.get('detail')}" for e in errors
            )
        except Exception:
            pass
        die(f"{method} {path} failed with HTTP {exc.code}: {detail}")


# --------------------------------------------------------------------------
# Local files
# --------------------------------------------------------------------------


def run(*argv: str) -> None:
    """openssl, with output discarded and no shell.

    Nothing here ever takes a passphrase on argv: the .p12 passphrase is
    passed as `file:<path>`, which openssl reads from a 0600 file.
    """
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        die(f"{argv[0]} {argv[1]} failed:\n{result.stderr.strip()}")


def refuse_if_inside_git(out_dir: pathlib.Path) -> None:
    probe = subprocess.run(
        ["git", "-C", str(out_dir), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        die(
            f"--out-dir {out_dir} is inside the git work tree at "
            f"{probe.stdout.strip()}. Choose a directory outside any "
            "repository: this material must never be committable."
        )


def find_wwdr(out_dir: pathlib.Path) -> pathlib.Path | None:
    """The Apple WWDR intermediate, from a local keychain.

    Bundling it into the .p12 means the CI keychain can build the full chain
    from the leaf to the Apple root without depending on what happens to be
    installed on the runner.
    """
    destination = out_dir / "wwdr.pem"
    for keychain in (
        pathlib.Path.home() / "Library/Keychains/login.keychain-db",
        pathlib.Path("/Library/Keychains/System.keychain"),
        pathlib.Path("/System/Library/Keychains/SystemRootCertificates.keychain"),
    ):
        found = subprocess.run(
            [
                "security",
                "find-certificate",
                "-c",
                "Apple Worldwide Developer Relations Certification Authority",
                "-p",
                str(keychain),
            ],
            capture_output=True,
            text=True,
        )
        if found.returncode == 0 and "BEGIN CERT" in found.stdout:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(found.stdout)
            return destination
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=pathlib.Path,
        default=pathlib.Path.home() / ".appstoreconnect",
        help="where the key, certificate and .p12 are written (mode 0600, "
        "outside any git work tree). Default: ~/.appstoreconnect",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="create a certificate even though the account already has a "
        "valid distribution one. Apple's cap is small; be sure.",
    )
    parser.add_argument(
        "--create-profile",
        action="store_true",
        help=f"also create the {PROFILE_NAME!r} App Store provisioning "
        "profile against the new certificate.",
    )
    args = parser.parse_args()

    for name in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_KEY_PATH"):
        if not os.environ.get(name):
            die(f"{name} is not set. See the module docstring.")
    key_id = os.environ["ASC_KEY_ID"]
    issuer_id = os.environ["ASC_ISSUER_ID"]
    asc_key = pathlib.Path(os.environ["ASC_KEY_PATH"]).expanduser()
    if not asc_key.is_file():
        die(f"ASC_KEY_PATH does not exist: {asc_key}")

    out_dir = args.out_dir.expanduser()
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    refuse_if_inside_git(out_dir)

    token = make_token(key_id, issuer_id, asc_key)

    existing = [
        c
        for c in api("GET", "/v1/certificates?limit=200", token).get("data", [])
        if c["attributes"].get("certificateType") == "DISTRIBUTION"
    ]
    if existing and not args.force:
        print("The account already has a distribution certificate:")
        for cert in existing:
            attrs = cert["attributes"]
            print(f"  {cert['id']}  expires {attrs.get('expirationDate')}")
        die(
            "Refusing to create another. Apple caps these at two or three per "
            "account. If this is a rotation, pass --force, ship a build with "
            "the new certificate, and only then revoke the old one."
        )

    key_path = out_dir / "wattracker-dist.key"
    csr_path = out_dir / "wattracker-dist.csr"
    cer_path = out_dir / "wattracker-dist.cer"
    pem_path = out_dir / "wattracker-dist.pem"
    p12_path = out_dir / "wattracker-dist.p12"
    pass_path = out_dir / "wattracker-dist.p12.pass"

    old_umask = os.umask(0o077)
    try:
        run("openssl", "genrsa", "-out", str(key_path), "2048")
        run(
            "openssl", "req", "-new",
            "-key", str(key_path),
            "-out", str(csr_path),
            "-subj", "/CN=WatTracker Distribution/O=WatTracker/C=US",
        )

        # csrContent takes the CSR as its PEM text, BEGIN/END lines and
        # newlines included - not a base64 re-encoding of it.
        created = api(
            "POST",
            "/v1/certificates",
            token,
            {
                "data": {
                    "type": "certificates",
                    "attributes": {
                        "certificateType": "DISTRIBUTION",
                        "csrContent": csr_path.read_text(),
                    },
                }
            },
        )["data"]
        attrs = created["attributes"]
        cer_path.write_bytes(base64.b64decode(attrs["certificateContent"]))
        run(
            "openssl", "x509",
            "-inform", "DER", "-in", str(cer_path),
            "-outform", "PEM", "-out", str(pem_path),
        )

        # 256 bits of passphrase, written to a 0600 file rather than printed
        # or passed on a command line.
        pass_path.write_text(secrets.token_hex(32))

        wwdr = find_wwdr(out_dir)
        export = [
            "openssl", "pkcs12", "-export",
            "-inkey", str(key_path),
            "-in", str(pem_path),
            "-name", "Apple Distribution: WatTracker",
            # macOS's `security import` understands only the legacy PKCS#12
            # algorithms: an OpenSSL 3 default .p12 fails with "MAC
            # verification failed", and one with only -macalg sha1 fixed
            # fails with "Unknown format in import". 3DES for both bags
            # rather than plain -legacy, which would protect the certificate
            # bag with RC2-40.
            "-keypbe", "PBE-SHA1-3DES",
            "-certpbe", "PBE-SHA1-3DES",
            "-macalg", "sha1",
            "-out", str(p12_path),
            "-passout", f"file:{pass_path}",
        ]
        if wwdr is not None:
            export[3:3] = ["-certfile", str(wwdr)]
        run(*export)
    finally:
        os.umask(old_umask)
    for path in (key_path, csr_path, cer_path, pem_path, p12_path, pass_path):
        os.chmod(path, 0o600)

    profile_line = ""
    if args.create_profile:
        profile = api(
            "POST",
            "/v1/profiles",
            token,
            {
                "data": {
                    "type": "profiles",
                    "attributes": {
                        "name": PROFILE_NAME,
                        "profileType": "IOS_APP_STORE",
                    },
                    "relationships": {
                        "bundleId": {
                            "data": {"id": bundle_id_of(token), "type": "bundleIds"}
                        },
                        "certificates": {
                            "data": [{"id": created["id"], "type": "certificates"}]
                        },
                    },
                }
            },
        )["data"]
        profile_line = (
            f"\nProfile {profile['attributes']['name']!r} created "
            f"({profile['id']}), expires "
            f"{profile['attributes']['expirationDate']}."
        )

    print(
        f"""
Certificate {created['id']} created.
  type    {attrs.get('certificateType')}
  expires {attrs.get('expirationDate')}   <- put this in your calendar
  files   {out_dir} (all mode 0600, none of them in a git tree)
{profile_line}

Load it into CI. Piped, not echoed - do not paste either value into a shell:

  base64 < {p12_path} | \\
    gh secret set IOS_DIST_P12_B64 --env ios-code-signing

  gh secret set IOS_DIST_P12_PASSWORD --env ios-code-signing < {pass_path}

Then push an ios-v* tag. If this was a rotation, wait for that build to reach
TestFlight before revoking the old certificate in the developer portal.
"""
    )
    return 0


def bundle_id_of(token: str) -> str:
    """The portal's opaque id for com.wattracker.ios.

    /v1/profiles wants that id, not the reverse-DNS identifier, and the two
    are unrelated strings.
    """
    found = api(
        "GET",
        f"/v1/bundleIds?filter[identifier]={BUNDLE_ID}&limit=1",
        token,
    ).get("data", [])
    if not found:
        die(
            f"{BUNDLE_ID} is not registered in the developer portal. Register "
            "it (or let one archive with -allowProvisioningUpdates do it) "
            "before creating a profile."
        )
    return found[0]["id"]


if __name__ == "__main__":
    raise SystemExit(main())
