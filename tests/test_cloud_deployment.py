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
    """A delete action is deployed only where something actually deletes.

    #153 grants one -- `entities/delete` on `CloudAuth`, to the read identity,
    for the expired-row sweep the read plane runs in process. That is a job
    that exists. A standalone cleanup identity with delete on blobs and on
    every table, with no job behind it, still is not.
    """

    assert "cleanupIdentity" not in BICEP
    assert "cleanupBlobRole" not in BICEP
    assert "cleanupTableRole" not in BICEP
    assert "blobServices/containers/blobs/delete" not in BICEP


def test_only_the_read_identity_may_delete_and_only_from_cloudauth():
    """`CloudAuth` grew without bound because nothing could remove a row.

    The sweep needs a delete; the budget kill switch and the quota counters
    live in the same table, and an absent kill-switch row reads as *enabled*.
    Azure table roles cannot be conditioned on a row key, so the blast radius
    of this grant is the whole table and the narrowing that matters is which
    identity holds it, on which table, and how many delete actions exist at
    all. `tests/test_cloud_security.py` covers the application-side exclusion.
    """

    # Exactly one delete action in the template, in exactly one custom role.
    assert BICEP.count("tables/entities/delete") == 1
    assert "roleName: 'Wattracker Cloud Auth Sweeper'" in BICEP
    assert (
        "assignableScopes: [authTable.id]" in BICEP
    )
    # Assigned once, to the read identity, scoped to CloudAuth.
    assert BICEP.count("authSweeperRoleDefinition.id") == 1
    sweep_assignment = BICEP.split(
        "resource readAuthSweepRole 'Microsoft.Authorization/roleAssignments"
    )[1].split("resource ")[0]
    assert "guid(authTable.id, readIdentity.id, 'auth-sweeper')" in sweep_assignment
    assert "scope: authTable" in sweep_assignment
    assert "readIdentity.properties.principalId" in sweep_assignment
    assert "syncIdentity" not in sweep_assignment
    # The sync identity's CloudAuth role is unchanged: read, and nothing else.
    reader_role = BICEP.split(
        "resource authReaderRoleDefinition 'Microsoft.Authorization/roleDefinitions"
    )[1].split("resource ")[0]
    assert "tableServices/tables/entities/read" in reader_role
    assert "delete" not in reader_role
    assert "add/action" not in reader_role
    assert "update/action" not in reader_role


def test_the_read_plane_can_persist_a_revocation():
    """#153's premise: a revocation route with no write grant persists nothing.

    Revoking is an *update* to a device row, which `authManagerRoleDefinition`
    already granted the read identity -- the plane both new device routes live
    on. The runbook has to say so, because the sync identity's read-only role
    on the same table is what makes this look uncertain.
    """

    manager_role = BICEP.split(
        "resource authManagerRoleDefinition 'Microsoft.Authorization/roleDefinitions"
    )[1].split("resource ")[0]
    assert "tableServices/tables/entities/update/action" in manager_role
    assert "guid(authTable.id, readIdentity.id, 'auth-manager')" in BICEP
    assert "| `POST /api/v1/devices/{credential_id}/revoke` | read |" in RUNBOOK
    assert "| `GET /api/v1/devices` | read |" in RUNBOOK
    assert "**Cross-namespace is 404, never 403.**" in RUNBOOK


def test_the_runbook_says_the_sweep_cannot_remove_the_kill_switch():
    """An absent kill-switch row reads as enabled, so this is not a detail.

    Whoever operates this deployment now has to know that one identity can
    delete from the table the switch lives in, and what stops it.
    """

    assert "### The expired-row sweep" in RUNBOOK
    assert "**It cannot delete the kill switch.**" in RUNBOOK
    assert "NEVER_SWEEP_RECORD_KINDS" in RUNBOOK
    assert "authSweeperRoleDefinition" in RUNBOOK
    # The old absolute -- "no managed identity holds a table entities/delete
    # action" -- is no longer true anywhere in the runbook.
    assert "no\nmanaged identity holds a table `entities/delete`" not in RUNBOOK
    assert "No deployed managed identity holds\na table `entities/delete`" not in RUNBOOK
