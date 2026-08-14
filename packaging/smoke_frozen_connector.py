"""Assert the frozen connector actually works, on the machine that built it.

CI cannot build this - the release job is hard-disabled until a certificate
exists - so this is the only thing between a spec change and a rider being
handed a binary that does nothing. tests/test_connector_packaging.py asserts
what the *checkout* says; this asserts what the *executable* does.

Three checks, in the order they can fail:

1. **Unpaired, it says so and stops.** With no configuration the connector
   must exit 2 and name what is missing in its log. A frozen windowed build
   has no stderr, so the log file is the only place that message can appear -
   which makes this a test of WP-A's file logging as much as of the exit code.
2. **Paired, it connects and answers.** Against a stub WebSocket server that
   speaks just enough of the protocol - the hello, one request, one response -
   the connector must attach and answer ``paths.default_activities_dir``. That
   is the whole client half exercised through the real transport.
3. **Its optional halves survived the freeze.** ``bleak`` and ``webviewpy``
   must be importable inside the frozen process. Availability, not hardware:
   exactly the argument smoke_frozen_ble.py makes, and for the same reason -
   a build whose backend failed to package starts fine and simply never works.

    python packaging/smoke_frozen_connector.py dist/WattrackerConnector.exe

Stdlib only, like its siblings: it runs against whatever python is available,
not necessarily the project venv. WATTRACKER_CONNECTOR_DIR is pointed at a
temporary directory throughout, so this can never read or write the real
configuration - the bar smoke_frozen_macos.py sets.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TOKEN = "s" * 43
_PROTOCOL = 2


# --------------------------------------------------------------- a stub server
class _StubServer(threading.Thread):
    """The smallest thing the connector will talk to.

    A hand-rolled WebSocket server rather than anything imported, because this
    script is stdlib-only by policy and the frozen binary is the thing under
    test - not the server. It performs the handshake, sends the hello the
    client waits for, issues one request, and records the answer.
    """

    daemon = True

    def __init__(self) -> None:
        super().__init__()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.authorization = None
        self.answer = None
        self.error = None

    def run(self) -> None:
        try:
            self._serve()
        except Exception as exc:  # reported by the main thread
            self.error = exc

    def _serve(self) -> None:
        conn, _addr = self._sock.accept()
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            headers = {}
            for line in request.decode("latin-1").split("\r\n")[1:]:
                if ": " in line:
                    name, value = line.split(": ", 1)
                    headers[name.lower()] = value
            self.authorization = headers.get("authorization")
            accept = base64.b64encode(
                hashlib.sha1(
                    headers.get("sec-websocket-key", "").encode() + _GUID
                ).digest()
            ).decode()
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
            )
            _send(conn, json.dumps(
                {"event": "hello", "protocol": _PROTOCOL, "device": "smoke"}
            ))
            _send(conn, json.dumps(
                {"id": 1, "method": "paths.default_activities_dir", "params": {}}
            ))
            deadline = time.time() + 30
            while time.time() < deadline:
                message = _recv(conn)
                if message is None:
                    return
                decoded = json.loads(message)
                if decoded.get("id") == 1:
                    self.answer = decoded
                    return


def _send(conn, text: str) -> None:
    payload = text.encode("utf-8")
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    conn.sendall(bytes(header) + payload)


def _recv(conn):
    """One text frame from a client. Clients always mask; servers never do."""
    header = _read_exactly(conn, 2)
    if header is None:
        return None
    length = header[1] & 0x7F
    if length == 126:
        extra = _read_exactly(conn, 2)
        if extra is None:
            return None
        length = struct.unpack(">H", extra)[0]
    elif length == 127:
        extra = _read_exactly(conn, 8)
        if extra is None:
            return None
        length = struct.unpack(">Q", extra)[0]
    mask = _read_exactly(conn, 4) if header[1] & 0x80 else b"\x00\x00\x00\x00"
    if mask is None:
        return None
    payload = _read_exactly(conn, length)
    if payload is None:
        return None
    return bytes(b ^ mask[i % 4] for i, b in enumerate(payload)).decode("utf-8")


def _read_exactly(conn, count: int):
    buffer = b""
    while len(buffer) < count:
        chunk = conn.recv(count - len(buffer))
        if not chunk:
            return None
        buffer += chunk
    return buffer


# ------------------------------------------------------------------ the checks
def _terminate_tree(process) -> None:
    """End the process *and its children*, and wait for them.

    A onefile build is two processes: the bootloader that unpacked the payload
    into %TEMP%\\_MEIxxxx, and the child it started to actually run the
    connector. Terminating the parent leaves the child alive, still holding the
    log file, the socket and the temporary directory this script is about to
    delete - and still holding the pipes, so anything waiting to read them
    waits forever. taskkill /T is what covers the pair.
    """
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
        capture_output=True, text=True,
    )
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()


def _run(executable, args, config_dir, timeout=60):
    """Run the binary to completion, and turn a hang into a failure.

    Not ``subprocess.run(timeout=...)``: that kills the process it started and
    then goes on waiting for pipes the surviving child still holds, so a
    connector that hangs hangs this script too, silently and without output.
    That is not hypothetical - a ``console=False`` build reports an unhandled
    exception in a modal dialog, on a machine where by definition nobody is
    looking at the screen.
    """
    environment = dict(os.environ)
    environment["WATTRACKER_CONNECTOR_DIR"] = str(config_dir)
    process = subprocess.Popen(
        [executable, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_tree(process)
        stdout, stderr = process.communicate(timeout=30)
        stderr = (stderr or "") + (
            f"\n[smoke] it was still running after {timeout}s and was killed. "
            "A windowed build with nothing on stdout is usually a fatal-error "
            "dialog waiting for a click nobody is there to give it."
        )
    return subprocess.CompletedProcess(
        [executable, *args], process.returncode, stdout, stderr
    )


def _log_text(config_dir) -> str:
    path = pathlib.Path(config_dir) / "connector.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def check_unpaired_refuses(executable, config_dir) -> bool:
    result = _run(executable, ["--headless", "--verbose"], config_dir, timeout=60)
    log = _log_text(config_dir)
    combined = f"{result.stdout}\n{result.stderr}\n{log}"
    if result.returncode != 2:
        print(f"FAIL: unpaired connector exited {result.returncode}, expected 2")
        print(combined[-2000:])
        return False
    if "Missing" not in combined or "pair" not in combined.lower():
        print("FAIL: unpaired connector did not say what was missing")
        print(combined[-2000:])
        return False
    print("ok: unpaired, exits 2 and says what to do")
    return True


def check_connects_and_answers(executable, config_dir) -> bool:
    server = _StubServer()
    server.start()
    (pathlib.Path(config_dir) / "connector.json").write_text(
        json.dumps({
            "server": f"http://127.0.0.1:{server.port}",
            "token": _TOKEN,
            "activities_dir": None,
            "workouts_dir": None,
        }),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [executable, "--headless", "--verbose"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "WATTRACKER_CONNECTOR_DIR": str(config_dir)},
    )
    try:
        server.join(timeout=60)
    finally:
        # The tree, not the process: the survivor of a half-killed onefile
        # build keeps the log file open, and this directory is deleted next.
        _terminate_tree(process)

    if server.error is not None:
        print(f"FAIL: stub server errored: {server.error!r}")
        return False
    if server.authorization != f"Bearer {_TOKEN}":
        print(f"FAIL: connector sent {server.authorization!r} as its credential")
        return False
    if not server.answer or "result" not in server.answer:
        print(f"FAIL: connector did not answer the request: {server.answer!r}")
        print(_log_text(config_dir)[-2000:])
        return False
    print(f"ok: connected, authenticated, answered {server.answer['result']!r}")
    return True


def check_optional_halves_survived(executable, config_dir) -> bool:
    """bleak and webviewpy must be importable *inside* the frozen process.

    Both are resolved at runtime, so neither is reached by PyInstaller's static
    analysis; both are wrapped best-effort in the spec. A build that lost
    either starts normally and is simply missing a feature - the radio, or the
    window - with nothing to say so.
    """
    ok = True
    for module, what in (("bleak", "Bluetooth"), ("webviewpy", "the tray window")):
        result = _run(executable, ["--smoke-import", module], config_dir, timeout=60)
        if result.returncode != 0:
            print(f"FAIL: {module} did not survive the freeze, so {what} is gone")
            print(f"{result.stdout}\n{result.stderr}"[-2000:])
            ok = False
        else:
            print(f"ok: {module} imports inside the frozen build")
    return ok


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    executable = argv[0]
    if not pathlib.Path(executable).exists():
        print(f"FAIL: no such executable: {executable}")
        return 1

    failures = 0
    for check in (
        check_unpaired_refuses,
        check_connects_and_answers,
        check_optional_halves_survived,
    ):
        with tempfile.TemporaryDirectory() as config_dir:
            if not check(executable, config_dir):
                failures += 1
    if failures:
        print(f"\n{failures} check(s) failed")
        return 1
    print("\nall connector smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
