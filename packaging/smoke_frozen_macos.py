#!/usr/bin/env python3
"""Smoke-test the frozen macOS app - the equivalent of smoke_frozen.ps1.

Two launches, because a .app can pass one and fail the other:

1. ``Contents/MacOS/wattracker`` run directly, which is what a terminal user or
   CI gets, and gives a real pid plus captured stdout/stderr;
2. ``open`` on the bundle, which goes through LaunchServices exactly as a
   double-click in Finder does - no tty, no inherited shell environment, the
   Info.plist actually consulted. This is the path the PyInstaller BUNDLE step
   exists for, so it is the one worth proving.

Both runs are fully sandboxed: HOME and every WATTRACKER_* path point into a
throwaway directory, so the real ~/.wattracker database is untouchable. The port
is kernel-assigned, never the default 8000, so a wattracker already running on
this machine is never disturbed. As a backstop the real database file's
size/mtime is captured before and compared after.

Stdlib only - it must run under the system python3 without the project venv.

Usage: python3 packaging/smoke_frozen_macos.py dist/wattracker.app
       python3 packaging/smoke_frozen_macos.py dist/wattracker/wattracker
"""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import smoke_http

READY_TIMEOUT = 90
# keyring.backends.macOS is what credstore reports as "system keychain". If the
# Keychain backend failed to package, keyring silently falls back and the
# settings page says "encrypted local file key" instead - a real regression that
# no HTTP status code would reveal.
EXPECTED_BACKEND = "system keychain"


def resolve_executable(target: Path) -> "tuple[Path, Path | None]":
    """Return (executable, bundle) for either a .app or a bare onedir binary."""
    if target.is_dir() and target.suffix == ".app":
        plist = plistlib.loads((target / "Contents" / "Info.plist").read_bytes())
        executable = target / "Contents" / "MacOS" / plist["CFBundleExecutable"]
        if not executable.is_file():
            raise SystemExit(f"bundle has no executable at {executable}")
        return executable, target
    if target.is_file():
        return target, None
    raise SystemExit(f"not a frozen app or executable: {target}")


def sandbox_env(home: Path, port: int) -> dict:
    """A child environment that cannot reach the user's real data.

    Overriding HOME is the load-bearing part: config.app_data_dir() and
    paths.py both fall back to os.path.expanduser("~"). WATTRACKER_DATA_DIR and
    WATTRACKER_DB are then set explicitly so an inherited value from the caller's
    shell cannot redirect the run back at a real database.
    """
    data = home / ".wattracker"
    activities = home / "Zwift" / "Activities"
    for directory in (home, data, activities):
        directory.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("WATTRACKER_")}
    env.update(
        HOME=str(home),
        WATTRACKER_DATA_DIR=str(data),
        WATTRACKER_DB=str(data / "wattracker.db"),
        WATTRACKER_ACTIVITIES_DIR=str(activities),
        WATTRACKER_HOST="127.0.0.1",
        WATTRACKER_PORT=str(port),
        WATTRACKER_OPEN_BROWSER="0",
        WATTRACKER_AUTO_SCAN="0",
    )
    return env


def check_ui(port: int, still_running=None) -> None:
    base = f"http://127.0.0.1:{port}"
    opener = smoke_http.build_opener()
    smoke_http.wait_until_serving(
        opener, base, timeout=READY_TIMEOUT, still_running=still_running
    )
    smoke_http.assert_ui_renders(opener, base)
    smoke_http.register_user(opener, base)
    smoke_http.assert_credential_backend(opener, base, EXPECTED_BACKEND)
    # bleak is an optional extra, so its absence is reported rather than fatal -
    # but when the build was made with [ble], "Bluetooth unavailable" in a
    # frozen app means the CoreBluetooth backend did not survive packaging.
    ride = smoke_http.get_text(opener, base + "/ride")
    if 'id="scanBtn"' not in ride:
        raise AssertionError("ride page did not render")
    ble = "Bluetooth available" in ride
    print(
        f"    UI rendered, credential backend is the {EXPECTED_BACKEND}, "
        f"bluetooth {'available' if ble else 'UNAVAILABLE (bleak not packaged)'}"
    )


def dump(label: str, path: Path) -> None:
    print(f"=== frozen app {label} ===")
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        print(f"<unavailable: {exc}>")
        return
    print(text if text else "<empty>")


def run_direct(executable: Path) -> None:
    """Launch the binary inside the bundle as a plain child process."""
    with tempfile.TemporaryDirectory(prefix="wattracker-frozen-") as temporary:
        root = Path(temporary)
        port = smoke_http.free_loopback_port()
        stdout_path, stderr_path = root / "stdout.txt", root / "stderr.txt"
        print(f"[direct] {executable} on port {port}")
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            proc = subprocess.Popen(
                [str(executable)],
                cwd=root,
                env=sandbox_env(root / "home", port),
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
            )
        try:
            check_ui(port, still_running=lambda: proc.poll() is None)
            dump("stdout", stdout_path)
        except BaseException:
            dump("stdout", stdout_path)
            dump("stderr", stderr_path)
            raise
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def bundle_pids(executable: Path) -> "list[int]":
    """pids whose command line is exactly this bundle's executable.

    Anchored on the absolute path under dist/, so it can never match a
    wattracker the developer is running from a source checkout.
    """
    result = subprocess.run(
        ["pgrep", "-f", f"^{re.escape(str(executable))}$"],
        capture_output=True,
        text=True,
    )
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]


def run_via_launchservices(executable: Path, bundle: Path) -> None:
    """Launch the .app the way Finder does, with no tty and no shell env."""
    if bundle_pids(executable):
        raise SystemExit("an instance of this bundle is already running; stop it first")
    with tempfile.TemporaryDirectory(prefix="wattracker-frozen-") as temporary:
        root = Path(temporary)
        port = smoke_http.free_loopback_port()
        stdout_path, stderr_path = root / "stdout.txt", root / "stderr.txt"
        stdout_path.touch()
        stderr_path.touch()
        env = sandbox_env(root / "home", port)
        command = ["open", "-n", "-g", "--stdout", str(stdout_path), "--stderr", str(stderr_path)]
        for name in sorted(k for k in env if k == "HOME" or k.startswith("WATTRACKER_")):
            command += ["--env", f"{name}={env[name]}"]
        command.append(str(bundle))
        print(f"[launchservices] open {bundle} on port {port}")
        subprocess.run(command, check=True, env=env)
        try:
            check_ui(port)
            dump("stdout", stdout_path)
        except BaseException:
            dump("stdout", stdout_path)
            dump("stderr", stderr_path)
            raise
        finally:
            for pid in bundle_pids(executable):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.time() + 10
            while bundle_pids(executable) and time.time() < deadline:
                time.sleep(0.2)
            for pid in bundle_pids(executable):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def real_database_fingerprint() -> "tuple | None":
    """(size, mtime) of the developer's real database, if one exists."""
    path = Path(os.path.expanduser("~")) / ".wattracker" / "wattracker.db"
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime


def main(argv: "list[str]") -> int:
    if sys.platform != "darwin":
        raise SystemExit("this smoke test is macOS-only")
    if len(argv) != 1:
        raise SystemExit(__doc__)
    executable, bundle = resolve_executable(Path(argv[0]).resolve())
    before = real_database_fingerprint()

    run_direct(executable)
    if bundle is not None:
        if shutil.which("open") is None:
            print("[launchservices] skipped: /usr/bin/open is unavailable")
        else:
            run_via_launchservices(executable, bundle)
    else:
        print("[launchservices] skipped: target is not a .app bundle")

    if real_database_fingerprint() != before:
        raise SystemExit("the smoke test modified the real wattracker database")
    print("frozen macOS smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
