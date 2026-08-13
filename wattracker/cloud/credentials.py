"""OS-secure storage for opt-in cloud credentials.

There is no plaintext-file fallback. If the platform keychain is unavailable,
cloud synchronization remains offline while the local app continues normally.
Tests can inject a memory backend without touching a developer keychain.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .client import SyncCredentials
from .security import INSTALLATION_ID_BYTES, new_installation_id

SERVICE = "wattracker.cloud"
_INSTALLATION_ACCOUNT = "installation-id"
_WRITER_ACCOUNT = "writer-credentials"


class CloudCredentialUnavailable(RuntimeError):
    """Secure credential storage is unavailable; cloud stays disabled."""


class SecretBackend(Protocol):
    def get(self, account: str) -> Optional[str]: ...
    def set(self, account: str, value: str) -> None: ...
    def delete(self, account: str) -> None: ...


class KeyringBackend:
    """Adapter around the OS keychain through the optional keyring package."""

    def __init__(self, service: str = SERVICE) -> None:
        self.service = service
        try:
            import keyring
            self._keyring = keyring
        except ImportError as exc:
            raise CloudCredentialUnavailable("OS secure storage is unavailable") from exc

    def get(self, account: str) -> Optional[str]:
        try:
            return self._keyring.get_password(self.service, account)
        except Exception as exc:
            raise CloudCredentialUnavailable("OS secure storage is unavailable") from exc

    def set(self, account: str, value: str) -> None:
        try:
            self._keyring.set_password(self.service, account, value)
        except Exception as exc:
            raise CloudCredentialUnavailable("OS secure storage is unavailable") from exc

    def delete(self, account: str) -> None:
        try:
            self._keyring.delete_password(self.service, account)
        except Exception as exc:
            # Missing entries are harmless; backend failures are not.
            if exc.__class__.__name__ != "PasswordDeleteError":
                raise CloudCredentialUnavailable("OS secure storage is unavailable") from exc


@dataclass(frozen=True)
class StoredCloudIdentity:
    installation_id: str
    writer: Optional[SyncCredentials] = field(default=None, repr=False)


class CloudCredentialStore:
    """Persist only opaque cloud material in a caller-selected secure backend."""

    def __init__(self, backend: SecretBackend) -> None:
        self.backend = backend

    def load_or_create_installation(self) -> str:
        value = self.backend.get(_INSTALLATION_ACCOUNT)
        if value:
            try:
                if len(bytes.fromhex(value)) == INSTALLATION_ID_BYTES:
                    return value
            except (TypeError, ValueError):
                pass
        installation_id = new_installation_id()
        self.backend.set(_INSTALLATION_ACCOUNT, installation_id)
        return installation_id

    def save_writer(self, credentials: SyncCredentials) -> None:
        payload = {
            "credential_id": credentials.credential_id,
            "subscription_key": credentials.subscription_key,
            "signing_key": base64.b64encode(credentials.signing_key).decode("ascii"),
            "namespace": credentials.namespace,
            "signature_algorithm": credentials.signature_algorithm,
        }
        self.backend.set(
            _WRITER_ACCOUNT,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )

    def load_writer(self) -> Optional[SyncCredentials]:
        raw = self.backend.get(_WRITER_ACCOUNT)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            return SyncCredentials(
                credential_id=payload["credential_id"],
                subscription_key=payload["subscription_key"],
                signing_key=base64.b64decode(payload["signing_key"], validate=True),
                namespace=payload["namespace"],
                signature_algorithm=payload.get("signature_algorithm", "hmac-sha256"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CloudCredentialUnavailable("stored cloud credential is invalid") from exc

    def revoke_local_writer(self) -> None:
        self.backend.delete(_WRITER_ACCOUNT)
