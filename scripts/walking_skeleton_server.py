#!/usr/bin/env python3
"""Run the walking skeleton (#171) end to end against a local server.

What this does, in order:

1. Reads a full object snapshot out of the local database, READ-ONLY, and
   pages it into publishable batches.  Not just the ``profile`` object it
   built when #171 only needed one FTP number: the iOS Dashboard reads
   ``training_state``, ``load_point`` and ``curve`` as well, and against a
   profile-only harness a paired app lands on the Dashboard's ``.noData``
   state, so "renders on real data" could not be checked at all (#234).
2. Starts the real cloud app with an in-memory store: on ``127.0.0.1`` by
   default, or, with ``--lan``, on every interface, advertising the address a
   phone on the same wifi or hotspot can reach this Mac at.  Deliberately not
   the default route's address, which on a machine with a VPN up belongs to
   the tunnel and is reachable from nothing (#234).
3. Enrolls a writer through the real enrollment routes, with a real Ed25519
   keypair generated in this process and never written anywhere.
4. Pushes every page through the real, signed ``/api/v1/sync/batches``, in
   order, each as its own batch.
5. Mints a pairing code through the real, writer-signed pairing route.
6. Prints the code and waits, so the iOS app can redeem it.

It is a development harness, not part of the product.  Nothing here runs in
the desktop app, and nothing here is a substitute for the deployment in
``infra/``: the point is only to give the phone a real server, with real
signatures, to talk to.

**It never writes to the local database.**  Every read goes through
``wattracker.cloud.snapshot.readonly_connection``, which opens SQLite with
``mode=ro`` and refuses to create a file that is not there.

Usage:

    python scripts/walking_skeleton_server.py            # user 1, default DB
    python scripts/walking_skeleton_server.py --user-id 5
    python scripts/walking_skeleton_server.py --db /path/to/copy.db --port 8765
    python scripts/walking_skeleton_server.py --lan       # reachable from a phone
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wattracker.cloud.api import (  # noqa: E402
    CloudConfig,
    CloudState,
    create_cloud_app,
)
from wattracker.cloud.security import (  # noqa: E402
    MemorySecurityStateBackend,
    canonical_request,
    digest_body,
    generate_signing_keypair,
    sign_request_ed25519,
)
from wattracker.cloud.snapshot import SnapshotError, snapshot_publish_pages  # noqa: E402

OPERATOR_SUBJECT = "walking-skeleton-operator"

# Untracked, gitignored override for ios/WatTracker/Config/Debug.xcconfig,
# which optionally includes it. The serving address changes every time the Mac
# changes network (a phone hotspot puts it on 172.20.10.x), so the committed
# default cannot be right for a device run, and hand-editing a tracked file
# before every run meant remembering to revert it afterwards (#234).
LOCAL_XCCONFIG = ROOT / "ios" / "WatTracker" / "Config" / "Local.xcconfig"


class LanAddressError(RuntimeError):
    """This machine has no usable LAN address to advertise."""


# Interfaces a phone on the same wifi cannot reach, whatever address they
# carry: VPN and other tunnels (utun/tun/tap/ipsec/ppp) and Apple's
# peer-to-peer radios (awdl is AirDrop, llw is low-latency WLAN).
TUNNEL_PREFIXES = ("utun", "tun", "tap", "ipsec", "ppp", "awdl", "llw")

# 10/8, 172.16/12 and 192.168/16. The middle one is why this is a mask test
# and not a string prefix test: an iPhone Personal Hotspot hands out
# 172.20.10.x, which is inside 172.16/12 and which "172.16." would reject.
PRIVATE_V4_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


class Interface(NamedTuple):
    name: str
    address: str


def is_private_ipv4(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in PRIVATE_V4_NETWORKS)


def is_tunnel(name: str) -> bool:
    return name.startswith(TUNNEL_PREFIXES)


def ipv4_interfaces() -> list[Interface]:
    """Every UP interface with an IPv4 address, in ``ifconfig`` order.

    ``ifconfig`` rather than a library: it is in the base system, it is the
    only enumeration that reports flags and addresses together, and adding a
    dependency to a development harness to read a network address would be a
    poor trade.
    """
    try:
        listing = subprocess.run(
            ["ifconfig", "-a"], capture_output=True, text=True, timeout=10, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise LanAddressError(f"could not enumerate network interfaces: {exc}")

    interfaces: list[Interface] = []
    name = ""
    up = False
    for line in listing.splitlines():
        header = re.match(r"^(\S+): flags=\d+<([^>]*)>", line)
        if header is not None:
            name, up = header.group(1), "UP" in header.group(2).split(",")
            continue
        address = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if address is not None and name and up:
            interfaces.append(Interface(name, address.group(1)))
    return interfaces


def default_route_interface() -> str | None:
    """Whichever interface holds the default route, or None if that is unreadable."""
    try:
        route = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"^\s*interface:\s*(\S+)", route, re.MULTILINE)
    return match.group(1) if match is not None else None


def _interface_sort_key(interface: Interface) -> tuple[int, str, int]:
    # en0 before en4 before bridge0: physical wifi/ethernet is where a phone
    # is, and the trailing number sorts numerically, not lexically.
    digits = re.search(r"(\d+)$", interface.name)
    return (
        0 if interface.name.startswith("en") else 1,
        re.sub(r"\d+$", "", interface.name),
        int(digits.group(1)) if digits else 0,
    )


def detect_lan_address(report=print) -> str:
    """The IPv4 address a phone on the same network can reach this Mac at.

    NOT the default route's address. Measured on the author's Mac: a VPN on
    ``utun5`` held the default route with ``10.5.0.2``, while the phone was on
    the hotspot with the Mac at ``172.20.10.11`` on ``en0``. The obvious probe
    -- asking a UDP socket which source address it would use for a public host
    -- follows the default route and answers ``10.5.0.2``, which no phone can
    reach. A VPN is the normal state of this machine, so that answer is wrong
    most of the time, and wrong in exactly the way this flag exists to prevent:
    a server that looks up and fails on the device as a pairing error.

    So: enumerate, drop tunnels and non-private addresses, prefer the default
    route only when it is itself a physical interface, and say out loud when
    more than one candidate survives rather than guessing in silence.
    """
    interfaces = ipv4_interfaces()
    candidates = [
        interface for interface in interfaces
        if not is_tunnel(interface.name)
        and not interface.address.startswith("127.")
        and is_private_ipv4(interface.address)
    ]
    if not candidates:
        seen = ", ".join(f"{i.name} {i.address}" for i in interfaces) or "none"
        raise LanAddressError(
            "no physical interface has a private IPv4 address, so nothing here "
            f"is reachable from a phone (found: {seen}). Join a wifi network or "
            "tether to the phone, or pass --host explicitly."
        )

    candidates.sort(key=_interface_sort_key)
    default_interface = default_route_interface()
    chosen = next(
        (item for item in candidates if item.name == default_interface),
        candidates[0],
    )
    if len(candidates) > 1:
        others = ", ".join(f"{i.name} {i.address}" for i in candidates if i != chosen)
        report(f"note         several reachable addresses; also saw {others}. "
               "Pass --host to override.")
    return chosen.address


def write_local_xcconfig(host: str, port: int, path: Path = LOCAL_XCCONFIG) -> Path:
    """Point the iOS Debug build at ``host:port`` without touching a tracked file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "// Written by scripts/walking_skeleton_server.py --lan. Gitignored,\n"
        "// and optionally included by Debug.xcconfig, so pointing the app at\n"
        "// today's address is never an edit to a tracked file.\n"
        f"WATTRACKER_API_HOST = {host}:{port}\n",
        encoding="utf-8",
    )
    return path


def default_db_path() -> Path:
    override = os.environ.get("WATTRACKER_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".wattracker" / "wattracker.db"


class LocalServer:
    """The cloud app on a local port, in a thread.

    Two addresses, deliberately kept apart: ``internal_url`` is what this
    process calls itself on (enrollment, publish, minting) and is always a
    loopback address, while ``advertised_url`` is what the phone is told to
    use. Binding ``0.0.0.0`` makes a fine listener and a useless URL.
    """

    def __init__(self, bind_host: str, port: int,
                 advertise_host: str | None = None) -> None:
        import uvicorn

        self.bind_host = bind_host
        self.advertise_host = advertise_host or bind_host
        self.port = port
        self.config = CloudConfig(
            server_secret=secrets.token_bytes(32),
            # A fresh operator token per run: it authorizes minting an
            # enrollment invitation, and a fixed one in a repository would be
            # a credential in a repository.
            operator_token=secrets.token_urlsafe(24),
            plane="all",
            require_subscription=True,
            # No gateway in front of a loopback server, so no proof to demand and
            # -- crucially -- no verified-subject header to trust. Both flags
            # off together is the honest configuration: a deployment that
            # claimed to enforce a subject with nothing issuing one would be a
            # control in name only, and CloudState refuses to start that way.
            require_gateway_proof=False,
            require_verified_subject=False,
            allowed_origins=(),
        )
        self.state = CloudState.create(
            self.config, security_backend=MemorySecurityStateBackend()
        )
        app = create_cloud_app(self.config, state=self.state)
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=bind_host, port=port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def internal_url(self) -> str:
        """Where this process calls itself. Never a wildcard bind address."""
        host = self.bind_host
        if host in ("0.0.0.0", "::", "localhost", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self.port}"

    @property
    def advertised_url(self) -> str:
        """Where the phone should be pointed."""
        return f"http://{self.advertise_host}:{self.port}"

    @property
    def reachable_off_box(self) -> bool:
        return not self.advertise_host.startswith("127.") and \
            self.advertise_host not in ("localhost", "::1")

    def start(self, timeout: float = 15.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getattr(self._server, "started", False):
                return
            time.sleep(0.05)
        raise RuntimeError("the local server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5.0)


def post(url: str, headers: dict[str, str], body: bytes) -> dict:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"{url} -> HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise SystemExit(f"{url} is unreachable: {exc}") from exc


def enroll_writer(server: LocalServer) -> tuple[dict, bytes]:
    """Take the desktop install through the real enrollment exchange."""
    private_key, public_key = generate_signing_keypair()
    invitation = post(
        f"{server.internal_url}/api/v1/enrollment/start",
        {
            "Content-Type": "application/json",
            "X-Operator-Token": server.config.operator_token,
            server.config.verified_subject_header: OPERATOR_SUBJECT,
        },
        b"{}",
    )
    enrolled = post(
        f"{server.internal_url}/api/v1/enrollment/complete",
        {
            "Content-Type": "application/json",
            server.config.verified_subject_header: OPERATOR_SUBJECT,
        },
        json.dumps(
            {
                "invitation": invitation["invitation"],
                "public_key": public_key.hex(),
            }
        ).encode(),
    )
    return enrolled, private_key


def signed_headers(
    enrolled: dict,
    private_key: bytes,
    *,
    method: str,
    path: str,
    body: bytes,
    idempotency_key: str,
    revision: str,
) -> dict[str, str]:
    timestamp = int(time.time())
    nonce = secrets.token_urlsafe(24)
    canonical = canonical_request(
        method,
        path,
        enrolled["signing_namespace"],
        timestamp,
        nonce,
        digest_body(body),
        idempotency_key,
        revision,
    )
    return {
        "Content-Type": "application/json",
        "Ocp-Apim-Subscription-Key": enrolled["subscription_key"],
        "X-Writer-Credential": enrolled["credential"],
        "X-Writer-Timestamp": str(timestamp),
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": idempotency_key,
        "X-Writer-Revision": revision,
        "X-Writer-Signature": sign_request_ed25519(private_key, canonical),
    }


class Published(NamedTuple):
    """What one publish put on the server, for the operator to read back.

    The object and batch counts are here because the whole point of the
    harness is checking what reached the device: "published 1 object" and
    "published 340 objects in 2 batches" are the difference between a
    Dashboard that renders and one stuck on ``.noData``.
    """

    ftp_watts: float
    objects: int
    batches: int


def publish_snapshot(server: LocalServer, enrolled: dict, private_key: bytes,
                     db_path: Path, user_id: int) -> Published:
    # A rider with enough activities pushes the derived objects (profile,
    # training_state, load_point, curve, ...) past a single page: those
    # objects come after every activity/activity-detail object in
    # ``snapshot_objects``'s fixed, offset-addressable order. Paging here
    # keeps them from being silently truncated off a one-batch publish.
    try:
        batches = snapshot_publish_pages(
            db_path, user_id, batch_id="walking-skeleton-snapshot", revision=1,
        )
    except SnapshotError as exc:
        raise SystemExit(
            f"could not page the snapshot for user {user_id} in {db_path}: {exc}"
        )

    # Resolve FTP, and refuse to publish anything, before posting a single
    # batch: the iOS FTP round-trip debug screen reads it off the ``profile``
    # object (CloudClient.fetchFTPWatts), so this is the same value the rider
    # will see on the device, and a partial publish followed by "nothing to
    # publish" would be a worse failure than not publishing at all.
    ftp_watts = next(
        (
            obj.data["ftp"]
            for batch in batches
            for obj in batch.objects
            if obj.kind == "profile" and obj.data.get("ftp") is not None
        ),
        None,
    )
    if ftp_watts is None:
        raise SystemExit(
            f"user {user_id} has no FTP in {db_path}: nothing to publish. "
            "Set one on the desktop, or pass --user-id for a rider who has."
        )

    for batch in batches:
        body = json.dumps(
            {
                "batch_id": batch.batch_id,
                "revision": batch.revision,
                "objects": [item.wire() for item in batch.objects],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        result = post(
            f"{server.internal_url}/api/v1/sync/batches",
            signed_headers(
                enrolled, private_key,
                method="POST", path="/api/v1/sync/batches", body=body,
                idempotency_key=batch.batch_id, revision=str(batch.revision),
            ),
            body,
        )
        if result.get("accepted") != len(batch.objects):
            raise SystemExit(f"the server did not accept the snapshot: {result}")

    return Published(
        ftp_watts=float(ftp_watts),
        objects=sum(len(batch.objects) for batch in batches),
        batches=len(batches),
    )


def mint_pairing_code(server: LocalServer, enrolled: dict, private_key: bytes) -> dict:
    path = "/api/v1/devices/pairing-codes"
    return post(
        f"{server.internal_url}{path}",
        signed_headers(
            enrolled, private_key,
            method="POST", path=path, body=b"",
            # The route fixes both: there is nothing here for a caller to pick.
            idempotency_key="device-pairing-code", revision="0",
        ),
        b"",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db_path(),
                        help="local database to READ (never written)")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--lan", action="store_true",
                        help="bind every interface and advertise this machine's "
                             "LAN address, so a phone can reach the server")
    parser.add_argument("--no-xcconfig", action="store_true",
                        help="with --lan, do not write ios/WatTracker/Config/"
                             "Local.xcconfig")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--codes", type=int, default=1,
                        help="how many single-use pairing codes to mint")
    parser.add_argument("--json", type=Path, default=None,
                        help="also write the run's facts to this file")
    args = parser.parse_args()

    if args.lan:
        try:
            advertise_host = detect_lan_address()
        except LanAddressError as exc:
            raise SystemExit(str(exc))
        server = LocalServer("0.0.0.0", args.port, advertise_host=advertise_host)
    else:
        server = LocalServer(args.host, args.port)

    xcconfig = None
    if args.lan and not args.no_xcconfig:
        xcconfig = write_local_xcconfig(server.advertise_host, args.port)

    server.start()
    enrolled, private_key = enroll_writer(server)
    published = publish_snapshot(
        server, enrolled, private_key, args.db, args.user_id
    )
    ftp_watts = published.ftp_watts
    codes = [
        mint_pairing_code(server, enrolled, private_key)["pairing_code"]
        for _ in range(max(1, args.codes))
    ]

    facts = {
        "base_url": server.advertised_url,
        "database": str(args.db),
        "user_id": args.user_id,
        "ftp_watts": ftp_watts,
        "pairing_codes": codes,
    }
    if args.json is not None:
        args.json.write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")

    print(f"serving      {server.advertised_url}")
    if not server.reachable_off_box:
        print("             loopback only - no phone can reach this. "
              "Re-run with --lan for a device run.")
    if xcconfig is not None:
        print(f"xcconfig     {xcconfig} (gitignored; rebuild Debug to pick it up)")
    print(f"database     {args.db} (read-only)")
    print(f"published    {published.objects} objects in "
          f"{published.batches} batch(es); profile ftp = {ftp_watts}")
    for code in codes:
        print(f"pairing code {code}")
    print("Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
