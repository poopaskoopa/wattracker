"""Assert the frozen build can still see Bluetooth.

smoke_frozen.ps1 checks that the app serves its UI. That catches a build which
dies, but not the failure this exists for: PyInstaller resolves bleak's backend
through collect_submodules(), which the spec wraps best-effort because
bleak + PyInstaller is historically fragile. A build whose WinRT backend failed
to package starts normally, serves every page, and simply never sees a trainer.
Nothing in a UI smoke test can tell that apart from a working build.

So this asserts on what the running app *reports* about Bluetooth - the same
reasoning as smoke_http.assert_credential_backend, which reads back the keyring
backend rather than trusting that a hidden import survived.

Two levels, because a CI runner has no radio:

* always - /ride/status must report Bluetooth available, and a scan must not
  fail with an import error. bluetooth_available() is an ``import bleak``
  check and is deliberately independent of whether an adapter exists, so this
  is meaningful on a machine with no Bluetooth at all.
* --require-device - a scan must also return at least one device. For a real
  hardware box with a trainer powered on; the definitive check, and the only
  one that proves the backend is not merely importable but working.

    python packaging/smoke_frozen_ble.py dist/wattracker/wattracker.exe
    python packaging/smoke_frozen_ble.py dist/wattracker/wattracker.exe --require-device

Stdlib only, like its sibling smoke tests: it runs against whatever python is
available, not necessarily the project venv.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

import smoke_http

# Signatures of a backend that did not package, as opposed to a machine with no
# Bluetooth hardware. The first is what a stripped bleak.backends.winrt looks
# like from the outside; the rest are how that surfaces through the app.
_IMPORT_FAILURE_MARKERS = (
    "no module named",
    "modulenotfound",
    "importerror",
    "bleak not installed",
)


def _looks_like_a_packaging_failure(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _IMPORT_FAILURE_MARKERS)


def main(argv) -> int:
    executable = argv[0]
    require_device = "--require-device" in argv[1:]

    temp_root = tempfile.mkdtemp(prefix="wattracker-frozen-ble-")
    port = smoke_http.free_loopback_port()
    env = dict(os.environ)
    env.update(
        WATTRACKER_DATA_DIR=os.path.join(temp_root, "data"),
        WATTRACKER_PORT=str(port),
        WATTRACKER_HOST="127.0.0.1",
        WATTRACKER_OPEN_BROWSER="0",
        WATTRACKER_AUTO_SCAN="0",
    )
    base = f"http://127.0.0.1:{port}"
    stdout_path = os.path.join(temp_root, "stdout.txt")
    stderr_path = os.path.join(temp_root, "stderr.txt")
    failures = []

    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        process = subprocess.Popen(
            [os.path.abspath(executable)], cwd=temp_root, env=env,
            stdout=out, stderr=err,
        )
        try:
            opener = smoke_http.build_opener()
            smoke_http.wait_until_serving(
                opener, base, timeout=120,
                still_running=lambda: process.poll() is None,
            )
            # The frozen build's own stdout is already being captured above,
            # which is exactly where a first-run operator reads the one-time
            # setup token from (wattracker/setuptoken.py). Registering without
            # it is refused, so this doubles as a check that a packaged build
            # still prints it.
            smoke_http.register_user(
                opener,
                base,
                smoke_http.read_setup_token(
                    stdout_path, still_running=lambda: process.poll() is None
                ),
            )

            status = json.loads(smoke_http.get_text(opener, base + "/ride/status"))
            print(f"/ride/status -> {status}")
            if not status.get("available"):
                failures.append(
                    "the frozen app reports Bluetooth unavailable "
                    f"({status.get('reason')!r}); the bleak backend did not package"
                )

            response = smoke_http.request(
                opener, base + "/ride/scan", {"smoke": "1"}, timeout=90
            )
            scan = json.loads(response.read().decode())
            devices = scan.get("devices") or []
            print(f"/ride/scan  -> available={scan.get('available')} "
                  f"reason={scan.get('reason')!r} devices={len(devices)}")
            for device in devices:
                print(f"    {device.get('name')}  {device.get('address')}  "
                      f"roles={device.get('roles')}  rssi={device.get('rssi')}")

            if _looks_like_a_packaging_failure(str(scan.get("reason"))):
                failures.append(
                    f"scan failed with an import error ({scan.get('reason')!r}); "
                    "the backend is missing from the bundle, not merely idle"
                )
            if require_device and not devices:
                failures.append(
                    "scan returned no devices with --require-device set; the "
                    "backend packaged but is not finding hardware that is present"
                )
            elif not devices:
                print("note: no devices seen. Not a failure without "
                      "--require-device - a machine with no adapter looks the "
                      "same from here. Only --require-device proves it works.")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    for path, label in ((stdout_path, "stdout"), (stderr_path, "stderr")):
        text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").strip()
        if text:
            print(f"--- frozen app {label} ---\n{text}")

    for failure in failures:
        print(f"FAIL: {failure}")
    print("RESULT:", "FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    if not sys.argv[1:]:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
