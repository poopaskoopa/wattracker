"""Per-user Zwift credential storage.

The email lives in ``user_settings``; the password never touches the DB in the
clear. Two backends, best available wins:

1. **keyring** (macOS Keychain / OS credential vault) when the package is
   importable and working - the password is stored under service
   ``wattracker-Zwift`` keyed by user id, nothing password-shaped in the DB
   (a sentinel marks which backend holds it).
2. On Windows, **DPAPI CurrentUser** when Credential Manager is unavailable.
   DPAPI binds the encrypted DB blob to the current Windows account and uses
   service/user context as additional entropy.  Windows never creates the
   file-key format.
3. On other platforms, **encrypted at rest** fallback: an HMAC-SHA256
   keystream cipher (CTR-style, random 16-byte nonce) with an HMAC-SHA256
   authentication tag over nonce||ciphertext, using independent enc/mac
   subkeys derived from a per-install random 32-byte key at
   ``<data dir>/credentials.key`` (created 0600). The authenticated ``enc2$``
   format is tamper-evident; old ``enc1$`` rows remain readable. Not HSM-grade,
   but a DB copy alone cannot reveal or silently alter the password.

The env var WATTRACKER_KEYRING=0 disables system keyring access.  The test
suite sets it so tests never touch the real keychain; Windows then uses DPAPI,
while other platforms use the authenticated file-key fallback.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as _secrets
import sys
from typing import NamedTuple, Optional

from . import config, db, windows_secrets

_SERVICE = "wattracker-Zwift"
_KEYRING_SENTINEL = "@keyring"
_KEYRING_PREFIX = "@keyring:v1:"
_KEY_FILE = "credentials.key"
_ENC_PREFIX = "enc1$"       # legacy: unauthenticated (still decrypted)
_ENC_PREFIX_V2 = "enc2$"    # current: authenticated (HMAC tag appended)
_TAG_LEN = 32               # HMAC-SHA256 digest length
_DPAPI_PREFIX = "dpapi1$"


class CredentialStorageError(RuntimeError):
    """No secure credential backend accepted the requested operation."""


class ZwiftCredentials(NamedTuple):
    email: str
    password: str


# ------------------------------------------------------------- keyring
def _keyring_enabled() -> bool:
    return os.environ.get("WATTRACKER_KEYRING", "1") not in ("0", "false", "no")


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _keyring_backend_allowed(backend) -> bool:
    """Reject known fail/plaintext stores and require WinVault on Windows."""
    cls = backend.__class__
    name = cls.__name__.lower()
    module = cls.__module__.lower()
    if name.startswith("fail") or "plaintext" in name or "plaintext" in module:
        return False
    if _is_windows():
        return module == "keyring.backends.windows" and "winvault" in name
    return True


def _keyring():
    """The keyring module, or None when unavailable/disabled."""
    if not _keyring_enabled():
        return None
    try:
        import keyring
        from keyring.errors import KeyringError  # noqa: F401

        # A fail-backend (no vault available) raises on use; probe cheaply.
        backend = keyring.get_keyring()
        if not _keyring_backend_allowed(backend):
            return None
        return keyring
    except Exception:
        return None


def _keyring_target(user_id: int, marker: Optional[str]) -> Optional[str]:
    """Resolve legacy/versioned DB markers to their exact vault target."""
    if marker == _KEYRING_SENTINEL:
        return f"user{user_id}"
    if not marker or not marker.startswith(_KEYRING_PREFIX):
        return None
    slot = marker[len(_KEYRING_PREFIX):]
    if len(slot) != 32 or any(c not in "0123456789abcdef" for c in slot):
        return None
    return f"user{user_id}:v1:{slot}"


def _new_keyring_slot(user_id: int) -> "tuple[str, str]":
    slot = _secrets.token_hex(16)
    return _KEYRING_PREFIX + slot, f"user{user_id}:v1:{slot}"


def storage_backend() -> str:
    """Human-readable name of the active password backend (for the UI)."""
    if _keyring():
        return "system keychain"
    if _is_windows():
        return (
            "Windows DPAPI"
            if windows_secrets.is_available()
            else "secure credential storage unavailable"
        )
    return "encrypted local file key"


# ----------------------------------------------------- file-key fallback
def _install_key(*, create: bool = True) -> bytes:
    """Per-install random key, created 0600 under the app data dir."""
    path = os.path.join(config.app_data_dir(), _KEY_FILE)
    if not os.path.exists(path):
        if not create:
            raise FileNotFoundError(path)
        key = _secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        config._restrict(path, 0o600)
        return key
    # Self-heal file permissions for installs created before storage
    # hardening. On Windows this is only relevant to legacy reads; no new
    # file-backed credential is ever created there.
    config._restrict(path, 0o600)
    with open(path, "rb") as f:
        key = f.read()
    if len(key) != 32:
        raise ValueError("invalid credential key")
    return key


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"),
                        hashlib.sha256).digest()
        counter += 1
    return out[:length]


def _subkeys(key: bytes) -> "tuple[bytes, bytes]":
    """Derive independent (enc, mac) subkeys from the file key (HKDF-style)."""
    enc = hmac.new(key, b"wattracker-cred-enc", hashlib.sha256).digest()
    mac = hmac.new(key, b"wattracker-cred-mac", hashlib.sha256).digest()
    return enc, mac


def _encrypt(plaintext: str) -> str:
    key = _install_key()
    enc_key, mac_key = _subkeys(key)
    nonce = _secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
    return _ENC_PREFIX_V2 + base64.b64encode(nonce + cipher + tag).decode("ascii")


def _decrypt(token: str) -> Optional[str]:
    if not token:
        return None
    # Current authenticated format: verify the tag before decrypting.
    if token.startswith(_ENC_PREFIX_V2):
        try:
            raw = base64.b64decode(token[len(_ENC_PREFIX_V2):])
            if len(raw) < 16 + _TAG_LEN:
                return None
            nonce, cipher, tag = raw[:16], raw[16:-_TAG_LEN], raw[-_TAG_LEN:]
            enc_key, mac_key = _subkeys(_install_key(create=False))
            expected = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                return None  # tampered or wrong key
            data = bytes(a ^ b for a, b in
                         zip(cipher, _keystream(enc_key, nonce, len(cipher))))
            return data.decode("utf-8")
        except Exception:
            return None
    # Legacy unauthenticated format (no integrity check available).
    if token.startswith(_ENC_PREFIX):
        try:
            raw = base64.b64decode(token[len(_ENC_PREFIX):])
            nonce, cipher = raw[:16], raw[16:]
            if len(raw) < 16:
                return None
            key = _install_key(create=False)
            data = bytes(a ^ b for a, b in
                         zip(cipher, _keystream(key, nonce, len(cipher))))
            return data.decode("utf-8")
        except Exception:
            return None
    return None


# ------------------------------------------------------------ public API
def save_zwift_credentials(user_id: int, email: str, password: str) -> str:
    """Store credentials; returns the backend name used."""
    email = (email or "").strip()
    if not email or not password:
        raise ValueError("both email and password are required")
    _old_email, old_enc = db.get_zwift_credentials_row(user_id)
    kr = _keyring()
    if kr is not None:
        marker, target = _new_keyring_slot(user_id)
        try:
            # Always stage in a fresh slot. The currently referenced vault
            # target is never overwritten before verification and DB commit.
            kr.set_password(_SERVICE, target, password)
            written = kr.get_password(_SERVICE, target)
            if not written or not hmac.compare_digest(written, password):
                raise CredentialStorageError(
                    "the system credential vault did not verify the saved password"
                )
            db.set_zwift_credentials_row(user_id, email, marker)
        except Exception:
            # A failed set can still have made a partial write. Remove only the
            # fresh staged slot; the formerly referenced target is untouched.
            try:
                kr.delete_password(_SERVICE, target)
            except Exception:
                pass
        else:
            old_target = _keyring_target(user_id, old_enc)
            if old_target and old_target != target:
                try:
                    # Commit already points at the verified new slot. Cleanup
                    # of the unreferenced old slot is intentionally best-effort.
                    kr.delete_password(_SERVICE, old_target)
                except Exception:
                    pass
            return "system keychain"

    if _is_windows():
        try:
            marker = windows_secrets.protect_password(password, _SERVICE, user_id)
        except windows_secrets.DPAPIError as exc:
            raise CredentialStorageError(
                "Windows Credential Manager and DPAPI are unavailable; "
                "Zwift credentials were not saved"
            ) from exc
        try:
            db.set_zwift_credentials_row(user_id, email, marker)
        except Exception:
            # DPAPI has no external record to orphan; the previous DB row is
            # still authoritative because set_zwift_credentials_row commits
            # atomically or raises.
            raise
        return "Windows DPAPI"

    # Preserve the historical non-Windows fallback and its on-disk format.
    try:
        marker = _encrypt(password)
    except Exception as exc:
        raise CredentialStorageError("local credential encryption failed") from exc
    try:
        db.set_zwift_credentials_row(user_id, email, marker)
    except Exception:
        # The existing row remains authoritative; the generated ciphertext has
        # no external state and is discarded.
        raise
    return "encrypted local file key"


def get_zwift_credentials(user_id: int) -> Optional[ZwiftCredentials]:
    email, enc = db.get_zwift_credentials_row(user_id)
    if not email or not enc:
        return None
    keyring_target = _keyring_target(user_id, enc)
    if keyring_target:
        kr = _keyring()
        if kr is None:
            return None
        try:
            password = kr.get_password(_SERVICE, keyring_target)
        except Exception:
            return None
        return ZwiftCredentials(email, password) if password else None
    if enc.startswith(_DPAPI_PREFIX):
        try:
            password = windows_secrets.unprotect_password(enc, _SERVICE, user_id)
        except windows_secrets.DPAPIError:
            return None
        return ZwiftCredentials(email, password) if password else None
    password = _decrypt(enc)
    return ZwiftCredentials(email, password) if password else None


def credentials_saved(user_id: int) -> bool:
    email, enc = db.get_zwift_credentials_row(user_id)
    return bool(email and enc)


def clear_zwift_credentials(user_id: int) -> None:
    _email, enc = db.get_zwift_credentials_row(user_id)
    keyring_target = _keyring_target(user_id, enc)
    if keyring_target:
        kr = _keyring()
        if kr is not None:
            try:
                kr.delete_password(_SERVICE, keyring_target)
            except Exception:
                pass
    # Clearing is an explicit destructive request.  Preserve historical
    # best-effort semantics: attempt vault deletion first, then remove the DB
    # reference even if the platform vault is currently unavailable.
    db.set_zwift_credentials_row(user_id, None, None)
