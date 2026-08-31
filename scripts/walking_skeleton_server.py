#!/usr/bin/env python3
"""Run the walking skeleton (#171) end to end against a local server.

What this does, in order:

1. Reads the rider's FTP out of the local database, READ-ONLY, and builds one
   ``profile`` object from it.
2. Starts the real cloud app on ``127.0.0.1`` with an in-memory store.
3. Enrolls a writer through the real enrollment routes, with a real Ed25519
   keypair generated in this process and never written anywhere.
4. Pushes the profile through the real, signed ``/api/v1/sync/batches``.
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
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
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
from wattracker.cloud.snapshot import profile_batch  # noqa: E402

OPERATOR_SUBJECT = "walking-skeleton-operator"


def default_db_path() -> Path:
    override = os.environ.get("WATTRACKER_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".wattracker" / "wattracker.db"


class LocalServer:
    """The cloud app on a loopback port, in a thread."""

    def __init__(self, host: str, port: int) -> None:
        import uvicorn

        self.host = host
        self.port = port
        self.config = CloudConfig(
            server_secret=secrets.token_bytes(32),
            # A fresh operator token per run: it authorizes minting an
            # enrollment invitation, and a fixed one in a repository would be
            # a credential in a repository.
            operator_token=secrets.token_urlsafe(24),
            plane="all",
            require_subscription=True,
            # No APIM in front of a loopback server, so no proof to demand and
            # -- crucially -- no verified-subject header to trust. Both flags
            # off together is the honest configuration: a deployment that
            # claimed to enforce a subject with nothing issuing one would be a
            # control in name only, and CloudState refuses to start that way.
            require_apim_proof=False,
            require_verified_subject=False,
            allowed_origins=(),
        )
        self.state = CloudState.create(
            self.config, security_backend=MemorySecurityStateBackend()
        )
        app = create_cloud_app(self.config, state=self.state)
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

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
        f"{server.base_url}/api/v1/enrollment/start",
        {
            "Content-Type": "application/json",
            "X-Operator-Token": server.config.operator_token,
            server.config.verified_subject_header: OPERATOR_SUBJECT,
        },
        b"{}",
    )
    enrolled = post(
        f"{server.base_url}/api/v1/enrollment/complete",
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


def publish_profile(server: LocalServer, enrolled: dict, private_key: bytes,
                    db_path: Path, user_id: int) -> float:
    batch = profile_batch(
        db_path, user_id, batch_id="walking-skeleton-profile", revision=1
    )
    if batch is None:
        raise SystemExit(
            f"user {user_id} has no FTP in {db_path}: nothing to publish. "
            "Set one on the desktop, or pass --user-id for a rider who has."
        )
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
        f"{server.base_url}/api/v1/sync/batches",
        signed_headers(
            enrolled, private_key,
            method="POST", path="/api/v1/sync/batches", body=body,
            idempotency_key=batch.batch_id, revision=str(batch.revision),
        ),
        body,
    )
    if result.get("accepted") != 1:
        raise SystemExit(f"the server did not accept the profile: {result}")
    return float(batch.objects[0].data["ftp_watts"])


def mint_pairing_code(server: LocalServer, enrolled: dict, private_key: bytes) -> dict:
    path = "/api/v1/devices/pairing-codes"
    return post(
        f"{server.base_url}{path}",
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
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--codes", type=int, default=1,
                        help="how many single-use pairing codes to mint")
    parser.add_argument("--json", type=Path, default=None,
                        help="also write the run's facts to this file")
    args = parser.parse_args()

    server = LocalServer(args.host, args.port)
    server.start()
    enrolled, private_key = enroll_writer(server)
    ftp_watts = publish_profile(
        server, enrolled, private_key, args.db, args.user_id
    )
    codes = [
        mint_pairing_code(server, enrolled, private_key)["pairing_code"]
        for _ in range(max(1, args.codes))
    ]

    facts = {
        "base_url": server.base_url,
        "database": str(args.db),
        "user_id": args.user_id,
        "ftp_watts": ftp_watts,
        "pairing_codes": codes,
    }
    if args.json is not None:
        args.json.write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")

    print(f"serving      {server.base_url}")
    print(f"database     {args.db} (read-only)")
    print(f"published    profile ftp_watts = {ftp_watts}")
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
