"""Static and fake-runner tests for the iOS release signing path.

These tests never invoke ``security``, ``gh``, Keychain, or an Apple
credential.  The helper is imported as a module and all child processes are
replaced by a runner that records argv and stdin.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import ios_release_secrets as helper


WORKFLOW = Path(".github/workflows/ios-release.yml")


class FakeRunner:
    def __init__(self, *, secret_values: tuple[str, ...] = ()):
        self.secret_values = secret_values
        self.calls: list[tuple[tuple[str, ...], bytes]] = []
        self.export_paths: list[Path] = []
        self.deleted_identities: list[str] = []
        self.deleted_keychains: list[str] = []
        self.temp_keychain: Path | None = None
        self.child_output = bytearray()

    def __call__(self, argv, *, input_data=b""):
        argv = tuple(argv)
        # `security export -P` is the documented CLI interface and is the one
        # unavoidable short-lived argv exposure.  gh secret set must never
        # receive a value on argv; its value is supplied via stdin below.
        if argv[0] == helper.GH_PATH:
            for secret in self.secret_values:
                assert secret not in argv
        self.calls.append((argv, input_data))
        if argv[:3] == ("security", "find-identity", "-v"):
            if argv[-1] == helper.LOGIN_KEYCHAIN:
                stdout = (
                    b'  1) AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA '
                    b'"Apple Distribution: First Rider (TEAMONE)"\n'
                    b'  2) BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB '
                    b'"Apple Distribution: Second Rider (TEAMTWO)"\n'
                    b'     2 valid identities found\n'
                )
            else:
                stdout = (
                    b'  1) AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA '
                    b'"Apple Distribution: First Rider (TEAMONE)"\n'
                    b'  2) BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB '
                    b'"Apple Distribution: Second Rider (TEAMTWO)"\n'
                    b'  3) CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC '
                    b'"Developer ID Application: Other Rider (TEAMTHREE)"\n'
                    b'     3 valid identities found\n'
                )
            self.child_output.extend(stdout)
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
        if argv[:2] == ("security", "export"):
            output_path = Path(argv[argv.index("-o") + 1])
            output_path.write_bytes(b"fake-p12-bytes")
            self.export_paths.append(output_path)
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if argv[:2] == ("security", "create-keychain"):
            self.temp_keychain = Path(argv[-1])
            self.temp_keychain.write_bytes(b"fake-keychain")
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if argv[:2] == ("security", "unlock-keychain"):
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if argv[:2] == ("security", "import"):
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if argv[:2] == ("security", "delete-identity"):
            self.deleted_identities.append(argv[argv.index("-Z") + 1])
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if argv[:2] == ("security", "delete-keychain"):
            self.deleted_keychains.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if argv[0] == helper.GH_PATH:
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected fake command: {argv!r}")


def test_workflow_validates_and_passes_the_secret_backed_host_separately():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "- name: Validate the iOS API host" in workflow
    assert "WATTRACKER_IOS_API_HOST: ${{ secrets.WATTRACKER_IOS_API_HOST }}" in workflow
    assert "WATTRACKER_API_SCHEME: https" in workflow
    assert "WATTRACKER_API_HOST: ${{ secrets.WATTRACKER_IOS_API_HOST }}" in workflow
    assert 'WATTRACKER_API_SCHEME="$WATTRACKER_API_SCHEME"' in workflow
    assert 'WATTRACKER_API_HOST="$WATTRACKER_API_HOST"' in workflow
    assert workflow.index("- name: Validate the iOS API host") < workflow.index(
        "- name: Archive for the App Store"
    )
    assert "api.wattracker.com" in workflow
    assert "contains whitespace" in workflow
    assert "must be a hostname" in workflow


def test_workflow_uses_manual_diagnostic_version_and_upload_is_tag_only():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'version="0.0.0"' in workflow
    assert "Manual dispatch archives and exports only" in workflow
    assert "if: startsWith(github.ref, 'refs/tags/ios-v')" in workflow


def test_workflow_classifies_certificate_failures_and_keeps_cleanup_unconditional():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for message in (
        "missing or empty",
        "not valid base64",
        "could not be opened; check its password",
        "contains no Apple Distribution identity",
        "certificate is expired",
        "team does not match APPLE_TEAM_ID",
    ):
        assert message in workflow
    assert "openssl pkcs12" in workflow
    assert "-passin env:IOS_DIST_P12_PASSWORD" in workflow
    assert "openssl x509" in workflow
    cleanup_start = workflow.index("- name: Remove the signing material")
    cleanup_end = workflow.find("\n      - name:", cleanup_start + 1)
    cleanup = workflow[cleanup_start:] if cleanup_end == -1 else workflow[cleanup_start:cleanup_end]
    assert "if: always()" in cleanup
    assert "security delete-keychain" in workflow
    assert "wattracker-ios-distribution-cert.pem" in workflow
    keychain_record = workflow.index('echo "IOS_SIGNING_KEYCHAIN=$keychain" >> "$GITHUB_ENV"')
    risky_validation = workflow.index("base64 --decode")
    assert keychain_record < risky_validation


def test_helper_uses_security_export_password_argv_but_never_gh_secret_argv():
    script = Path("scripts/ios_release_secrets.py").read_text(encoding="utf-8")
    assert '"-P",' in script
    assert '"secret", "set"' in script
    assert "input_data=value.encode" in script
    assert "input_data=(password +" not in script


def test_helper_requires_selection_and_never_puts_password_in_argv_or_output(
    tmp_path, monkeypatch, capsys
):
    password = "password-sentinel-that-must-never-leak"
    host = "cloud.example.test"
    p12_b64 = "ZmFrZS1wMTItYnl0ZXM="
    runner = FakeRunner(secret_values=(p12_b64, password, host))
    generated = iter((password, "source-password", "temporary-keychain-password"))
    monkeypatch.setattr(helper.secrets, "token_urlsafe", lambda length: next(generated))
    host_config = tmp_path / "Production.local.xcconfig"
    host_config.write_text(f"WATTRACKER_API_HOST = {host};\n", encoding="utf-8")

    helper.install_secrets(
        runner=runner,
        input_fn=lambda prompt: "2",
        host_config=host_config,
    )

    export_argv, export_input = next(
        (argv, data) for argv, data in runner.calls if argv[:2] == ("security", "export")
    )
    assert export_argv[-2] == "-o"
    assert export_argv[export_argv.index("-o") + 2 :] == ()
    assert export_input == b""
    assert runner.temp_keychain is not None
    assert runner.deleted_identities == [
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
    ]
    assert runner.deleted_keychains == [str(runner.temp_keychain)]
    assert [argv[1] for argv, _ in runner.calls if argv[0] == "security"] == [
        "find-identity",
        "export",
        "create-keychain",
        "unlock-keychain",
        "import",
        "find-identity",
        "delete-identity",
        "delete-identity",
        "export",
        "delete-keychain",
    ]
    export_calls = [
        (argv, data) for argv, data in runner.calls if argv[:2] == ("security", "export")
    ]
    assert len(export_calls) == 2
    assert all(argv[-2] == "-o" and argv[argv.index("-o") + 2 :] == () for argv, _ in export_calls)
    assert export_calls[1][0][export_calls[1][0].index("-P") + 1] == password
    gh_calls = [(argv, data) for argv, data in runner.calls if argv[0] == helper.GH_PATH]
    assert gh_calls
    assert [data for _, data in gh_calls] == [
        p12_b64.encode(),
        password.encode(),
        host.encode(),
    ]
    assert all(not any(secret in argv for secret in runner.secret_values) for argv, _ in gh_calls)
    captured = capsys.readouterr()
    assert all(secret not in captured.out for secret in runner.secret_values)
    assert all(secret not in captured.err for secret in runner.secret_values)
    assert all(secret.encode() not in runner.child_output for secret in runner.secret_values)
    assert any(argv[0] == helper.GH_PATH for argv, _ in runner.calls)
    assert all(not path.exists() for path in runner.export_paths)

    host_inputs = [
        data.decode()
        for argv, data in runner.calls
        if argv[0] == helper.GH_PATH and argv[3] == "WATTRACKER_IOS_API_HOST"
    ]
    assert host_inputs == [host]


def test_helper_dry_run_does_not_export_or_set_secrets(tmp_path, capsys):
    runner = FakeRunner()
    host_config = tmp_path / "Production.local.xcconfig"
    host_config.write_text("WATTRACKER_API_HOST = cloud.example.test;\n", encoding="utf-8")

    helper.install_secrets(
        runner=runner,
        input_fn=lambda prompt: "1",
        dry_run=True,
        host_config=host_config,
    )

    assert not any(argv[:2] == ("security", "export") for argv, _ in runner.calls)
    assert not any(argv[0] == helper.GH_PATH for argv, _ in runner.calls)
    assert "no p12 was exported" in capsys.readouterr().out


@pytest.mark.parametrize(
    "host",
    [
        "",
        " cloud.example.test",
        "cloud.example.test ",
        "https://cloud.example.test",
        "cloud.example.test/path",
        "cloud example.test",
        "cloud.example.test:443",
        "api.wattracker.com",
        "-cloud.example.test",
        "cloud..example.test",
    ],
)
def test_helper_rejects_unsafe_hosts(host):
    with pytest.raises(helper.SecretSetupError):
        helper.validate_host(host)


def test_helper_accepts_a_realistic_host_without_printing_it():
    assert helper.validate_host("api.prod.example.test") == "api.prod.example.test"
