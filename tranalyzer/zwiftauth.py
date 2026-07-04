"""Authenticated Zwift / ZwiftPower access (community-standard flows).

Flows implemented (verified against currently-maintained community wrappers,
e.g. rally25rs/zwift-api-wrapper and Sauce4Zwift, 2026-07):

1. **Zwift SSO** - OAuth2 resource-owner password grant against
   ``https://secure.zwift.com/auth/realms/zwift/protocol/openid-connect/token``
   with the public client id ``Zwift_Mobile_Link`` (falling back once to
   ``Zwift Game Client`` if the realm rejects the client id). Yields
   access/refresh tokens; ``refresh_sso_token`` exchanges the refresh token.
   Used here for rider-ID auto-detection via
   ``https://us-or-rly101.zwift.com/api/profiles/me``.

2. **ZwiftPower session cookies** - the phpBB SSO redirect dance:
   GET ``https://zwiftpower.com/ucp.php?mode=login&login=external&
   oauth_service=oauthzpsso`` (redirects to the secure.zwift.com Keycloak
   login page), POST the credentials to the page's ``<form action=...>``,
   follow the redirect back to zwiftpower.com - the cookie jar then holds a
   valid session for the JSON endpoints (``cache3/profile/{id}_all.json``).

Politeness: every function performs exactly one attempt (plus at most one
alternate-client-id retry that never re-sends on bad credentials); callers
surface ``ZwiftAuthError`` instead of retrying.
"""
from __future__ import annotations

import html as _html
import http.cookiejar
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

AUTH_HOST = "https://secure.zwift.com"
TOKEN_URL = AUTH_HOST + "/auth/realms/zwift/protocol/openid-connect/token"
CLIENT_IDS = ("Zwift_Mobile_Link", "Zwift Game Client")
API_HOST = "https://us-or-rly101.zwift.com"
PROFILE_ME_URL = API_HOST + "/api/profiles/me"
ZP_SSO_URL = ("https://zwiftpower.com/ucp.php?mode=login&login=external"
              "&oauth_service=oauthzpsso")

_UA = "TRanalyzer/0.1 (local training analyzer)"
_TIMEOUT_S = 20


class ZwiftAuthError(Exception):
    """Zwift/ZwiftPower authentication failed (bad credentials, SSO change...).

    ``credential_problem`` is True only when the credentials themselves were
    rejected - callers use it to pause automatic retries (never a transient
    network error) so accounts can't be locked by retry storms.
    """

    def __init__(self, message: str, credential_problem: bool = False) -> None:
        super().__init__(message)
        self.credential_problem = credential_problem


def _read(resp) -> str:
    return resp.read().decode("utf-8", "replace")


def sso_token(email: str, password: str, timeout: float = _TIMEOUT_S) -> Dict:
    """Password-grant token exchange. Returns the token JSON.

    Exactly one attempt per client id, and the alternate client id is tried
    only for client-id rejections - never for bad credentials (no lockouts).
    """
    last_error = "no client id accepted"
    for client_id in CLIENT_IDS:
        body = urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": client_id,
                "username": email,
                "password": password,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            TOKEN_URL, data=body,
            headers={"User-Agent": _UA,
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(_read(resp))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8", "replace")).get(
                    "error", "")
            except Exception:
                pass
            if detail in ("invalid_client", "unauthorized_client"):
                last_error = f"client id rejected ({client_id})"
                continue  # try the alternate public client id once
            raise ZwiftAuthError(
                "Zwift login failed - check your email and password"
                + (f" ({detail})" if detail else ""),
                credential_problem=True,
            ) from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise ZwiftAuthError(f"Zwift SSO unreachable: {e}") from e
        except ValueError as e:
            raise ZwiftAuthError(f"Zwift SSO returned bad JSON: {e}") from e
    raise ZwiftAuthError(f"Zwift SSO rejected the request: {last_error}")


def refresh_sso_token(refresh_token: str, timeout: float = _TIMEOUT_S) -> Dict:
    """Exchange a refresh token for a new access token."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_IDS[0],
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"User-Agent": _UA,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(_read(resp))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        raise ZwiftAuthError(f"token refresh failed: {e}") from e


def fetch_profile_me(access_token: str, timeout: float = _TIMEOUT_S) -> Dict:
    """The authenticated rider's own Zwift profile (contains numeric ``id``)."""
    req = urllib.request.Request(
        PROFILE_ME_URL,
        headers={"User-Agent": _UA, "Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(_read(resp))
    except urllib.error.HTTPError as e:
        raise ZwiftAuthError(f"Zwift profile fetch failed (HTTP {e.code})") from e
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        raise ZwiftAuthError(f"Zwift profile fetch failed: {e}") from e


def zwiftpower_login(email: str, password: str, timeout: float = _TIMEOUT_S):
    """Run the ZwiftPower SSO dance; returns an opener with session cookies."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", _UA)]
    try:
        with opener.open(ZP_SSO_URL, timeout=timeout) as resp:
            page = _read(resp)
            landed = resp.geturl()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise ZwiftAuthError(f"ZwiftPower SSO unreachable: {e}") from e

    if "zwiftpower.com" in urllib.parse.urlparse(landed).netloc:
        return opener  # already had a valid session (cookie reuse)

    m = re.search(r'<form[^>]+action="([^"]+)"', page)
    if not m:
        raise ZwiftAuthError("Zwift SSO login form not found (flow changed?)")
    action = _html.unescape(m.group(1))
    body = urllib.parse.urlencode(
        {"username": email, "password": password}
    ).encode("utf-8")
    try:
        with opener.open(action, data=body, timeout=timeout) as resp:
            final = resp.geturl()
            final_page = _read(resp)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise ZwiftAuthError(f"Zwift SSO login failed: {e}") from e

    # Keycloak re-renders its login form (still on secure.zwift.com) on bad
    # credentials; success redirects back to zwiftpower.com.
    if "zwiftpower.com" not in urllib.parse.urlparse(final).netloc:
        raise ZwiftAuthError(
            "Zwift login failed - check your email and password",
            credential_problem=True,
        )
    del final_page  # content unused; the cookies are the point
    return opener


def fetch_zwiftpower_json(opener, url: str, timeout: float = _TIMEOUT_S) -> Dict:
    """GET a ZwiftPower JSON endpoint with an authenticated opener."""
    try:
        with opener.open(url, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            body = _read(resp)
    except urllib.error.HTTPError as e:
        raise ZwiftAuthError(f"ZwiftPower rejected the session (HTTP {e.code})") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise ZwiftAuthError(f"ZwiftPower unreachable: {e}") from e
    if "json" not in ctype:
        raise ZwiftAuthError("ZwiftPower session invalid (got a login page)")
    try:
        return json.loads(body)
    except ValueError as e:
        raise ZwiftAuthError(f"ZwiftPower returned bad JSON: {e}") from e


def detect_rider_id(email: str, password: str) -> Tuple[str, Dict]:
    """Log in to Zwift SSO and return (rider_id, token) from /api/profiles/me."""
    token = sso_token(email, password)
    profile = fetch_profile_me(token.get("access_token") or "")
    rid = profile.get("id")
    if not rid:
        raise ZwiftAuthError("Zwift profile has no rider id")
    return str(rid), token


def fetch_results_authenticated(
    email: str,
    password: str,
    rider_id: Optional[str] = None,
    profile_url_template: Optional[str] = None,
) -> Tuple[Dict, str]:
    """One authenticated fetch: SSO (+rider-id auto-detect) then ZwiftPower.

    Returns (profile results JSON document, rider_id). Raises ZwiftAuthError
    on any authentication problem.
    """
    if not rider_id or not str(rider_id).isdigit():
        rider_id, _token = detect_rider_id(email, password)
    opener = zwiftpower_login(email, password)
    template = profile_url_template or (
        "https://zwiftpower.com/cache3/profile/{rider_id}_all.json"
    )
    doc = fetch_zwiftpower_json(opener, template.format(rider_id=rider_id))
    return doc, str(rider_id)
