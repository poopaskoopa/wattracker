import builtins
import sys
import types

import pytest

from wattracker.cloud.client import SyncCredentials
from wattracker.cloud.credentials import (
    CloudCredentialStore,
    CloudCredentialUnavailable,
    KeyringBackend,
)
from wattracker.cloud.desktop_sync import DesktopCloudSync
from wattracker import credstore


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, account):
        return self.values.get(account)

    def set(self, account, value):
        self.values[account] = value

    def delete(self, account):
        self.values.pop(account, None)


@pytest.mark.parametrize("disabled_value", ["0", "false", "no"])
@pytest.mark.parametrize("operation", ["get", "set"])
def test_default_cloud_backend_respects_disabled_keyring_without_importing_or_calling_it(
    monkeypatch, disabled_value, operation,
):
    monkeypatch.setenv("WATTRACKER_KEYRING", disabled_value)
    calls = []
    failing_keyring = types.ModuleType("keyring")
    fake_errors = types.ModuleType("keyring.errors")
    fake_errors.KeyringError = RuntimeError
    failing_keyring.errors = fake_errors
    safe_backend = type(
        "WinVaultKeyring", (), {"__module__": "keyring.backends.windows"}
    )()
    failing_keyring.get_keyring = lambda: safe_backend

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("disabled keyring was called")

    failing_keyring.get_password = fail
    failing_keyring.set_password = fail
    failing_keyring.delete_password = fail
    monkeypatch.setitem(sys.modules, "keyring", failing_keyring)
    monkeypatch.setitem(sys.modules, "keyring.errors", fake_errors)

    real_import = builtins.__import__
    imports = []

    def record_keyring_import(name, *args, **kwargs):
        if name == "keyring" or name.startswith("keyring."):
            imports.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", record_keyring_import)

    sync = DesktopCloudSync()
    with pytest.raises(CloudCredentialUnavailable, match="secure storage"):
        if operation == "get":
            sync.credential_store.backend.get("account")
        else:
            sync.credential_store.backend.set("account", "value")

    assert calls == []
    assert imports == []


def _install_fake_keyring(monkeypatch, backend):
    calls = []
    fake_keyring = types.ModuleType("keyring")
    fake_errors = types.ModuleType("keyring.errors")
    fake_errors.KeyringError = RuntimeError
    fake_keyring.errors = fake_errors
    fake_keyring.get_keyring = lambda: backend

    def set_password(*args):
        calls.append(args)

    fake_keyring.set_password = set_password
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    monkeypatch.setitem(sys.modules, "keyring.errors", fake_errors)
    monkeypatch.setenv("WATTRACKER_KEYRING", "1")
    return calls


@pytest.mark.parametrize(
    "backend_name, backend_module",
    [
        ("PlaintextKeyring", "keyrings.alt.file"),
        ("FailKeyring", "keyring.backends.fail"),
    ],
)
def test_keyring_backend_rejects_unsafe_backends_before_writing(
    monkeypatch, backend_name, backend_module,
):
    backend = type(backend_name, (), {"__module__": backend_module})()
    calls = _install_fake_keyring(monkeypatch, backend)

    with pytest.raises(CloudCredentialUnavailable, match="secure storage"):
        KeyringBackend()

    assert calls == []


def test_keyring_backend_rejects_non_winvault_backend_on_windows(monkeypatch):
    backend = type("MacKeyring", (), {"__module__": "keyring.backends.macOS"})()
    calls = _install_fake_keyring(monkeypatch, backend)
    monkeypatch.setattr(credstore, "_is_windows", lambda: True)

    with pytest.raises(CloudCredentialUnavailable, match="secure storage"):
        KeyringBackend()

    assert calls == []


def test_cloud_identity_and_writer_are_stored_only_in_secure_backend():
    backend = MemorySecrets()
    store = CloudCredentialStore(backend)
    store.probe()
    assert backend.values == {}
    first = store.load_or_create_installation()
    assert first == store.load_or_create_installation()
    creds = SyncCredentials("c" * 64, "subscription", b"private-key", "n" * 64)
    store.save_writer(creds)
    loaded = store.load_writer()
    assert loaded == creds
    assert b"private-key" not in backend.values["writer-credentials"].encode()
    store.revoke_local_writer()
    assert store.load_writer() is None


def test_failed_probe_delete_reuses_recoverable_account_across_stores():
    class FailingDeleteSecrets(MemorySecrets):
        fail_delete = True

        def delete(self, account):
            if self.fail_delete:
                raise CloudCredentialUnavailable("delete unavailable")
            super().delete(account)

    backend = FailingDeleteSecrets()
    # Recreating the store also exercises recovery without in-memory tracking.
    for _ in range(3):
        with pytest.raises(CloudCredentialUnavailable, match="delete unavailable"):
            CloudCredentialStore(backend).probe()
        assert set(backend.values) == {"credential-probe"}

    backend.fail_delete = False
    CloudCredentialStore(backend).probe()
    assert backend.values == {}


@pytest.mark.parametrize("failure", ["partial_write", "read", "mismatch"])
def test_probe_cleans_up_after_write_and_verification_failures(failure):
    class FailingSecrets(MemorySecrets):
        def set(self, account, value):
            super().set(account, value)
            if failure == "partial_write":
                raise CloudCredentialUnavailable("write failed after storing")

        def get(self, account):
            if failure == "read":
                raise CloudCredentialUnavailable("read unavailable")
            return "incorrect value"

    backend = FailingSecrets()
    with pytest.raises(CloudCredentialUnavailable):
        CloudCredentialStore(backend).probe()
    assert backend.values == {}


def test_scoped_writers_are_isolated_and_reject_invalid_user_ids():
    backend = MemorySecrets()
    store = CloudCredentialStore(backend)
    legacy = SyncCredentials("c" * 64, "legacy-subscription", b"legacy-key", "l" * 64)
    first = SyncCredentials("a" * 64, "subscription-a", b"private-a", "n" * 64)
    second = SyncCredentials("b" * 64, "subscription-b", b"private-b", "m" * 64)

    store.save_writer(legacy)
    store.save_writer(first, user_id=1)
    store.save_writer(second, user_id=2)

    assert store.load_writer(user_id=1) == first
    assert store.load_writer(user_id=2) == second
    assert store.load_writer() == legacy
    assert store.load_writer(user_id=3) is None
    assert set(backend.values) == {
        "writer-credentials", "writer-credentials:user:1",
        "writer-credentials:user:2",
    }

    store.revoke_local_writer(user_id=1)
    assert store.load_writer(user_id=1) is None
    assert store.load_writer(user_id=2) == second

    for invalid_user_id in (True, False, 0, -1, "1", 1.0):
        with pytest.raises(ValueError, match="user_id must be positive"):
            store.load_writer(user_id=invalid_user_id)
