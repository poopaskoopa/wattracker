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
# Container Apps scale to zero and measured cold starts are about 20 seconds.
_REQUEST_TIMEOUT_SECONDS = 30.0
_REQUEST_TIMEOUT_MESSAGE = (
    "cloud admin request timed out: service did not respond within 30 seconds, "
    "may be scaling up from zero; it should be retried"
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_HEX_ID_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")


class AdminError(RuntimeError):
    """A safe, user-facing failure that contains no secret material."""


class _RequestTimeout(AdminError):
    def __init__(self) -> None:
        super().__init__(_REQUEST_TIMEOUT_MESSAGE)


class _RevokeHTTPError(AdminError):
    def __init__(self, status: int, retry_after: str | None) -> None:
        super().__init__("cloud admin revoke request failed")
        self.status = status
        self.retry_after = retry_after


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


def _request_json(
    endpoint: str,
    path: str,
    token: str,
    *,
    method: str,
    preserve_revoke_status: bool = False,
) -> Mapping[str, Any]:
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
    except urllib.error.HTTPError as exc:
        if preserve_revoke_status:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            raise _RevokeHTTPError(exc.code, retry_after) from exc
        raise AdminError("cloud admin request failed") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise _RequestTimeout() from exc
        raise AdminError("cloud admin request failed") from exc
    except TimeoutError as exc:
        raise _RequestTimeout() from exc
    except (OSError, ValueError) as exc:
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


def _version(endpoint: str, token: str) -> dict[str, Any]:
    payload = _request_json(endpoint, "/api/v1/admin/version", token, method="GET")
    commit = payload.get("commit")
    if not isinstance(commit, str) or (commit != "source" and _COMMIT_RE.fullmatch(commit) is None):
        raise AdminError("cloud admin response was invalid")
    result = {"commit": commit}
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
        if "operator_handle" not in row and "installation_id" in row:
            raise AdminError(
                "cloud admin response uses the old installation_id field; "
                "the deployed image predates this CLI; readImage/syncImage must be "
                "re-pinned to the current digest and redeployed"
            )
        operator_handle = row.get("operator_handle")
        status = row.get("status")
        capabilities = row.get("capabilities")
        algorithm = row.get("signature_algorithm")
        if (
            not isinstance(operator_handle, str)
            or status not in {"active", "revoked"}
            or not isinstance(capabilities, list)
            or not all(isinstance(item, str) for item in capabilities)
            or not isinstance(algorithm, str)
        ):
            raise AdminError("cloud admin response was invalid")
        result_rows.append({
            "operator_handle": operator_handle,
            "status": status,
            "capabilities": capabilities,
            "signature_algorithm": algorithm,
        })
    result = {"installations": result_rows}
    if _contains_token(result, token):
        raise AdminError("cloud admin response was invalid")
    return result


def _revoke_installation(endpoint: str, token: str, operator_handle: str) -> dict[str, Any]:
    if not isinstance(operator_handle, str) or _HEX_ID_RE.fullmatch(operator_handle) is None:
        raise AdminError("invalid operator handle or credential id")
    payload = _request_json(
        endpoint,
        "/api/v1/admin/installations/"
        + urllib.parse.quote(operator_handle, safe="")
        + "/revoke",
        token,
        method="POST",
        preserve_revoke_status=True,
    )
    result = {
        "operator_handle": payload.get("operator_handle"),
        "status": payload.get("status"),
    }
    if (
        not isinstance(result["operator_handle"], str)
        or _HEX_ID_RE.fullmatch(result["operator_handle"]) is None
        or result["status"] != "revoked"
    ):
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
    for name in ("invite", "version", "list-installations"):
        help_text = (
            "list writer installations; output contains opaque operator handles"
            if name == "list-installations"
            else "report the running cloud image commit"
            if name == "version"
            else None
        )
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--endpoint", default=argparse.SUPPRESS)
    revoke = commands.add_parser(
        "revoke-installation",
        help="revoke by opaque operator handle or writer credential id",
        description="Revoke by opaque operator handle or writer credential id.",
    )
    revoke.add_argument(
        "operator_handle",
        help="opaque operator handle or real writer credential id",
    )
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
        elif args.command == "version":
            result = _version(endpoint, token)
        elif args.command == "list-installations":
            result = _list_installations(endpoint, token)
        else:
            result = _revoke_installation(endpoint, token, args.operator_handle)
        json.dump(result, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except _RevokeHTTPError as exc:
        if exc.status == 404:
            print("no such installation", file=sys.stderr)
        elif exc.status == 503:
            retry_after = exc.retry_after
            if (
                retry_after is not None
                and len(retry_after.strip()) <= 5
                and re.fullmatch(r"[0-9]+", retry_after.strip())
            ):
                seconds = int(retry_after.strip())
                if 0 <= seconds <= 86400:
                    print(
                        f"cloud admin unavailable; retry in {seconds} seconds",
                        file=sys.stderr,
                    )
                else:
                    print("cloud admin unavailable; retry later", file=sys.stderr)
            else:
                print("cloud admin unavailable; retry later", file=sys.stderr)
        else:
            print("cloud admin request failed", file=sys.stderr)
        return 2
    except AdminError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
