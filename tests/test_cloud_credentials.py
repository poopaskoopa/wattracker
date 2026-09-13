import pytest

from wattracker.cloud.client import SyncCredentials
from wattracker.cloud.credentials import CloudCredentialStore


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, account):
        return self.values.get(account)

    def set(self, account, value):
        self.values[account] = value

    def delete(self, account):
        self.values.pop(account, None)


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
