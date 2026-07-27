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


# ------------------------------------------------- external (tailnet) host
# This value is appended to the Host allowlist, so every rejection below is a
# security assertion, not input hygiene: anything that is not one exact DNS
# name must fail closed rather than be sanitised into something else.
@pytest.mark.parametrize("value, expected", [
    ("laptop.tail1234.ts.net", "laptop.tail1234.ts.net"),
    ("laptop.tail1234.ts.net:8443", "laptop.tail1234.ts.net:8443"),
    # DNS is case-insensitive, the allowlist comparison is not.
    ("Laptop.Tail1234.TS.NET", "laptop.tail1234.ts.net"),
    ("LAPTOP.ts.net:443", "laptop.ts.net:443"),
    ("  laptop.ts.net  ", "laptop.ts.net"),
    ("wattracker", "wattracker"),
    ("100.64.0.5", "100.64.0.5"),
])
def test_public_host_accepted(monkeypatch, value, expected):
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOST", value)
    assert config.public_host() == expected


@pytest.mark.parametrize("value", ["", "   "])
def test_public_host_unset_is_none(monkeypatch, value):
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOST", value)
    assert config.public_host() is None


def test_public_host_absent_is_none(monkeypatch):
    monkeypatch.delenv("WATTRACKER_PUBLIC_HOST", raising=False)
    assert config.public_host() is None


@pytest.mark.parametrize("value", [
    "https://laptop.ts.net",           # scheme
    "http://laptop.ts.net",
    "laptop.ts.net/calendar.ics",      # path
    "laptop.ts.net/",
    "laptop.ts.net?a=b",               # query
    "laptop.ts.net#frag",              # fragment
    "*",                               # wildcard, in every form
    "*.ts.net",
    ".ts.net",
    "laptop.*.ts.net",
    "laptop .ts.net",                  # embedded space
    "laptop\t.ts.net",
    "laptop\n.ts.net",
    "laptop.ts.net\x7f",               # control character
    "laptop\x01.ts.net",
    "user@laptop.ts.net",              # userinfo
    "laptop.ts.net:0",                 # port range
    "laptop.ts.net:65536",
    "laptop.ts.net:-1",
    "laptop.ts.net:",
    "laptop.ts.net:https",
    "laptop.ts.net:8443:8443",
    "laptop.ts.net:8４43",              # non-ASCII digit
    ":8443",
    "-laptop.ts.net",                  # malformed label
    "laptop-.ts.net",
    "laptop..ts.net",
    "laptop.ts.net.",
    "lap_top.ts.net",
    "l" * 64 + ".ts.net",              # label over 63
    (("a" * 63 + ".") * 4) + "ts.net",  # name over 253
    "[::1]",                           # IPv6 literal is not a DNS name
    "[::1]:8443",
    "läptop.ts.net",                   # IDN must be given as punycode
])
def test_public_host_rejected(monkeypatch, value):
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOST", value)
    with pytest.raises(ValueError):
        config.public_host()


@pytest.mark.parametrize("value, expected", [
    ("https", "https"), ("http", "http"), ("HTTPS", "https"), (" http ", "http"),
])
def test_public_scheme_accepted(monkeypatch, value, expected):
    monkeypatch.setenv("WATTRACKER_PUBLIC_SCHEME", value)
    assert config.public_scheme() == expected


@pytest.mark.parametrize("value", ["", "   "])
def test_public_scheme_defaults_to_https(monkeypatch, value):
    monkeypatch.setenv("WATTRACKER_PUBLIC_SCHEME", value)
    assert config.public_scheme() == "https"


def test_public_scheme_absent_defaults_to_https(monkeypatch):
    monkeypatch.delenv("WATTRACKER_PUBLIC_SCHEME", raising=False)
    assert config.public_scheme() == "https"


@pytest.mark.parametrize("value", ["ftp", "file", "javascript", "wss", "https:"])
def test_public_scheme_rejected(monkeypatch, value):
    monkeypatch.setenv("WATTRACKER_PUBLIC_SCHEME", value)
    with pytest.raises(ValueError):
        config.public_scheme()


def test_public_host_does_not_affect_the_bind_host(monkeypatch):
    """Naming an external host must never move the socket off loopback."""
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOST", "laptop.ts.net")
    monkeypatch.delenv("WATTRACKER_HOST", raising=False)
    assert config.server_host() == "127.0.0.1"


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
    # proxy_headers must stay explicitly off: the app binds loopback, so
    # uvicorn's default trusted-proxy range (127.0.0.1) would cover every
    # caller and let any of them set request.client.host via X-Forwarded-For.
    assert calls["kwargs"] == {
        "host": "localhost", "port": 8123, "reload": False,
        "proxy_headers": False,
    }


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
