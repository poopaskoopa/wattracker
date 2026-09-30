#!/usr/bin/env python3
"""Install the iOS release signing secrets from the owner's login keychain.

This is an owner-only operation.  ``security export`` requires its PKCS#12
passphrase through ``-P``; that unavoidable short-lived argv exposure is why
this script is owner-only. Values passed to ``gh secret set`` are always on
stdin, never argv, and all child output is captured.

The runner is injectable so tests can exercise the control flow without
invoking ``security``, exporting a key, or contacting GitHub.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


LOGIN_KEYCHAIN = "login.keychain-db"
GH_PATH = "/opt/homebrew/bin/gh"
GH_ENVIRONMENT = "ios-code-signing"
DEFAULT_HOST_CONFIG = Path("ios/WatTracker/Config/Production.local.xcconfig")
_HOST_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_IDENTITY_RE = re.compile(
    r'^\s*(?P<number>\d+)\)\s+(?P<fingerprint>[0-9A-Fa-f]{40})\s+'
    r'"(?P<label>[^"]+)"(?:\s+\(CSSMERR_[^)]+\))?\s*$'
)


class SecretSetupError(RuntimeError):
    """A safe, user-facing error that never contains a secret value."""


@dataclass(frozen=True)
class Identity:
    fingerprint: str
    label: str


Runner = Callable[..., subprocess.CompletedProcess[bytes]]
Output = Callable[[str], None]
Input = Callable[[str], str]


def default_runner(
    argv: Sequence[str], *, input_data: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    """Run a child with all output captured and no inherited secret stdin."""

    try:
        return subprocess.run(
            list(argv),
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        raise SecretSetupError(f"{Path(argv[0]).name} is not installed or not on PATH") from None
    except OSError:
        raise SecretSetupError(f"could not run {Path(argv[0]).name}") from None


def _run(
    runner: Runner,
    argv: Sequence[str],
    *,
    input_data: bytes = b"",
    failure: str,
) -> subprocess.CompletedProcess[bytes]:
    # Do not include stdout/stderr in an exception: a tool error may echo a
    # password or the contents of a secret supplied on stdin.
    result = runner(tuple(argv), input_data=input_data)
    if result.returncode != 0:
        raise SecretSetupError(failure)
    return result


def _run_with_private_umask(
    runner: Runner,
    argv: Sequence[str],
    *,
    failure: str,
) -> subprocess.CompletedProcess[bytes]:
    """Run the exporter while its newly-created p12 inherits mode 0600."""

    previous_umask = os.umask(0o077)
    try:
        return _run(runner, argv, failure=failure)
    finally:
        os.umask(previous_umask)


def _find_identities(
    keychain: str | Path,
    *,
    policy: str | None,
    apple_distribution_only: bool,
    runner: Runner,
    failure: str,
    verbose: bool = True,
) -> list[Identity]:
    argv = ["security", "find-identity"]
    if verbose:
        argv.append("-v")
    if policy is not None:
        argv.extend(("-p", policy))
    argv.append(str(keychain))
    result = _run(
        runner,
        argv,
        failure=failure,
    )
    raw = result.stdout.decode("utf-8", errors="replace")
    identities: list[Identity] = []
    fingerprints: set[str] = set()
    for line in raw.splitlines():
        match = _IDENTITY_RE.match(line)
        if match and (
            not apple_distribution_only or "Apple Distribution" in match.group("label")
        ):
            fingerprint = match.group("fingerprint")
            if fingerprint.casefold() in fingerprints:
                continue
            fingerprints.add(fingerprint.casefold())
            identities.append(Identity(fingerprint=fingerprint, label=match.group("label")))
    return identities


def find_identities(*, runner: Runner = default_runner) -> list[Identity]:
    return _find_identities(
        LOGIN_KEYCHAIN,
        policy="codesigning",
        apple_distribution_only=True,
        verbose=True,
        runner=runner,
        failure="could not inspect the login keychain",
    )


def _count_private_keys(keychain: str | Path, *, runner: Runner) -> int:
    result = _run(
        runner,
        ("security", "find-key", "-t", "private", str(keychain)),
        failure="could not inspect private keys in the temporary signing keychain",
    )
    raw = result.stdout.decode("utf-8", errors="replace")
    return sum(1 for line in raw.splitlines() if line.startswith("keychain: "))


def choose_identity(
    identities: Sequence[Identity],
    *,
    input_fn: Input = input,
    output: Output = print,
) -> Identity:
    if not identities:
        raise SecretSetupError("no Apple Distribution identity was found in the login keychain")
    for number, identity in enumerate(identities, start=1):
        # Certificate subjects are public metadata.  The private key is never
        # returned by find-identity and is never printed here.
        output(f"{number}: {identity.label}")
    if len(identities) == 1:
        return identities[0]
    choice = input_fn("Select the Apple Distribution identity number: ").strip()
    try:
        selected = int(choice)
    except ValueError:
        raise SecretSetupError("identity selection must be a number") from None
    if not 1 <= selected <= len(identities):
        raise SecretSetupError("identity selection is out of range")
    return identities[selected - 1]


def validate_host(host: str) -> str:
    """Validate a bare, non-placeholder DNS host without returning secrets."""

    if not host or host != host.strip() or any(char.isspace() for char in host):
        raise SecretSetupError("the production API host is missing, empty, or contains whitespace")
    if "://" in host or any(char in host for char in "/?#:"):
        raise SecretSetupError("the production API host must be a hostname, not a URL or path")
    if not _HOST_RE.fullmatch(host):
        raise SecretSetupError("the production API host is not a valid hostname")
    if host.casefold() == "api.wattracker.com":
        raise SecretSetupError("the production API host must not be the placeholder host")
    return host


def read_production_host(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*WATTRACKER_API_HOST\s*=\s*(.*?)\s*;?\s*$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        return validate_host(value)
    return None


def _set_secret(
    name: str,
    value: str,
    *,
    runner: Runner,
    gh_path: str,
) -> None:
    _run(
        runner,
        (
            gh_path,
            "secret",
            "set",
            name,
            "--env",
            GH_ENVIRONMENT,
            "--repo",
            "poopaskoopa/wattracker",
        ),
        input_data=value.encode("utf-8"),
        failure=f"could not set the {name} GitHub secret",
    )


def _cleanup_temporary_material(
    runner: Runner,
    temp_dir: Path,
    temp_keychain: Path,
    *,
    keychain_created: bool,
) -> None:
    cleanup_failed = False
    if keychain_created:
        try:
            _run(
                runner,
                ("security", "delete-keychain", str(temp_keychain)),
                failure="could not remove the temporary signing keychain",
            )
        except SecretSetupError:
            cleanup_failed = True
    try:
        shutil.rmtree(temp_dir)
    except OSError:
        cleanup_failed = True
    if cleanup_failed:
        raise SecretSetupError("could not clean up temporary signing material")


def install_secrets(
    *,
    runner: Runner = default_runner,
    input_fn: Input = input,
    output: Output = print,
    dry_run: bool = False,
    host_config: Path = DEFAULT_HOST_CONFIG,
    gh_path: str = GH_PATH,
) -> None:
    output_warning = (
        "The first export inspects every identity in the login keychain; "
        "macOS may prompt once per key."
    )
    output(output_warning)
    identities = find_identities(runner=runner)
    identity = choose_identity(identities, input_fn=input_fn, output=output)
    host = read_production_host(host_config)

    if dry_run:
        output("Dry run: no p12 was exported and no GitHub secret was changed.")
        if host is not None:
            output("Dry run: a validated production API host would be stored.")
        return

    password = secrets.token_urlsafe(32)
    source_password = secrets.token_urlsafe(32)
    temp_keychain_password = secrets.token_urlsafe(32)
    temp_dir = Path(tempfile.mkdtemp(prefix="wattracker-ios-secrets-"))
    temp_keychain = temp_dir / "temporary.keychain-db"
    source_p12_path = temp_dir / "all-identities.p12"
    selected_p12_path = temp_dir / "distribution.p12"
    keychain_created = False
    try:
        temp_dir.chmod(0o700)
        _run_with_private_umask(
            runner,
            (
                "security",
                "export",
                "-k",
                LOGIN_KEYCHAIN,
                "-t",
                "identities",
                "-f",
                "pkcs12",
                "-P",
                source_password,
                "-o",
                str(source_p12_path),
            ),
            failure="could not export identities from the login keychain",
        )
        if not source_p12_path.is_file() or source_p12_path.stat().st_size == 0:
            raise SecretSetupError("security export produced no source p12 file")
        source_p12_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

        keychain_created = True
        _run(
            runner,
            ("security", "create-keychain", "-p", temp_keychain_password, str(temp_keychain)),
            failure="could not create the temporary signing keychain",
        )
        _run(
            runner,
            ("security", "unlock-keychain", "-p", temp_keychain_password, str(temp_keychain)),
            failure="could not unlock the temporary signing keychain",
        )
        _run(
            runner,
            (
                "security",
                "import",
                str(source_p12_path),
                "-k",
                str(temp_keychain),
                "-P",
                source_password,
                "-T",
                "/usr/bin/security",
            ),
            failure="could not import identities into the temporary signing keychain",
        )
        temporary_identities = _find_identities(
            temp_keychain,
            policy=None,
            apple_distribution_only=False,
            verbose=False,
            runner=runner,
            failure="could not inspect the temporary signing keychain",
        )
        selected_fingerprint = identity.fingerprint.casefold()
        if not any(
            candidate.fingerprint.casefold() == selected_fingerprint
            for candidate in temporary_identities
        ):
            raise SecretSetupError("the selected identity was not found after import")
        for candidate in temporary_identities:
            if candidate.fingerprint.casefold() == selected_fingerprint:
                continue
            _run(
                runner,
                (
                    "security",
                    "delete-identity",
                    "-Z",
                    candidate.fingerprint,
                    str(temp_keychain),
                ),
                failure="could not remove a non-selected temporary identity",
            )

        remaining_identities = _find_identities(
            temp_keychain,
            policy=None,
            apple_distribution_only=False,
            verbose=False,
            runner=runner,
            failure="could not verify the temporary signing keychain identities",
        )
        if len(remaining_identities) != 1:
            raise SecretSetupError(
                "the temporary signing keychain must contain exactly one identity before export"
            )
        if remaining_identities[0].fingerprint.casefold() != selected_fingerprint:
            raise SecretSetupError("the selected identity was not the only identity left")
        if _count_private_keys(temp_keychain, runner=runner) != 1:
            raise SecretSetupError(
                "the temporary signing keychain must contain exactly one private key before export"
            )

        _run_with_private_umask(
            runner,
            (
                "security",
                "export",
                "-k",
                str(temp_keychain),
                "-t",
                "identities",
                "-f",
                "pkcs12",
                "-P",
                password,
                "-o",
                str(selected_p12_path),
            ),
            failure="could not export the selected Apple Distribution identity",
        )
        if not selected_p12_path.is_file() or selected_p12_path.stat().st_size == 0:
            raise SecretSetupError("security export produced no selected-identity p12 file")
        selected_p12_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        encoded = base64.b64encode(selected_p12_path.read_bytes()).decode("ascii")
        _set_secret("IOS_DIST_P12_B64", encoded, runner=runner, gh_path=gh_path)
        _set_secret("IOS_DIST_P12_PASSWORD", password, runner=runner, gh_path=gh_path)
        if host is not None:
            _set_secret("WATTRACKER_IOS_API_HOST", host, runner=runner, gh_path=gh_path)
        output("iOS release secrets were set without printing their values.")
    finally:
        active_exception = sys.exc_info()[1]
        try:
            _cleanup_temporary_material(
                runner,
                temp_dir,
                temp_keychain,
                keychain_created=keychain_created,
            )
        except SecretSetupError as cleanup_error:
            if active_exception is None:
                raise
            active_exception.add_note(str(cleanup_error))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the selected identity and planned actions without exporting or setting secrets",
    )
    parser.add_argument(
        "--host-config",
        type=Path,
        default=DEFAULT_HOST_CONFIG,
        help="optional Production.local.xcconfig path",
    )
    args = parser.parse_args(argv)
    try:
        install_secrets(dry_run=args.dry_run, host_config=args.host_config)
    except SecretSetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
