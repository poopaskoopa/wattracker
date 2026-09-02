"""The iOS client's transport and storage posture, asserted from the tree.

Three of #159's requirements are not properties of any one function, so no
Swift unit test can hold them:

* certificate validation is always on, with no exception that can ship;
* nothing sensitive is written to ``UserDefaults``;
* nothing sensitive is logged, in any configuration.

Each is a property of the whole target -- one added ``NSExceptionDomains``
entry, one ``@AppStorage`` on a token, one ``print`` in a catch block undoes
it, in a file nobody thought to look at.  These read the sources and the build
configuration, which is where those mistakes would be visible.

They are Python because the facts live in a plist and two xcconfigs, and
because the Swift suite cannot see its own build settings.
"""
from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

IOS = Path(__file__).resolve().parents[1] / "ios" / "WatTracker"
INFO_PLIST = IOS / "WatTracker" / "Info.plist"
CONFIG = IOS / "Config"
SOURCES = sorted((IOS / "WatTracker").rglob("*.swift"))
CLOUD_SOURCES = sorted((IOS / "WatTracker" / "Cloud").rglob("*.swift"))
ALL_SWIFT = sorted(IOS.rglob("*.swift"))

# The production host. An ATS exception naming it, at any nesting, is the
# failure this file exists to catch.
PRODUCTION_HOST = "api.wattracker.com"


@pytest.fixture(scope="module")
def info() -> dict:
    # The comments are stripped first because this plist's prose contains
    # ``--``, which Xcode accepts inside an XML comment and a conforming XML
    # parser does not. The comments are the most valuable thing in that file
    # and are not being reformatted to suit a test; the test reads the data.
    return plistlib.loads(re.sub(rb"<!--.*?-->", b"", INFO_PLIST.read_bytes(), flags=re.S))


def _uncommented(text: str) -> str:
    """Source with its comments blanked, line numbering intact.

    Whole-line comments only. A trailing ``//`` is left alone so that stripping
    one can never swallow the code before it -- and so that a rule about what
    the code may contain is never satisfied by deleting part of a line.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(
        "" if line.lstrip().startswith("//") else line for line in text.splitlines()
    )


def _read(paths) -> dict[Path, str]:
    return {path: _uncommented(path.read_text(encoding="utf-8")) for path in paths}


def _config(name: str) -> str:
    return _uncommented((CONFIG / name).read_text(encoding="utf-8"))


# --------------------------------------------------------- transport security
def test_app_transport_security_has_no_global_relaxation(info):
    ats = info.get("NSAppTransportSecurity", {})
    for key in (
        "NSAllowsArbitraryLoads",
        "NSAllowsArbitraryLoadsInWebContent",
        "NSAllowsArbitraryLoadsForMedia",
    ):
        assert not ats.get(key), f"{key} disables certificate validation app-wide"


def test_the_only_ats_exception_is_loopback(info):
    ats = info.get("NSAppTransportSecurity", {})
    domains = ats.get("NSExceptionDomains", {})
    # Local networking is the developer affordance and cannot reach the public
    # internet; a named exception domain can, so the list is pinned exactly.
    assert set(domains) == {"localhost"}, (
        "an exception domain other than localhost can reach a real host"
    )
    entry = domains["localhost"]
    assert entry.get("NSExceptionAllowsInsecureHTTPLoads") is True
    assert not entry.get("NSIncludesSubdomains"), (
        "subdomains of localhost are not the developer's own machine"
    )
    assert PRODUCTION_HOST not in repr(ats)


def test_the_production_scheme_is_https():
    base = _config("Base.xcconfig")
    assert re.search(r"^WATTRACKER_API_SCHEME = https$", base, re.M)
    assert re.search(rf"^WATTRACKER_API_HOST = {re.escape(PRODUCTION_HOST)}$", base, re.M)
    release = _config("Release.xcconfig")
    assert "WATTRACKER_API_SCHEME" not in release, (
        "a release build must not override the production scheme"
    )


def test_no_source_can_accept_a_certificate_the_system_rejected():
    """The one delegate hook that can, and the trust object it would use.

    A `URLSession` built without a delegate cannot be asked to override an
    evaluation. Adding the delegate method anywhere is what would change that,
    so the method name itself is the thing to refuse.
    """
    banned = (
        "didReceive challenge",
        "URLAuthenticationChallenge",
        "serverTrust",
        "SecTrustEvaluate",
        "NSURLSessionDelegate",
        "URLSessionDelegate",
    )
    for path, text in _read(ALL_SWIFT).items():
        for needle in banned:
            assert needle not in text, f"{path.name} reaches into TLS evaluation"


def test_the_software_key_fallback_is_only_in_the_debug_configuration():
    flag = "WATTRACKER_SOFTWARE_KEYS_ALLOWED"
    debug = _config("Debug.xcconfig")
    release = _config("Release.xcconfig")
    base = _config("Base.xcconfig")
    assert flag in debug
    assert flag not in base
    assert flag not in release
    assert re.search(r"^SWIFT_ACTIVE_COMPILATION_CONDITIONS =\s*$", release, re.M), (
        "a release build must define no compilation conditions at all"
    )


# ------------------------------------------------------------------- storage
def test_nothing_in_the_app_uses_user_defaults():
    """Not "no secrets in defaults" -- nothing at all.

    A defaults plist has no protection class and is copied into every backup.
    The moment one exists, the next value somebody adds to it is a judgement
    call; keeping the file from existing is not.
    """
    for path, text in _read(ALL_SWIFT).items():
        for needle in ("UserDefaults", "NSUserDefaults", "@AppStorage", "@SceneStorage"):
            assert needle not in text, f"{path.name} writes to defaults"


def test_the_only_durable_secrets_are_keychain_items():
    """The signing key and the device credential, and nothing else.

    A reader context is a five-minute bearer token and is deliberately held in
    memory only: `PairedDevice`, the one shape that reaches storage, has no
    field it could arrive in.
    """
    credential_store = _uncommented(
        (IOS / "WatTracker" / "Cloud" / "DeviceCredentialStore.swift").read_text(
            encoding="utf-8"
        )
    )
    assert "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly" in credential_store
    assert "readerContext" not in credential_store.split("struct PairedDevice")[1].split("}")[0]

    key_store = _uncommented(
        (IOS / "WatTracker" / "Cloud" / "DeviceKey.swift").read_text(encoding="utf-8")
    )
    assert "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly" in key_store

    cache = _uncommented(
        (IOS / "WatTracker" / "Cloud" / "SnapshotCache.swift").read_text(encoding="utf-8")
    )
    assert "completeFileProtectionUntilFirstUserAuthentication" in cache
    assert "isExcludedFromBackup" in cache


# ------------------------------------------------------------------- logging
LOGGING_CALLS = re.compile(
    r"(?<![A-Za-z0-9_.])(print|debugPrint|dump|NSLog|os_log)\s*\(|"
    r"(?<![A-Za-z0-9_.])(Logger|OSLog)\s*\("
)

# Names whose value must never be interpolated into anything, anywhere.
SENSITIVE = (
    "token", "readerContext", "reader_context", "credentialID", "credential_id",
    "subscriptionKey", "subscription_key", "signature", "privateKey", "bearer",
    "Authorization", "publicKeyX963",
)


def test_the_cloud_layer_logs_nothing_at_all():
    """Not "logs nothing sensitive": logs nothing.

    Every value this layer handles is either a secret, an identifier for one,
    or the rider's training data. There is no line of it worth a log entry, and
    `#if DEBUG` around one would only mean the mistake ships to whoever builds
    Debug on a device.
    """
    for path, text in _read(CLOUD_SOURCES).items():
        match = LOGGING_CALLS.search(text)
        assert match is None, f"{path.name} logs: {match.group(0)!r}"


def test_no_log_line_anywhere_can_carry_a_secret():
    for path, text in _read(ALL_SWIFT).items():
        for number, line in enumerate(text.splitlines(), start=1):
            if not LOGGING_CALLS.search(line):
                continue
            for name in SENSITIVE:
                assert name not in line, f"{path.name}:{number} logs {name}"


def test_a_decode_failure_never_quotes_the_body_it_choked_on():
    """`DecodingError`'s description quotes the offending JSON.

    On this API that body is the rider's training data, and on the refresh
    route it is a bearer token. The client's own message names the route
    instead, and this pins that: re-raising the underlying error would put a
    token into whatever the caller does with it.
    """
    client = _uncommented(
        (IOS / "WatTracker" / "Cloud" / "CloudClient.swift").read_text(encoding="utf-8")
    )
    body = client.split("private func sendReturningResponse")[1]
    assert "returned an unreadable body" in body
    assert "\\(error)" not in body, "the decoding error would quote the response body"
