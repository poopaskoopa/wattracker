"""Static and fake-runner tests for the iOS release signing path.

These tests never invoke ``security``, ``gh``, Keychain, or an Apple
credential.  The helper is imported as a module and all child processes are
replaced by a runner that records argv and stdin.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from scripts import ios_release_secrets as helper


WORKFLOW = Path(".github/workflows/ios-release.yml")
IOS_TESTFLIGHT_DOCS = Path("docs/ios-testflight.md")
ROOT_CERT = Path(".github/certs/AppleIncRootCertificate.cer")
APPLE_ROOT_CA_SHA256 = "b0b1730ecbc7ff4505142c49f1295e6eda6bcaed7e2c68c5be91b5a11001f024"
WWDR_CERT = Path(".github/certs/AppleWWDRCAG3.cer")
WWDR_G3_SHA256 = "dcf21878c77f4198e4b4614f03d696d89c66c66008d4244e1b99161aac91601f"


def test_docs_require_a_session_created_self_hosted_runner():
    docs = IOS_TESTFLIGHT_DOCS.read_text(encoding="utf-8")
    assert "LaunchDaemon" in docs
    assert "SessionCreate=true" in docs


class FakeRunner:
    def __init__(
        self,
        *,
        secret_values: tuple[str, ...] = (),
        temporary_identities: tuple[tuple[str, str, str], ...] | None = None,
        keep_deleted_identities: bool = False,
        extra_private_keys: int = 0,
        fail_selected_export: bool = False,
    ):
        self.secret_values = secret_values
        self.calls: list[tuple[tuple[str, ...], bytes]] = []
        self.export_paths: list[Path] = []
        self.deleted_identities: list[str] = []
        self.deleted_keychains: list[str] = []
        self.temp_keychain: Path | None = None
        self.child_output = bytearray()
        self.keep_deleted_identities = keep_deleted_identities
        self.extra_private_keys = extra_private_keys
        self.fail_selected_export = fail_selected_export
        self.temporary_identities = list(
            temporary_identities
            or (
                (
                    "A" * 40,
                    "Apple Distribution: First Rider (TEAMONE)",
                    "",
                ),
                (
                    "B" * 40,
                    "Apple Distribution: Second Rider (TEAMTWO)",
                    "",
                ),
                (
                    "C" * 40,
                    "Developer ID Application: Other Rider (TEAMTHREE)",
                    "CSSMERR_TP_NOT_TRUSTED",
                ),
            )
        )
        self.remaining_identities = {
            fingerprint: (label, status)
            for fingerprint, label, status in self.temporary_identities
        }

    def __call__(self, argv, *, input_data=b""):
        argv = tuple(argv)
        # `security export -P` is the documented CLI interface and is the one
        # unavoidable short-lived argv exposure.  gh secret set must never
        # receive a value on argv; its value is supplied via stdin below.
        if argv[0] == helper.GH_PATH:
            for secret in self.secret_values:
                assert secret not in argv
        self.calls.append((argv, input_data))
        if argv[:2] == ("security", "find-identity"):
            if argv[-1] == helper.LOGIN_KEYCHAIN:
                stdout = (
                    b'  1) AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA '
                    b'"Apple Distribution: First Rider (TEAMONE)"\n'
                    b'  2) BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB '
                    b'"Apple Distribution: Second Rider (TEAMTWO)"\n'
                    b'     2 valid identities found\n'
                )
            else:
                lines = []
                records = list(self.remaining_identities.items())
                for _ in range(2):
                    for number, (fingerprint, (label, status)) in enumerate(records, start=1):
                        suffix = f" ({status})" if status else ""
                        lines.append(f'  {number}) {fingerprint} "{label}"{suffix}\n')
                    lines.append(f"     {len(records)} identities found\n")
                stdout = "".join(lines).encode()
            self.child_output.extend(stdout)
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
        if argv[:2] == ("security", "export"):
            output_path = Path(argv[argv.index("-o") + 1])
            if self.fail_selected_export and output_path.name == "distribution.p12":
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")
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
            fingerprint = argv[argv.index("-Z") + 1]
            self.deleted_identities.append(fingerprint)
            if not self.keep_deleted_identities:
                self.remaining_identities.pop(fingerprint, None)
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if argv[:2] == ("security", "find-key"):
            key_count = len(self.remaining_identities) + self.extra_private_keys
            stdout = "".join(
                f'keychain: "{self.temp_keychain or "temporary"}"\n'
                for _ in range(key_count)
            ).encode()
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
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
    upload_start = workflow.index("- name: Validate and upload to TestFlight")
    upload_end = workflow.find("\n      - name:", upload_start + 1)
    upload = workflow[upload_start:] if upload_end == -1 else workflow[upload_start:upload_end]
    assert "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/ios-v')" in upload
    assert "if: startsWith(github.ref, 'refs/tags/ios-v')" not in upload


def test_workflow_classifies_certificate_failures_and_keeps_cleanup_unconditional():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for message in (
        "missing or empty",
        "not valid base64",
        "could not be opened; check its password",
        "could not be imported",
        "does not contain exactly one Apple Distribution identity",
        "certificate is expired",
        "team does not match APPLE_TEAM_ID",
    ):
        assert message in workflow
    assert "/usr/bin/openssl pkcs12" in workflow
    assert "-passin env:IOS_DIST_P12_PASSWORD" in workflow
    assert "/usr/bin/openssl x509" in workflow
    cleanup_start = workflow.index("- name: Remove the signing material")
    cleanup_end = workflow.find("\n      - name:", cleanup_start + 1)
    cleanup = workflow[cleanup_start:] if cleanup_end == -1 else workflow[cleanup_start:cleanup_end]
    assert "if: always()" in cleanup
    assert "security delete-keychain" in workflow
    assert "wattracker-ios-distribution-cert.pem" in workflow
    keychain_record = workflow.index('echo "IOS_SIGNING_KEYCHAIN=$keychain" >> "$GITHUB_ENV"')
    risky_validation = workflow.index("base64 --decode")
    assert keychain_record < risky_validation
    assert 'echo "IOS_ROOT_TRUST_CERT=$root_cert" >> "$GITHUB_ENV"' not in workflow


def test_workflow_pins_and_imports_only_the_checked_in_wwdr_g3_before_validity_check():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert WWDR_CERT.is_file()
    assert hashlib.sha256(WWDR_CERT.read_bytes()).hexdigest() == WWDR_G3_SHA256
    assert f'wwdr_expected_sha256="{WWDR_G3_SHA256}"' in workflow
    assert "shasum -a 256 \"$wwdr_cert\"" in workflow
    assert "curl" not in workflow
    assert "wget" not in workflow

    # Keep the operator literal: changing != to == must make this test fail.
    fingerprint_comparison = 'if [ "$wwdr_actual_sha256" != "$wwdr_expected_sha256" ]; then'
    assert workflow.count(fingerprint_comparison) == 1
    assert 'if [ "$wwdr_actual_sha256" == "$wwdr_expected_sha256" ]; then' not in workflow
    fingerprint_check = workflow.index(fingerprint_comparison)
    intermediate_import = workflow.index('security import "$wwdr_cert"')
    validity_check = workflow.index('security find-identity -v -p codesigning "$keychain"')
    p12_import = workflow.index('security import "$p12"')
    assert fingerprint_check < intermediate_import < p12_import < validity_check


def test_workflow_pins_and_imports_the_checked_in_apple_root_before_validity_check():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert ROOT_CERT.is_file()
    assert hashlib.sha256(ROOT_CERT.read_bytes()).hexdigest() == APPLE_ROOT_CA_SHA256
    subject = subprocess.run(
        [
            "/usr/bin/openssl",
            "x509",
            "-inform",
            "der",
            "-in",
            str(ROOT_CERT),
            "-noout",
            "-subject",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout
    assert "CN=Apple Root CA" in subject
    assert f'root_expected_sha256="{APPLE_ROOT_CA_SHA256}"' in workflow
    assert "shasum -a 256 \"$root_cert\"" in workflow
    assert "curl" not in workflow
    assert "wget" not in workflow

    fingerprint_comparison = 'if [ "$root_actual_sha256" != "$root_expected_sha256" ]; then'
    assert workflow.count(fingerprint_comparison) == 1
    fingerprint_check = workflow.index(fingerprint_comparison)
    root_import = workflow.index(
        'security add-trusted-cert -r trustRoot -p codeSign'
    )
    assert 'security import "$root_cert"' not in workflow
    wwdr_import = workflow.index('security import "$wwdr_cert"')
    p12_import = workflow.index('security import "$p12"')
    search_list = workflow.index(
        'security list-keychains -d user -s'
    )
    validity_check = workflow.index('security find-identity -v -p codesigning "$keychain"')
    assert fingerprint_check < root_import < search_list < wwdr_import < p12_import < validity_check


def test_workflow_trusts_the_pinned_root_in_the_job_keychain():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    search_list_command = 'security list-keychains -d user -s'
    archive_start = workflow.index("- name: Archive for the App Store")
    import_start = workflow.index(
        "- name: Import the distribution certificate into a temporary keychain"
    )
    signing_setup = workflow[import_start:archive_start]

    assert signing_setup.count(search_list_command) == 1
    search_list = signing_setup.index(search_list_command)
    root_trust = signing_setup.index(
        'security add-trusted-cert -r trustRoot -p codeSign'
    )
    assert 'security import "$root_cert"' not in signing_setup
    assert '-k "$keychain" "$root_cert"' in signing_setup[root_trust:]
    search_list_end = signing_setup.index(
        'if ! security import "$wwdr_cert"', search_list
    )
    search_list_setup = signing_setup[search_list:search_list_end]
    assert '/Library/Keychains/System.keychain' in search_list_setup
    assert '/System/Library/Keychains/SystemRootCertificates.keychain' in search_list_setup
    assert search_list_setup.index('"$keychain"') < search_list_setup.index('login.keychain-db')
    assert search_list_setup.index('login.keychain-db') < search_list_setup.index('/Library/Keychains/System.keychain')
    assert root_trust < search_list
    assert 'remove-trusted-cert' not in signing_setup
    assert 'echo "IOS_ROOT_TRUST_CERT=$root_cert" >> "$GITHUB_ENV"' not in signing_setup
    assert (
        'security find-identity -v -p codesigning /Library/Keychains/System.keychain 2>&1 || true'
    ) in signing_setup
    assert (
        'security find-identity -v -p codesigning '
        '/System/Library/Keychains/SystemRootCertificates.keychain 2>&1 || true'
    ) in signing_setup
    assert (
        'security find-identity -v -p codesigning login.keychain-db 2>&1 || true'
    ) in signing_setup
    assert 'if [ "${non_job_identity_count:-0}" -ne 0 ]; then' in signing_setup
    assert 'if [ "${identity_count:-0}" -ne 1 ] || [ "${distribution_count:-0}" -ne 1 ]; then' in signing_setup


def test_workflow_keeps_keychain_cleanup_unconditional_without_trust_cleanup():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    cleanup_start = workflow.index("- name: Remove the signing material")
    cleanup = workflow[cleanup_start:]
    keychain_delete = cleanup.index('security delete-keychain')

    assert "if: always()" in cleanup
    assert 'security list-keychains -d user -s login.keychain-db || true' in cleanup
    assert "add-trusted-cert" not in cleanup
    assert "remove-trusted-cert" not in cleanup
    assert keychain_delete < cleanup.index('rm -f "$RUNNER_TEMP/wattracker-ios-signing.p12"')
    assert 'security default-keychain -d user -s "$IOS_ORIGINAL_DEFAULT_KEYCHAIN" || true' in cleanup


def test_workflow_reunlocks_and_sets_partition_list_immediately_before_archive():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    archive_start = workflow.index("- name: Archive for the App Store")
    archive = workflow[archive_start:]
    mask = workflow.index("printf '::add-mask::%s\\n' \"$keychain_password\"")
    env_record = workflow.index(
        'echo "IOS_SIGNING_KEYCHAIN_PASSWORD=$keychain_password" >> "$GITHUB_ENV"'
    )
    unlock = archive.index(
        'security unlock-keychain -p "$IOS_SIGNING_KEYCHAIN_PASSWORD" "$IOS_SIGNING_KEYCHAIN"'
    )
    partition = archive.index(
        "security set-key-partition-list -S apple-tool:,apple:,codesign: -s"
    )
    xcodebuild = archive.index("archive_with_redacted_output xcodebuild archive")
    assert mask < env_record
    assert unlock < partition < xcodebuild
    assert 'OTHER_CODE_SIGN_FLAGS="--keychain $IOS_SIGNING_KEYCHAIN"' in archive
    import_start = workflow.index(
        "- name: Import the distribution certificate into a temporary keychain"
    )
    archive_start = workflow.index("- name: Archive for the App Store")
    import_setup = workflow[import_start:archive_start]
    assert 'original_default_keychain="$(security default-keychain -d user' in import_setup
    assert 'original_default_keychain="login.keychain-db"' in import_setup
    assert 'echo "IOS_ORIGINAL_DEFAULT_KEYCHAIN=$original_default_keychain" >> "$GITHUB_ENV"' in import_setup
    assert 'security default-keychain -d user -s "$IOS_SIGNING_KEYCHAIN"' in archive


def _archive_script() -> str:
    script = _workflow_step_script("Archive for the App Store")
    return script[script.index('export IOS_CERT_CN='):]


def test_workflow_masks_leaf_and_wwdr_common_names_before_diagnostics():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    leaf_cn = 'printf \'::add-mask::%s\\n\' "$leaf_cn"'
    leaf_legal_name = 'printf \'::add-mask::%s\\n\' "$leaf_legal_name"'
    cert_team = 'printf \'::add-mask::%s\\n\' "$cert_team"'
    wwdr_cn = 'printf \'::add-mask::%s\\n\' "$wwdr_cn"'
    wwdr_legal_name = 'printf \'::add-mask::%s\\n\' "$wwdr_legal_name"'
    assert workflow.count(leaf_cn) == 1
    assert workflow.count(leaf_legal_name) == 1
    assert workflow.count(cert_team) == 1
    assert workflow.count(wwdr_cn) == 1
    assert workflow.count(wwdr_legal_name) == 1
    for extraction in (
        "-nameopt sep_multiline,utf8 | sed -n 's/^ *CN=//p'",
        "-nameopt sep_multiline,utf8 | sed -n 's/^ *O=//p'",
        "-nameopt sep_multiline,utf8 | sed -n 's/^ *OU=//p'",
    ):
        assert extraction in workflow
    assert workflow.index('leaf_cn="$(/usr/bin/openssl x509') < workflow.index(leaf_cn)
    assert workflow.index('leaf_legal_name="$(/usr/bin/openssl x509') < workflow.index(leaf_legal_name)
    assert workflow.index('cert_team="$(/usr/bin/openssl x509') < workflow.index(cert_team)
    assert workflow.index('wwdr_cn="$(/usr/bin/openssl x509') < workflow.index(wwdr_cn)
    assert workflow.index('wwdr_legal_name="$(/usr/bin/openssl x509') < workflow.index(wwdr_legal_name)
    diagnose_start = workflow.index('if [ "${DIAGNOSE_SIGNING:-false}" = "true" ]; then')
    assert workflow.index(leaf_cn) < diagnose_start
    assert workflow.index(leaf_legal_name) < diagnose_start
    assert workflow.index(cert_team) < diagnose_start
    assert workflow.index(wwdr_cn) < diagnose_start
    assert workflow.index(wwdr_legal_name) < diagnose_start


def test_archive_extracts_apple_ordered_cn_and_o_and_redacts_xcodebuild_streams(tmp_path):
    script = _archive_script()
    sentinel_cn = "Apple Distribution: Sentinel Legal Name (TEAM-SENTINEL)"
    sentinel_o = "Sentinel Legal Name"
    cert_pem = tmp_path / "wattracker-ios-distribution-cert.pem"
    key_pem = tmp_path / "sentinel-key.pem"
    generated = subprocess.run(
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "30",
            "-subj",
            f"/UID=sentinel-uid/CN={sentinel_cn}/OU=TEAM-SENTINEL/O={sentinel_o}/C=US",
            "-keyout",
            str(key_pem),
            "-out",
            str(cert_pem),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert generated.returncode == 0
    subject = subprocess.run(
        [
            "/usr/bin/openssl",
            "x509",
            "-in",
            str(cert_pem),
            "-noout",
            "-subject",
            "-nameopt",
            "sep_multiline,utf8",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout
    assert [line.strip() for line in subject.splitlines()[1:]] == [
        "UID=sentinel-uid",
        f"CN={sentinel_cn}",
        "OU=TEAM-SENTINEL",
        f"O={sentinel_o}",
        "C=US",
    ]

    fake_xcodebuild = tmp_path / "xcodebuild"
    fake_xcodebuild.write_text(
        "#!/bin/sh\n"
        "if [ \"${IOS_CERT_CN:-}\" != \"$EXPECTED_CN\" ] || "
        "[ \"${IOS_CERT_LEGAL_NAME:-}\" != \"$EXPECTED_O\" ]; then\n"
        "  echo 'mask values were not exact RDN values' >&2\n"
        "  exit 42\n"
        "fi\n"
        "printf '%s\\n' \"Signing Identity: \\\"$IOS_CERT_CN\\\"\"\n"
        "printf '%s\\n' \"Signing Identity: \\\"$IOS_CERT_CN\\\"\" >&2\n"
        "printf '%s\\n' \"Subject O: $IOS_CERT_LEGAL_NAME\"\n"
        "printf '%s\\n' \"Subject O: $IOS_CERT_LEGAL_NAME\" >&2\n"
        "exit \"${FAKE_XCODEBUILD_STATUS:-0}\"\n",
        encoding="utf-8",
    )
    fake_xcodebuild.chmod(0o700)
    fake_security = tmp_path / "security"
    fake_security.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_security.chmod(0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{tmp_path}:{environment['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "APPLE_TEAM_ID": "TEAM-SENTINEL",
            "IOS_SIGNING_KEYCHAIN": str(tmp_path / "signing.keychain-db"),
            "IOS_SIGNING_KEYCHAIN_PASSWORD": "fake-keychain-password",
            "EXPECTED_CN": sentinel_cn,
            "EXPECTED_O": sentinel_o,
            "ASC_KEY_PATH": str(tmp_path / "AuthKey.p8"),
            "ASC_KEY_ID": "not-a-secret-key-id",
            "ASC_ISSUER_ID": "not-a-secret-issuer-id",
            "WATTRACKER_API_SCHEME": "https",
            "WATTRACKER_API_HOST": "cloud.example.test",
            "MARKETING_VERSION": "0.0.0",
            "BUILD_NUMBER": "1.1",
        }
    )
    result = subprocess.run(
        ["/bin/bash"],
        input=script,
        text=True,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert sentinel_cn not in result.stdout
    assert sentinel_cn not in result.stderr
    assert sentinel_o not in result.stdout
    assert sentinel_o not in result.stderr
    assert "Signing Identity: [REDACTED]" in result.stdout
    assert "Signing Identity: [REDACTED]" in result.stderr
    assert "Subject O: [REDACTED]" in result.stdout
    assert "Subject O: [REDACTED]" in result.stderr


def test_archive_redaction_preserves_a_failing_xcodebuild_status(tmp_path):
    script = _archive_script()
    cert_pem = tmp_path / "wattracker-ios-distribution-cert.pem"
    key_pem = tmp_path / "sentinel-key.pem"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "30",
            "-subj",
            "/UID=sentinel-uid/CN=Apple Distribution: Sentinel Legal Name (TEAM-SENTINEL)/OU=TEAM-SENTINEL/O=Sentinel Legal Name/C=US",
            "-keyout",
            str(key_pem),
            "-out",
            str(cert_pem),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    fake_xcodebuild = tmp_path / "xcodebuild"
    fake_xcodebuild.write_text(
        "#!/bin/sh\n"
        "echo 'Signing Identity: \"Apple Distribution: Sentinel Legal Name (TEAM-SENTINEL)\"' >&2\n"
        "exit 37\n",
        encoding="utf-8",
    )
    fake_xcodebuild.chmod(0o700)
    fake_security = tmp_path / "security"
    fake_security.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_security.chmod(0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{tmp_path}:{environment['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "APPLE_TEAM_ID": "TEAM-SENTINEL",
            "IOS_SIGNING_KEYCHAIN": str(tmp_path / "signing.keychain-db"),
            "IOS_SIGNING_KEYCHAIN_PASSWORD": "fake-keychain-password",
            "ASC_KEY_PATH": str(tmp_path / "AuthKey.p8"),
            "ASC_KEY_ID": "not-a-secret-key-id",
            "ASC_ISSUER_ID": "not-a-secret-issuer-id",
            "WATTRACKER_API_SCHEME": "https",
            "WATTRACKER_API_HOST": "cloud.example.test",
            "MARKETING_VERSION": "0.0.0",
            "BUILD_NUMBER": "1.1",
        }
    )
    result = subprocess.run(
        ["/bin/bash"],
        input=script,
        text=True,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 37
    assert "Sentinel Legal Name" not in result.stdout
    assert "Sentinel Legal Name" not in result.stderr


def test_workflow_diagnose_summary_has_exact_safe_boundary():
    script = _workflow_step_script("Import the distribution certificate into a temporary keychain")
    start_marker = 'if [ "${DIAGNOSE_SIGNING:-false}" = "true" ]; then\n'
    system_guard_marker = (
        'non_job_identities="$(\n'
        '  security find-identity -v -p codesigning /Library/Keychains/System.keychain'
    )
    start = script.index(start_marker)
    end = script.index(system_guard_marker, start)
    diagnostic = script[start:end]

    assert diagnostic.startswith(start_marker)
    assert diagnostic.rstrip().endswith("fi")
    assert script[end:].startswith(system_guard_marker)
    assert diagnostic.count("security find-identity") == 2
    assert 'valid_identity_output="$(security find-identity -v -p codesigning "$keychain" 2>&1 || true)"' in diagnostic
    assert 'all_identity_output="$(security find-identity -p codesigning "$keychain" 2>&1 || true)"' in diagnostic
    for summary in (
        "Diagnostic valid identity count:",
        "Diagnostic all identity count:",
        "Diagnostic CSSMERR status count:",
        "Diagnostic CSSMERR status:",
        "Diagnostic certificate type:",
        "Diagnostic certificate expiry:",
    ):
        assert summary in diagnostic
    assert diagnostic.count("printf 'Diagnostic ") == 8
    for forbidden in ("find-certificate", '"labl"', '"subj"', "-fingerprint", "-sha1"):
        assert forbidden not in diagnostic


def test_workflow_diagnose_summary_never_prints_fake_security_identity_name(tmp_path):
    script = _workflow_step_script("Import the distribution certificate into a temporary keychain")
    start = script.index('if [ "${DIAGNOSE_SIGNING:-false}" = "true" ]; then')
    end = script.index(
        'non_job_identities="$(\n'
        '  security find-identity -v -p codesigning /Library/Keychains/System.keychain',
        start,
    )
    diagnostic = script[start:end]

    sentinel = "Apple Distribution: Sentinel Legal Name (TEAM-SENTINEL)"
    security_runner = tmp_path / "security"
    security_runner.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = find-identity ]; then\n"
        f"  printf '%s\\n' '  1) {'A' * 40} \"{sentinel}\" (CSSMERR_TP_NOT_TRUSTED)'\n"
        "fi\n",
        encoding="utf-8",
    )
    security_runner.chmod(0o700)
    cert_pem = tmp_path / "leaf.pem"
    subprocess.run(
        ["/usr/bin/openssl", "x509", "-inform", "der", "-in", str(WWDR_CERT), "-out", str(cert_pem)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{tmp_path}:{environment['PATH']}",
            "DIAGNOSE_SIGNING": "true",
        }
    )
    result = subprocess.run(
        ["/bin/bash"],
        input=(
            "set -euo pipefail\n"
            f"keychain={tmp_path / 'fake.keychain'}\n"
            f"cert_pem={cert_pem}\n"
            f"wwdr_cert={WWDR_CERT}\n"
            + diagnostic
        ),
        text=True,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr
    assert "CSSMERR_TP_NOT_TRUSTED" in result.stdout
    assert "Diagnostic certificate type:" in result.stdout
    assert "Diagnostic certificate expiry:" in result.stdout
    assert '"labl"' not in diagnostic
    assert '"subj"' not in diagnostic


def test_helper_uses_security_export_password_argv_but_never_gh_secret_argv():
    script = Path("scripts/ios_release_secrets.py").read_text(encoding="utf-8")
    assert '"-P",' in script
    assert '"secret",' in script
    assert '"set",' in script
    assert '"--repo",' in script
    assert '"poopaskoopa/wattracker",' in script
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
        "find-identity",
        "find-key",
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
    assert all(
        argv[-2:] == ("--repo", "poopaskoopa/wattracker") for argv, _ in gh_calls
    )
    captured = capsys.readouterr()
    assert "may prompt once per key" in captured.out
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
    temp_find_calls = [
        argv
        for argv, _ in runner.calls
        if argv[:2] == ("security", "find-identity") and argv[-1] != helper.LOGIN_KEYCHAIN
    ]
    assert len(temp_find_calls) == 2
    assert all("-v" not in argv for argv in temp_find_calls)


def test_helper_cleans_up_temporary_material_when_export_fails(tmp_path, monkeypatch):
    runner = FakeRunner(fail_selected_export=True)
    generated = iter(("final-password", "source-password", "temporary-keychain-password"))
    monkeypatch.setattr(
        helper.secrets,
        "token_urlsafe",
        lambda length: next(generated),
    )

    with pytest.raises(helper.SecretSetupError, match="could not export the selected"):
        helper.install_secrets(runner=runner, input_fn=lambda prompt: "2", host_config=tmp_path / "missing")

    assert runner.temp_keychain is not None
    assert runner.deleted_keychains == [str(runner.temp_keychain)]
    assert all(not path.exists() for path in runner.export_paths)


def test_helper_aborts_if_two_identities_remain_before_final_export(tmp_path):
    runner = FakeRunner(
        keep_deleted_identities=True,
        temporary_identities=(
            ("A" * 40, "Apple Distribution: First Rider (TEAMONE)", ""),
            ("B" * 40, "Apple Distribution: Second Rider (TEAMTWO)", ""),
        ),
    )

    with pytest.raises(helper.SecretSetupError, match="exactly one identity"):
        helper.install_secrets(runner=runner, input_fn=lambda prompt: "2", host_config=tmp_path / "missing")

    assert len([path for path in runner.export_paths if path.name == "all-identities.p12"]) == 1
    assert len([path for path in runner.export_paths if path.name == "distribution.p12"]) == 0


def test_helper_aborts_if_two_private_keys_remain_before_final_export(tmp_path):
    runner = FakeRunner(extra_private_keys=1)

    with pytest.raises(helper.SecretSetupError, match="exactly one private key"):
        helper.install_secrets(runner=runner, input_fn=lambda prompt: "2", host_config=tmp_path / "missing")

    assert len([path for path in runner.export_paths if path.name == "distribution.p12"]) == 0


def test_helper_deletes_expired_identity_before_final_export(tmp_path):
    expired_fingerprint = "D" * 40
    runner = FakeRunner(
        temporary_identities=(
            ("A" * 40, "Apple Distribution: First Rider (TEAMONE)", ""),
            ("B" * 40, "Apple Distribution: Second Rider (TEAMTWO)", ""),
            (
                expired_fingerprint,
                "Apple Distribution: Expired Rider (TEAMTHREE)",
                "CSSMERR_TP_CERT_EXPIRED",
            ),
            (
                "C" * 40,
                "Developer ID Application: Other Rider (TEAMFOUR)",
                "CSSMERR_TP_NOT_TRUSTED",
            ),
        )
    )

    helper.install_secrets(
        runner=runner,
        input_fn=lambda prompt: "2",
        host_config=tmp_path / "missing",
    )

    final_export_index = next(
        index
        for index, (argv, _) in enumerate(runner.calls)
        if argv[:2] == ("security", "export")
        and Path(argv[argv.index("-o") + 1]).name == "distribution.p12"
    )
    assert expired_fingerprint in runner.deleted_identities
    assert any(
        index < final_export_index
        and argv[:2] == ("security", "delete-identity")
        and argv[argv.index("-Z") + 1] == expired_fingerprint
        for index, (argv, _) in enumerate(runner.calls)
    )


def _workflow_step_script(step_name: str) -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    step_start = workflow.index(f"      - name: {step_name}")
    run_start = workflow.index("        run: |\n", step_start) + len("        run: |\n")
    step_ends = [
        end
        for end in (
            workflow.find("\n      - name:", run_start),
            workflow.find("\n      #", run_start),
        )
        if end != -1
    ]
    step_end = min(step_ends, default=-1)
    block = workflow[run_start:] if step_end == -1 else workflow[run_start:step_end]
    return "\n".join(line[10:] for line in block.splitlines()) + "\n"


@pytest.mark.parametrize("host", ["api.wattracker.com", "https://api.wattracker.com"])
def test_workflow_host_guard_rejects_bad_hosts_when_executed(host):
    script = _workflow_step_script("Validate the iOS API host")
    environment = os.environ.copy()
    environment["WATTRACKER_IOS_API_HOST"] = host
    result = subprocess.run(
        ["/bin/bash"],
        input=script,
        text=True,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 1


def test_helper_dry_run_does_not_export_or_set_secrets(tmp_path, capsys):
    runner = FakeRunner()
    host = "cloud.example.test"
    host_config = tmp_path / "Production.local.xcconfig"
    host_config.write_text(f"WATTRACKER_API_HOST = {host};\n", encoding="utf-8")

    helper.install_secrets(
        runner=runner,
        input_fn=lambda prompt: "1",
        dry_run=True,
        host_config=host_config,
    )

    assert not any(argv[:2] == ("security", "export") for argv, _ in runner.calls)
    assert not any(argv[0] == helper.GH_PATH for argv, _ in runner.calls)
    captured = capsys.readouterr().out
    assert "no p12 was exported" in captured
    assert host not in captured


def test_helper_dry_run_never_generates_or_prints_a_password(tmp_path, monkeypatch, capsys):
    password = "dry-run-password-sentinel"

    def fail_if_generated(length):
        raise AssertionError(f"dry-run generated {password}")

    monkeypatch.setattr(helper.secrets, "token_urlsafe", fail_if_generated)
    helper.install_secrets(
        runner=FakeRunner(),
        input_fn=lambda prompt: "1",
        dry_run=True,
        host_config=tmp_path / "missing",
    )

    assert password not in capsys.readouterr().out


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


def test_helper_refuses_an_unparseable_identity_row_in_the_temporary_keychain(tmp_path):
    """A temp-keychain identity the parser cannot read must stop the export.

    Skipping it would leave it undeleted, so it would ride along in the p12;
    the private-key count is a second line of defence, not the only one.
    """
    runner = FakeRunner(
        temporary_identities=(
            ("A" * 40, "Apple Distribution: First Rider (TEAMONE)", ""),
            ("B" * 40, "Developer ID Application: Other Rider (TEAMTWO)", "SOME_OTHER_STATUS"),
        ),
    )

    with pytest.raises(helper.SecretSetupError, match="cannot parse"):
        helper.install_secrets(runner=runner, input_fn=lambda prompt: "1", host_config=tmp_path / "missing")

    assert len([path for path in runner.export_paths if path.name == "distribution.p12"]) == 0
