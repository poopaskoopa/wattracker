"""Password hashing/verification using stdlib hashlib.scrypt with per-user salt.

Stored format (current): ``scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>`` - the cost
parameters are encoded per-hash so they can be raised over time without breaking
older stored hashes. The legacy 3-field format ``scrypt$<salt_hex>$<hash_hex>``
(which implies n=16384, r=8, p=1) is still accepted on verify; ``needs_rehash``
flags such hashes so the login handler can transparently upgrade them.

No plaintext is ever stored.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time as _time
from typing import Optional

# Current cost. scrypt memory use is ~128*N*r bytes (~128 MiB here), which
# exceeds OpenSSL's default 32 MiB cap, so maxmem must be set explicitly.
_N = 2 ** 17  # 131072 (2**17)
_R = 8
_P = 1
_LEGACY_N = 16384  # 2**14, the cost of pre-upgrade 3-field hashes
_DKLEN = 32
_SALT_BYTES = 16
_MAXMEM = 128 * _N * _R * 2  # generous headroom over the 128*N*r requirement

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 1024  # cap scrypt input so a huge password can't DoS the CPU


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=_DKLEN, maxmem=_MAXMEM,
    )


def hash_password(password: str) -> str:
    """Hash a password with a fresh random salt at the current cost."""
    salt = os.urandom(_SALT_BYTES)
    dk = _derive(password, salt, _N, _R, _P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def _parse(stored: str) -> "Optional[tuple[int, int, int, str, str]]":
    """Parse a stored hash into (n, r, p, salt_hex, hash_hex), or None."""
    parts = stored.split("$")
    if len(parts) == 6:
        algo, n, r, p, salt_hex, hash_hex = parts
        if algo != "scrypt":
            return None
        return int(n), int(r), int(p), salt_hex, hash_hex
    if len(parts) == 3:
        algo, salt_hex, hash_hex = parts
        if algo != "scrypt":
            return None
        return _LEGACY_N, _R, _P, salt_hex, hash_hex
    return None


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify a password against a stored hash (new or legacy)."""
    try:
        parsed = _parse(stored)
        if parsed is None:
            return False
        n, r, p, salt_hex, hash_hex = parsed
        salt = bytes.fromhex(salt_hex)
        dk = _derive(password, salt, n, r, p)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    """True when a stored hash should be re-hashed at the current cost.

    That is: the legacy 3-field format, or any hash whose N is below the current
    target. Unparseable blobs return False (nothing to upgrade to).
    """
    parsed = _parse(stored)
    if parsed is None:
        return False
    return parsed[0] < _N


# A fixed hash so the login handler can spend the same scrypt time on a missing
# user as on a real one, avoiding a username-enumeration timing oracle.
_DUMMY_HASH = hash_password("wattracker::no-such-user")


def dummy_verify(password: str) -> None:
    """Run a throwaway verify to equalize timing when the user doesn't exist."""
    verify_password(password, _DUMMY_HASH)


def validate_credentials(username: str, password: str) -> "str | None":
    """Return an error message if credentials are invalid, else None."""
    if not username or not username.strip():
        return "Username is required."
    if not password or len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if len(password) > MAX_PASSWORD_LEN:
        return f"Password must be at most {MAX_PASSWORD_LEN} characters."
    return None


class LoginThrottle:
    """In-process per-username failed-login throttle with exponential backoff.

    Single-process app, so a plain dict under a lock is sufficient. Keyed by the
    lowercased username. After ``threshold`` consecutive failures a lockout
    window opens, doubling each further failure up to ``max_seconds``; a success
    clears the counter. ``clock`` is injectable for deterministic tests.
    """

    def __init__(
        self,
        threshold: int = 5,
        base_seconds: float = 1.0,
        max_seconds: float = 300.0,
        clock=_time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._base = base_seconds
        self._max = max_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # key -> [consecutive_failures, locked_until_monotonic]
        self._state: "dict[str, list]" = {}

    @staticmethod
    def _key(username: str) -> str:
        return (username or "").strip().lower()

    def retry_after(self, username: str) -> float:
        """Seconds the caller must wait before another attempt, 0.0 if allowed."""
        key = self._key(username)
        with self._lock:
            st = self._state.get(key)
            if not st:
                return 0.0
            remaining = st[1] - self._clock()
            return remaining if remaining > 0 else 0.0

    def record_failure(self, username: str) -> None:
        key = self._key(username)
        with self._lock:
            st = self._state.setdefault(key, [0, 0.0])
            st[0] += 1
            if st[0] >= self._threshold:
                over = st[0] - self._threshold
                backoff = min(self._base * (2 ** over), self._max)
                st[1] = self._clock() + backoff

    def record_success(self, username: str) -> None:
        key = self._key(username)
        with self._lock:
            self._state.pop(key, None)
