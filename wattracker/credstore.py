"""Per-user Zwift credential storage.

The email lives in ``user_settings``; the password never touches the DB in the
clear. Two backends, best available wins:

1. **keyring** (macOS Keychain / OS credential vault) when the package is
   importable and working - the password is stored under service
   ``wattracker-Zwift`` keyed by user id, nothing password-shaped in the DB
   (a sentinel marks which backend holds it).
2. **Encrypted at rest** fallback: an HMAC-SHA256 keystream cipher (CTR-style,
   random 16-byte nonce, constant-time-decryptable with the same key) using a
   per-install random 32-byte key at ``<data dir>/credentials.key`` created
   with 0600 permissions. Not HSM-grade, but the DB alone (or a copied DB)
   can't reveal the password.

The env var WATTRACKER_KEYRING=0 forces the file-key backend (the test suite
sets it so tests never touch the real Keychain).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as _secrets
from typing import NamedTuple, Optional

from . import config, db

_SERVICE = "wattracker-Zwift"
_KEYRING_SENTINEL = "@keyring"
_KEY_FILE = "credentials.key"
_ENC_PREFIX = "enc1$"


class ZwiftCredentials(NamedTuple):
    email: str
    password: str


# ------------------------------------------------------------- keyring
def _keyring_enabled() -> bool:
    return os.environ.get("WATTRACKER_KEYRING", "1") not in ("0", "false", "no")


def _keyring():
    """The keyring module, or None when unavailable/disabled."""
    if not _keyring_enabled():
        return None
    try:
        import keyring
        from keyring.errors import KeyringError  # noqa: F401

        # A fail-backend (no vault available) raises on use; probe cheaply.
        backend = keyring.get_keyring()
        if backend.__class__.__name__.lower().startswith("fail"):
            return None
        return keyring
    except Exception:
        return None


def storage_backend() -> str:
    """Human-readable name of the active password backend (for the UI)."""
    return "system keychain" if _keyring() else "encrypted local file key"


# ----------------------------------------------------- file-key fallback
def _install_key() -> bytes:
    """Per-install random key, created 0600 under the app data dir."""
    path = os.path.join(config.app_data_dir(), _KEY_FILE)
    if not os.path.exists(path):
        key = _secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key
    with open(path, "rb") as f:
        return f.read()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"),
                        hashlib.sha256).digest()
        counter += 1
    return out[:length]


def _encrypt(plaintext: str) -> str:
    key = _install_key()
    nonce = _secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    return _ENC_PREFIX + base64.b64encode(nonce + cipher).decode("ascii")


def _decrypt(token: str) -> Optional[str]:
    if not token or not token.startswith(_ENC_PREFIX):
        return None
    try:
        raw = base64.b64decode(token[len(_ENC_PREFIX):])
        nonce, cipher = raw[:16], raw[16:]
        key = _install_key()
        data = bytes(a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher))))
        return data.decode("utf-8")
    except Exception:
        return None


# ------------------------------------------------------------ public API
def save_zwift_credentials(user_id: int, email: str, password: str) -> str:
    """Store credentials; returns the backend name used."""
    email = (email or "").strip()
    if not email or not password:
        raise ValueError("both email and password are required")
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_SERVICE, f"user{user_id}", password)
            db.set_zwift_credentials_row(user_id, email, _KEYRING_SENTINEL)
            return "system keychain"
        except Exception:
            pass  # vault refused: fall through to the encrypted file key
    db.set_zwift_credentials_row(user_id, email, _encrypt(password))
    return "encrypted local file key"


def get_zwift_credentials(user_id: int) -> Optional[ZwiftCredentials]:
    email, enc = db.get_zwift_credentials_row(user_id)
    if not email or not enc:
        return None
    if enc == _KEYRING_SENTINEL:
        kr = _keyring()
        if kr is None:
            return None
        try:
            password = kr.get_password(_SERVICE, f"user{user_id}")
        except Exception:
            return None
        return ZwiftCredentials(email, password) if password else None
    password = _decrypt(enc)
    return ZwiftCredentials(email, password) if password else None


def credentials_saved(user_id: int) -> bool:
    email, enc = db.get_zwift_credentials_row(user_id)
    return bool(email and enc)


def clear_zwift_credentials(user_id: int) -> None:
    _email, enc = db.get_zwift_credentials_row(user_id)
    if enc == _KEYRING_SENTINEL:
        kr = _keyring()
        if kr is not None:
            try:
                kr.delete_password(_SERVICE, f"user{user_id}")
            except Exception:
                pass
    db.set_zwift_credentials_row(user_id, None, None)
