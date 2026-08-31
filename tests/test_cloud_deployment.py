from pathlib import Path


ROOT = Path(__file__).parents[1]
BICEP = (ROOT / "infra" / "azure" / "main.bicep").read_text()
RUNBOOK = (ROOT / "docs" / "cloud-sync.md").read_text()


def test_container_origins_are_private_to_the_internal_environment():
    assert "vnetConfiguration:" in BICEP
    assert "internal: true" in BICEP
    assert BICEP.count("external: false") == 2
    assert "ipSecurityRestrictions" not in BICEP
    assert "apimBackendIpRanges" not in BICEP
    assert BICEP.count("clientCertificateMode: 'Ignore'") == 2
    assert "environment is internal" in RUNBOOK
    assert "both app ingresses are non-external" in RUNBOOK
    assert "private Container App FQDNs" in RUNBOOK


def test_apim_uses_vnet_and_does_not_claim_client_certificate_authentication():
    assert "sku: { name: 'Standard'; capacity: 1 }" in BICEP
    assert "virtualNetworkType: 'External'" in BICEP
    assert "virtualNetworkConfiguration:" in BICEP
    assert "subnetResourceId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'apim')" in BICEP
    assert "<validate-client-certificate" not in BICEP
    assert "X-APIM-Client-Certificate-Verified" not in BICEP
    assert "authentication-certificate" not in BICEP
    assert "X-APIM-Request-Proof" in BICEP


def test_production_app_limits_are_documented_as_durable_not_best_effort():
    """The app's daily counters are the cost control, not a backstop.

    #164 removes the gateway whose `quota-by-key` policy used to be the only
    durable limit, so the runbook must not still tell an operator that the
    app-side counters reset on every replica change -- they do not, and an
    operator who believes they do has no reason to trust any of them.
    """

    assert "name: 'CloudReplay'" in BICEP
    assert "replay-writer" in BICEP
    assert "best-effort" not in RUNBOOK
    assert "**The app-side daily quota counters are durable.**" in RUNBOOK
    assert "refuses a non-durable quota manager at boot" in RUNBOOK
    # The gateway policies stay documented while the gateway exists; what
    # changed is that the app no longer depends on them.
    assert '<quota-by-key calls="1000" renewal-period="86400"' in BICEP
    assert '<rate-limit-by-key calls="60" renewal-period="60"' in BICEP


def test_cleanup_delete_identity_is_not_deployed_without_a_cleanup_job():
    assert "cleanupIdentity" not in BICEP
    assert "cleanupBlobRole" not in BICEP
    assert "cleanupTableRole" not in BICEP
