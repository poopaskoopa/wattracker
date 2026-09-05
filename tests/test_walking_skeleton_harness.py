"""The device-run pieces of scripts/walking_skeleton_server.py (#234).

Only the pure functions: the harness's server binds a real port and publishes
a real snapshot, and neither belongs in CI. What is worth pinning here is the
part a device run fails on silently - being told an address the phone cannot
reach.
"""
import importlib.util
import socket
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
HARNESS_PATH = ROOT / "scripts" / "walking_skeleton_server.py"
DEBUG_XCCONFIG = (ROOT / "ios" / "WatTracker" / "Config" / "Debug.xcconfig").read_text()


def load_harness():
    spec = importlib.util.spec_from_file_location("walking_skeleton_server", HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def has_network() -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return not probe.getsockname()[0].startswith("127.")
    except OSError:
        return False
    finally:
        probe.close()


def test_lan_detection_returns_a_routable_ipv4():
    if not has_network():
        pytest.skip("no non-loopback IPv4 on this machine")
    harness = load_harness()

    address = harness.detect_lan_address()

    socket.inet_aton(address)  # raises if it is not dotted-quad IPv4
    assert not address.startswith("127.")


def test_lan_detection_raises_rather_than_returning_loopback(monkeypatch):
    # A silent fall back to 127.0.0.1 is the bug this flag exists to fix: the
    # harness would print a URL, the phone would fail to reach it, and the
    # failure would read as a pairing error.
    harness = load_harness()

    class Unroutable:
        def connect(self, address):
            raise OSError("Network is unreachable")

        def getsockname(self):
            raise AssertionError("connect failed; must not be asked")

        def close(self):
            pass

    monkeypatch.setattr(harness.socket, "socket", lambda *a, **k: Unroutable())
    with pytest.raises(harness.LanAddressError):
        harness.detect_lan_address()


def test_local_xcconfig_holds_exactly_the_host_line(tmp_path):
    harness = load_harness()
    path = tmp_path / "Local.xcconfig"

    harness.write_local_xcconfig("172.20.10.11", 8765, path)

    lines = [line for line in path.read_text().splitlines()
             if line and not line.startswith("//")]
    assert lines == ["WATTRACKER_API_HOST = 172.20.10.11:8765"]


def test_debug_xcconfig_optionally_includes_the_local_override_last():
    # The `?` keeps a checkout without Local.xcconfig building, and the
    # include has to come after the default for the override to win.
    assert 'WATTRACKER_API_HOST = localhost:8765' in DEBUG_XCCONFIG
    assert DEBUG_XCCONFIG.index('#include? "Local.xcconfig"') > DEBUG_XCCONFIG.index(
        "WATTRACKER_API_HOST ="
    )
    assert (ROOT / ".gitignore").read_text().count(
        "ios/WatTracker/Config/Local.xcconfig"
    ) == 1


def test_default_run_advertises_loopback_and_says_so():
    harness = load_harness()
    server = harness.LocalServer.__new__(harness.LocalServer)
    server.bind_host = "127.0.0.1"
    server.advertise_host = "127.0.0.1"
    server.port = 8765

    assert server.advertised_url == "http://127.0.0.1:8765"
    assert server.internal_url == "http://127.0.0.1:8765"
    assert not server.reachable_off_box


def test_lan_run_advertises_the_lan_address_but_calls_itself_on_loopback():
    harness = load_harness()
    server = harness.LocalServer.__new__(harness.LocalServer)
    server.bind_host = "0.0.0.0"
    server.advertise_host = "172.20.10.11"
    server.port = 8765

    assert server.advertised_url == "http://172.20.10.11:8765"
    assert server.internal_url == "http://127.0.0.1:8765"
    assert server.reachable_off_box
