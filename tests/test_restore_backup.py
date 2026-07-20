"""Tests for the offline restore CLI (wattracker.restore_backup)."""
import os
import sqlite3

from wattracker import backup, db, restore_backup
from wattracker.config import db_path


def _server_down():
    return (False, "")


def _server_up():
    return (True, "test says a server is running")


def _usernames(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT username FROM users")}
    finally:
        conn.close()


def test_restore_round_trip_reverts_data(capsys):
    db.init_db()
    db.create_user("original", "hash")
    # Snapshot the state that has only "original".
    backup.create_backup("manual")
    # Now mutate the live DB.
    db.create_user("added_later", "hash")
    assert _usernames(db_path()) == {"original", "added_later"}

    rc = restore_backup.main(["--restore", "1"], server_check=_server_down)
    assert rc == 0
    # Data actually reverted: the later user is gone.
    assert _usernames(db_path()) == {"original"}


def test_restore_refuses_when_server_running(capsys):
    db.init_db()
    db.create_user("original", "hash")
    backup.create_backup("manual")
    db.create_user("added_later", "hash")

    rc = restore_backup.main(["--restore", "1"], server_check=_server_up)
    assert rc == 1
    # DB untouched because the restore was refused.
    assert _usernames(db_path()) == {"original", "added_later"}
    assert "Refusing to restore" in capsys.readouterr().err


def test_restore_creates_pre_restore_backup():
    db.init_db()
    db.create_user("original", "hash")
    backup.create_backup("manual")
    restore_backup.main(["--restore", "1"], server_check=_server_down)
    pre = [b for b in backup.list_backups() if b["reason"] == "pre-restore"]
    assert len(pre) == 1


def test_restore_removes_stale_wal_shm():
    db.init_db()
    db.create_user("original", "hash")
    backup.create_backup("manual")
    live = db_path()
    # Simulate leftover WAL sidecars from the running DB.
    for suffix in ("-wal", "-shm"):
        with open(live + suffix, "wb") as f:
            f.write(b"stale")
    restore_backup.main(["--restore", "1"], server_check=_server_down)
    assert not os.path.exists(live + "-wal")
    assert not os.path.exists(live + "-shm")


def test_no_args_lists_backups_and_usage(capsys):
    db.init_db()
    db.create_user("original", "hash")
    backup.create_backup("manual")
    rc = restore_backup.main([], server_check=_server_down)
    assert rc == 0
    out = capsys.readouterr()
    assert "Available backups" in out.out
    assert "[1]" in out.out
    assert "Usage" in out.err


def test_out_of_range_index_errors(capsys):
    db.init_db()
    db.create_user("original", "hash")
    backup.create_backup("manual")
    rc = restore_backup.main(["--restore", "99"], server_check=_server_down)
    assert rc == 2
    assert "out of range" in capsys.readouterr().err


def test_non_numeric_index_errors(capsys):
    db.init_db()
    rc = restore_backup.main(["--restore", "abc"], server_check=_server_down)
    assert rc == 2
    assert "not a number" in capsys.readouterr().err


def test_server_probe_uses_configured_loopback_port(monkeypatch):
    seen = {}
    monkeypatch.setenv("WATTRACKER_HOST", "::1")
    monkeypatch.setenv("WATTRACKER_PORT", "9123")
    monkeypatch.setattr(restore_backup.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "", "returncode": 1})())
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
    def connect(address, timeout):
        seen.update(address=address, timeout=timeout)
        raise OSError
    monkeypatch.setattr(restore_backup.socket, "create_connection", connect)
    assert restore_backup._server_running() == (False, "")
    assert seen["address"] == ("::1", 9123)
