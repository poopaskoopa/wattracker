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


def fake_network(monkeypatch, harness, interfaces, default_interface):
    monkeypatch.setattr(
        harness, "ipv4_interfaces",
        lambda: [harness.Interface(name, address) for name, address in interfaces],
    )
    monkeypatch.setattr(harness, "default_route_interface", lambda: default_interface)


def test_a_vpn_holding_the_default_route_does_not_win(monkeypatch):
    # The exact shape of the author's Mac while writing this: a VPN on utun5
    # holds the default route, so the obvious UDP-socket probe answers
    # 10.5.0.2 - an address no phone on the hotspot can reach.
    harness = load_harness()
    fake_network(
        monkeypatch, harness,
        [("lo0", "127.0.0.1"), ("en0", "172.20.10.11"), ("utun5", "10.5.0.2")],
        default_interface="utun5",
    )

    assert harness.detect_lan_address(report=lambda line: None) == "172.20.10.11"


def test_a_hotspot_only_machine_returns_its_hotspot_address(monkeypatch):
    # 172.20.10.x is the iPhone Personal Hotspot range. It is inside
    # 172.16/12, so the private-range test must be a mask and not the
    # "172.16." prefix that would reject it.
    harness = load_harness()
    fake_network(
        monkeypatch, harness,
        [("lo0", "127.0.0.1"), ("en0", "172.20.10.11")],
        default_interface="en0",
    )

    assert harness.detect_lan_address(report=lambda line: None) == "172.20.10.11"


def test_the_default_route_wins_when_it_is_itself_physical(monkeypatch):
    harness = load_harness()
    fake_network(
        monkeypatch, harness,
        [("en0", "192.168.1.125"), ("en5", "10.9.9.9")],
        default_interface="en5",
    )
    said = []

    assert harness.detect_lan_address(report=said.append) == "10.9.9.9"
    # Two plausible candidates: the operator has to be told, because only they
    # know which network the phone is on.
    assert said and "en0 192.168.1.125" in said[0] and "--host" in said[0]


def test_the_lowest_numbered_physical_interface_breaks_a_tie(monkeypatch):
    harness = load_harness()
    fake_network(
        monkeypatch, harness,
        [("en5", "10.9.9.9"), ("en0", "192.168.1.125")],
        default_interface="utun3",
    )

    assert harness.detect_lan_address(report=lambda line: None) == "192.168.1.125"


def test_a_tunnel_only_machine_raises_rather_than_advertising_the_tunnel(monkeypatch):
    # Silently returning something unreachable is the bug this flag exists to
    # fix: the harness would print a URL, the phone would fail to reach it,
    # and the failure would read as a pairing error.
    harness = load_harness()
    fake_network(
        monkeypatch, harness,
        [("lo0", "127.0.0.1"), ("utun5", "10.5.0.2")],
        default_interface="utun5",
    )

    with pytest.raises(harness.LanAddressError, match="utun5 10.5.0.2"):
        harness.detect_lan_address(report=lambda line: None)


def test_a_public_address_is_not_offered_to_a_phone(monkeypatch):
    harness = load_harness()
    fake_network(
        monkeypatch, harness, [("en0", "203.0.113.4")], default_interface="en0",
    )

    with pytest.raises(harness.LanAddressError):
        harness.detect_lan_address(report=lambda line: None)


def test_interface_enumeration_reads_this_machine():
    # Not a fixture: the parser has to survive real ifconfig output, including
    # the "inet A --> A" point-to-point form a tunnel prints.
    harness = load_harness()

    interfaces = harness.ipv4_interfaces()

    assert any(item.name == "lo0" and item.address == "127.0.0.1"
               for item in interfaces)
    for item in interfaces:
        socket.inet_aton(item.address)  # raises if it is not dotted-quad IPv4


def test_lan_detection_returns_a_routable_ipv4():
    harness = load_harness()
    reachable = [
        item for item in harness.ipv4_interfaces()
        if not harness.is_tunnel(item.name) and harness.is_private_ipv4(item.address)
    ]
    if not reachable:
        pytest.skip("no phone-reachable IPv4 on this machine")

    address = harness.detect_lan_address(report=lambda line: None)

    assert harness.is_private_ipv4(address)
    assert address in {item.address for item in reachable}


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
