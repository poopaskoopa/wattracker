import builtins
import sys
import types

import pytest

from wattracker.cloud.client import SyncCredentials
from wattracker.cloud.credentials import (
    CloudCredentialStore,
    CloudCredentialUnavailable,
)
from wattracker.cloud.desktop_sync import DesktopCloudSync


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, account):
        return self.values.get(account)

    def set(self, account, value):
        self.values[account] = value

    def delete(self, account):
        self.values.pop(account, None)


@pytest.mark.parametrize("operation", ["get", "set"])
def test_default_cloud_backend_respects_disabled_keyring_without_importing_or_calling_it(
    monkeypatch, operation,
):
    calls = []
    failing_keyring = types.ModuleType("keyring")

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("disabled keyring was called")

    failing_keyring.get_password = fail
    failing_keyring.set_password = fail
    failing_keyring.delete_password = fail
    monkeypatch.setitem(sys.modules, "keyring", failing_keyring)

    real_import = builtins.__import__

    def reject_keyring_import(name, *args, **kwargs):
        if name == "keyring":
            raise AssertionError("disabled keyring was imported")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_keyring_import)

    sync = DesktopCloudSync()
    with pytest.raises(CloudCredentialUnavailable, match="secure storage"):
        if operation == "get":
            sync.credential_store.backend.get("account")
        else:
            sync.credential_store.backend.set("account", "value")

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
