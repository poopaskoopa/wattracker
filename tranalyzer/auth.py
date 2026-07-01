"""Password hashing/verification using stdlib hashlib.scrypt with per-user salt.

Stored format: ``scrypt$<salt_hex>$<hash_hex>``. No plaintext is ever stored.
"""
from __future__ import annotations

import hashlib
import hmac
import os

_N = 16384  # CPU/memory cost (2**14)
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16

MIN_PASSWORD_LEN = 8


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )


def hash_password(password: str) -> str:
    """Hash a password with a fresh random salt."""
    salt = os.urandom(_SALT_BYTES)
    dk = _derive(password, salt)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify a password against a stored ``scrypt$salt$hash``."""
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        dk = _derive(password, salt)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def validate_credentials(username: str, password: str) -> "str | None":
    """Return an error message if credentials are invalid, else None."""
    if not username or not username.strip():
        return "Username is required."
    if not password or len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    return None
