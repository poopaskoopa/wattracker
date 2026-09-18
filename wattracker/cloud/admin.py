"""Operator bootstrap client for the Wattracker cloud API.

The operator token is intentionally not a command-line option.  It is read
from the environment or the OS keychain and is sent only as an HTTP header.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .credentials import CloudCredentialUnavailable, KeyringBackend

_ENDPOINT_ENV = "WATTRACKER_CLOUD_ENDPOINT"
_TOKEN_ENV = "WATTRACKER_CLOUD_OPERATOR_TOKEN"
_KEYCHAIN_ACCOUNT = "operator-token"
_MAX_RESPONSE_BYTES = 256 * 1024
_REQUEST_TIMEOUT_SECONDS = 15.0
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_INSTALLATION_ID_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class AdminError(RuntimeError):
    """A safe, user-facing failure that contains no secret material."""


class _ArgumentError(AdminError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        # argparse includes unknown argument text in its default error.  That
        # text could contain a token supplied by mistake, so never echo it.
        raise _ArgumentError("invalid command-line arguments")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _validate_endpoint(value: str) -> str:
    """Validate and normalize an absolute cloud endpoint.

    HTTPS is required everywhere except the exact loopback hostnames needed
    by the local walking-skeleton server.  Credentials, queries, and
    fragments are rejected so the endpoint cannot smuggle secret-like values
    into URLs or logs.
    """

    if not isinstance(value, str) or not value or any(ord(char) < 0x21 for char in value):
        raise AdminError("invalid cloud endpoint")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        parsed.port  # Validate a supplied port without retaining it.
    except ValueError as exc:
        raise AdminError("invalid cloud endpoint") from exc
    if (
        not parsed.scheme
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AdminError("invalid cloud endpoint")
    scheme = parsed.scheme.lower()
    host = hostname.lower()
    if scheme == "https":
        pass
    elif scheme == "http" and host in _LOOPBACK_HOSTS:
        pass
    else:
        raise AdminError("cloud endpoint must use HTTPS")
    return value.rstrip("/")


def _load_operator_token() -> str:
    value = os.environ.get(_TOKEN_ENV)
    if isinstance(value, str) and value:
        return value
    try:
        value = KeyringBackend().get(_KEYCHAIN_ACCOUNT)
    except CloudCredentialUnavailable as exc:
        raise AdminError("operator token is unavailable") from exc
    except Exception as exc:
        raise AdminError("operator token is unavailable") from exc
    if not isinstance(value, str) or not value:
        raise AdminError("operator token is unavailable")
    return value


def _request_json(endpoint: str, path: str, token: str, *, method: str) -> Mapping[str, Any]:
    url = _validate_endpoint(endpoint) + path
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Operator-Token": token,
        },
        method=method,
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
        raise AdminError("cloud admin request failed") from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise AdminError("cloud admin response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdminError("cloud admin response was invalid") from exc
    if not isinstance(payload, Mapping):
        raise AdminError("cloud admin response was invalid")
    return payload


def _contains_token(value: Any, token: str) -> bool:
    if isinstance(value, str):
        return token in value
    if isinstance(value, Mapping):
        return any(_contains_token(key, token) or _contains_token(child, token)
                   for key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_token(child, token) for child in value)
    return False


def _invite(endpoint: str, token: str) -> dict[str, Any]:
    payload = _request_json(endpoint, "/api/v1/enrollment/start", token, method="POST")
    result = {"invitation": payload.get("invitation"), "expires_at": payload.get("expires_at")}
    if not isinstance(result["invitation"], str) or result["expires_at"] is None:
        raise AdminError("cloud admin response was invalid")
    if _contains_token(result, token):
        raise AdminError("cloud admin response was invalid")
    return result


def _list_installations(endpoint: str, token: str) -> dict[str, Any]:
    payload = _request_json(endpoint, "/api/v1/admin/installations", token, method="GET")
    rows = payload.get("installations")
    if not isinstance(rows, list):
        raise AdminError("cloud admin response was invalid")
    result_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise AdminError("cloud admin response was invalid")
        installation_id = row.get("installation_id")
        status = row.get("status")
        capabilities = row.get("capabilities")
        algorithm = row.get("signature_algorithm")
        if (
            not isinstance(installation_id, str)
            or status not in {"active", "revoked"}
            or not isinstance(capabilities, list)
            or not all(isinstance(item, str) for item in capabilities)
            or not isinstance(algorithm, str)
        ):
            raise AdminError("cloud admin response was invalid")
        result_rows.append({
            "installation_id": installation_id,
            "status": status,
            "capabilities": capabilities,
            "signature_algorithm": algorithm,
        })
    result = {"installations": result_rows}
    if _contains_token(result, token):
        raise AdminError("cloud admin response was invalid")
    return result


def _revoke_installation(endpoint: str, token: str, installation_id: str) -> dict[str, Any]:
    if not isinstance(installation_id, str) or _INSTALLATION_ID_RE.fullmatch(installation_id) is None:
        raise AdminError("invalid installation id")
    payload = _request_json(
        endpoint,
        "/api/v1/admin/installations/"
        + urllib.parse.quote(installation_id, safe="")
        + "/revoke",
        token,
        method="POST",
    )
    result = {
        "installation_id": payload.get("installation_id"),
        "status": payload.get("status"),
    }
    if result["installation_id"] != installation_id or result["status"] != "revoked":
        raise AdminError("cloud admin response was invalid")
    if _contains_token(result, token):
        raise AdminError("cloud admin response was invalid")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="python -m wattracker.cloud.admin")
    parser.add_argument("--endpoint", default=None, help="absolute cloud endpoint")
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_ArgumentParser
    )
    for name in ("invite", "list-installations"):
        command = commands.add_parser(name)
        command.add_argument("--endpoint", default=argparse.SUPPRESS)
    revoke = commands.add_parser("revoke-installation")
    revoke.add_argument("installation_id")
    revoke.add_argument("--endpoint", default=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        endpoint_value = args.endpoint or os.environ.get(_ENDPOINT_ENV, "")
        endpoint = _validate_endpoint(endpoint_value)
        token = _load_operator_token()
        if args.command == "invite":
            result = _invite(endpoint, token)
        elif args.command == "list-installations":
            result = _list_installations(endpoint, token)
        else:
            result = _revoke_installation(endpoint, token, args.installation_id)
        json.dump(result, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except AdminError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
