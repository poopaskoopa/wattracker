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


def test_production_app_limits_are_documented_as_best_effort_and_apim_is_durable():
    assert "name: 'CloudReplay'" in BICEP
    assert "replay-writer" in BICEP
    assert "best-effort process-local" in RUNBOOK
    assert '<quota-by-key calls="1000" renewal-period="86400"' in BICEP
    assert '<rate-limit-by-key calls="60" renewal-period="60"' in BICEP
