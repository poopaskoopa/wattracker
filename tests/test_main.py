import pytest

from wattracker import config
import wattracker.__main__ as launcher


@pytest.mark.parametrize("value, expected", [("127.0.0.1", "127.0.0.1"), ("localhost", "localhost"), ("::1", "::1"), ("[::1]", "::1")])
def test_loopback_hosts(monkeypatch, value, expected):
    monkeypatch.setenv("WATTRACKER_HOST", value)
    assert config.server_host() == expected


@pytest.mark.parametrize("value", ["", "0.0.0.0", "127.0.0.2", "192.168.1.4", "example.com"])
def test_non_loopback_hosts_rejected(monkeypatch, value):
    monkeypatch.setenv("WATTRACKER_HOST", value)
    with pytest.raises(ValueError):
        config.server_host()


@pytest.mark.parametrize("value", ["0", "65536", "abc", ""])
def test_invalid_ports_rejected(monkeypatch, value):
    monkeypatch.setenv("WATTRACKER_PORT", value)
    with pytest.raises(ValueError):
        config.server_port()


def test_ipv6_browser_url():
    assert config.browser_url("::1", 8123) == "http://[::1]:8123"


def test_main_resolves_runtime_config_without_browser(monkeypatch):
    calls = {}
    monkeypatch.setenv("WATTRACKER_HOST", "localhost")
    monkeypatch.setenv("WATTRACKER_PORT", "8123")
    monkeypatch.setenv("WATTRACKER_OPEN_BROWSER", "0")
    monkeypatch.setattr("wattracker.db.init_db", lambda: calls.setdefault("db", True))
    monkeypatch.setattr(launcher.threading, "Thread", lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser thread created")))
    monkeypatch.setattr(launcher.uvicorn, "run", lambda *args, **kwargs: calls.update(args=args, kwargs=kwargs))
    launcher.main()
    assert calls["db"] is True
    assert calls["args"] == ("wattracker.server:app",)
    assert calls["kwargs"] == {"host": "localhost", "port": 8123, "reload": False}


def test_main_browser_thread_gets_correct_url(monkeypatch):
    captured = {}
    class Thread:
        def __init__(self, target, args, daemon):
            captured.update(target=target, args=args, daemon=daemon)
        def start(self):
            captured["started"] = True
    monkeypatch.setenv("WATTRACKER_HOST", "[::1]")
    monkeypatch.setenv("WATTRACKER_PORT", "8124")
    monkeypatch.setenv("WATTRACKER_OPEN_BROWSER", "yes")
    monkeypatch.setattr("wattracker.db.init_db", lambda: None)
    monkeypatch.setattr(launcher.threading, "Thread", Thread)
    monkeypatch.setattr(launcher.uvicorn, "run", lambda *a, **k: None)
    launcher.main()
    assert captured["args"] == ("http://[::1]:8124",)
    assert captured["daemon"] is True and captured["started"] is True
