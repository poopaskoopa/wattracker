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
    first = store.load_or_create_installation()
    assert first == store.load_or_create_installation()
    creds = SyncCredentials("c" * 64, "subscription", b"private-key", "n" * 64)
    store.save_writer(creds)
    loaded = store.load_writer()
    assert loaded == creds
    assert b"private-key" not in backend.values["writer-credentials"].encode()
    store.revoke_local_writer()
    assert store.load_writer() is None
