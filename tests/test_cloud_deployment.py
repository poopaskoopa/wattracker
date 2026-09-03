import importlib.util
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
BICEP = (ROOT / "infra" / "azure" / "main.bicep").read_text()
RUNBOOK = (ROOT / "docs" / "cloud-sync.md").read_text()
BUDGET_HOOK_ROOT = ROOT / "infra" / "azure" / "budget-hook"
BUDGET_HOOK = (BUDGET_HOOK_ROOT / "function_app.py").read_text()
BUDGET_HOOK_IMPL = (ROOT / "wattracker" / "cloud" / "budget_hook.py").read_text()
BUDGET_HOOK_REQUIREMENTS = (BUDGET_HOOK_ROOT / "requirements.txt").read_text()
BUDGET_HOOK_README = (BUDGET_HOOK_ROOT / "README.md").read_text()
AZURE_README = (ROOT / "infra" / "azure" / "README.md").read_text()
BUDGET_HOOK_HOST = json.loads((BUDGET_HOOK_ROOT / "host.json").read_text())
PACKAGE_HELPER = (ROOT / "scripts" / "package_budget_hook.py").read_text()


def test_public_container_apps_are_tls_terminated_and_authenticate_at_the_app():
    assert "vnetConfiguration:" in BICEP
    assert "internal: false" in BICEP
    assert BICEP.count("external: true") == 2
    assert BICEP.count("allowInsecure: false") == 2
    assert BICEP.count("clientCertificateMode: 'Ignore'") == 2
    assert "readIdentity.properties.clientId" in BICEP
    assert "syncIdentity.properties.clientId" in BICEP
    assert "Microsoft.ApiManagement" not in BICEP
    assert "X-APIM-Request-Proof" not in BICEP
    assert "public HTTPS ingress" in RUNBOOK
    assert "application enforces" in RUNBOOK


def test_storage_uses_service_endpoints_and_a_deny_by_default_firewall():
    assert "publicNetworkAccess: 'Enabled'" in BICEP
    assert "allowBlobPublicAccess: false" in BICEP
    assert "allowSharedKeyAccess: false" in BICEP
    assert "defaultAction: 'Deny'" in BICEP
    assert "bypass: 'None'" in BICEP
    assert "serviceEndpoints:" in BICEP
    assert "service: 'Microsoft.Storage'" in BICEP
    assert "budgetHookIpRules" in BICEP
    assert "ipRules:" in BICEP
    assert "resourceAccessRules:" in BICEP
    assert re.search(
        r"resourceAccessRules:\s*\[\s*\{\s*tenantId:\s*subscription\(\)\.tenantId\s*"
        r"resourceId:\s*budgetHookApp\.id",
        BICEP,
    )
    assert "virtualNetworkRules:" in BICEP
    assert "resource acaSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01'" in BICEP
    assert "id: acaSubnet.id" in BICEP
    assert "infrastructureSubnetId: acaSubnet.id" in BICEP
    assert "resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName" not in BICEP
    assert "Microsoft.Network/privateEndpoints" not in BICEP
    assert "Microsoft.Network/privateDnsZones" not in BICEP
    assert "storage firewall" in RUNBOOK
    assert "anonymous blobs" in RUNBOOK


def test_budget_actions_target_authenticated_durable_kill_switch_handlers():
    assert "budgetHookRoleDefinition" in BICEP
    assert "budgetHookPrincipalId" in BICEP
    assert "budgetHookHost" in BICEP
    assert "budgetHookFunctionAppName" in BICEP
    assert "param budgetHookFunctionKey" not in BICEP
    assert "resource budgetHookApp 'Microsoft.Web/sites@2022-09-01' existing" in BICEP
    assert "listKeys('${budgetHookApp.id}/host/default', '2022-03-01').functionKeys.default" in BICEP
    assert "name: 'CloudAuth'" in BICEP
    assert "name: 'CloudControl'" in BICEP
    assert "controlReaderRoleDefinition" in BICEP
    assert "assignableScopes: [controlTable.id]" in BICEP
    assert "readControlRole" in BICEP
    assert "syncControlRole" in BICEP
    assert re.search(
        r"var writeShutdownWebhookUri = 'https://\$\{budgetHookHost\}/budget/disable-writes\?code=",
        BICEP,
    )
    assert re.search(
        r"var publicShutdownWebhookUri = 'https://\$\{budgetHookHost\}/budget/disable-public-api\?code=",
        BICEP,
    )
    assert re.search(
        r"resource writeShutdownActionGroup[\s\S]*?serviceUri: writeShutdownWebhookUri",
        BICEP,
    )
    assert re.search(
        r"resource publicShutdownActionGroup[\s\S]*?serviceUri: publicShutdownWebhookUri",
        BICEP,
    )
    assert '"/budget/disable-writes"' in BUDGET_HOOK_IMPL
    assert '"/budget/disable-public-api"' in BUDGET_HOOK_IMPL
    assert '"/budget/clear"' in BUDGET_HOOK_IMPL
    assert "disable_writes" in BUDGET_HOOK_IMPL
    assert "disable_public_api" in BUDGET_HOOK_IMPL
    assert "clear_kill_switch" in BUDGET_HOOK_IMPL
    assert "AuthLevel.FUNCTION" in BUDGET_HOOK
    assert "platform_authenticated=True" in BUDGET_HOOK
    assert "create_budget_hook_app" in BUDGET_HOOK
    assert "from_managed_identity" in BUDGET_HOOK
    budget_role = BICEP.split(
        "resource budgetHookRoleDefinition 'Microsoft.Authorization/roleDefinitions"
    )[1].split("resource replayWriterRoleDefinition")[0]
    assert "entities/delete" not in budget_role
    for notification, threshold in (("actual50", 50), ("actual80", 80), ("actual100", 100)):
        assert re.search(
            rf"resource budget[\s\S]*?properties:\s*\{{\s*amount:\s*10"
            rf"[\s\S]*?{notification}:\s*\{{[\s\S]*?threshold:\s*{threshold}"
            rf"\s+thresholdType:\s*'Actual'",
            BICEP,
        )
    assert "param budgetStartDate string" in BICEP
    assert "param budgetEndDate string" in BICEP
    assert "@minLength(32)" in BICEP
    assert "startDate: budgetStartDate" in BICEP
    assert "endDate: budgetEndDate" in BICEP
    assert "startDate: '2026-01-01'" not in BICEP
    assert "Azure Function" in RUNBOOK
    assert "budget 80%" in RUNBOOK
    assert "budget 100%" in RUNBOOK


def test_table_upsert_roles_include_insert_or_merge_write_and_keep_table_scopes():
    write_action = "Microsoft.Storage/storageAccounts/tableServices/tables/entities/write"
    assert BICEP.count(write_action) == 3
    role_scopes = {
        "authManagerRoleDefinition": "storage.id",
        "budgetHookRoleDefinition": "controlTable.id",
        "replayWriterRoleDefinition": "replayTable.id",
    }
    for role_name, table_id in role_scopes.items():
        role = BICEP.split(f"resource {role_name} ", 1)[1].split("\nresource ", 1)[0]
        assert write_action in role
        assert f"assignableScopes: [{table_id}]" in role

    assignments = {
        "readAuthRole": "authTable",
        "budgetHookRole": "controlTable",
        "syncReplayRole": "replayTable",
    }
    for assignment_name, table_name in assignments.items():
        assignment = BICEP.split(f"resource {assignment_name} ", 1)[1].split("\nresource ", 1)[0]
        assert f"scope: {table_name}" in assignment


def test_budget_hook_project_has_a_root_host_and_no_parent_checkout_requirement():
    assert BUDGET_HOOK_HOST == {
        "version": "2.0",
        "extensions": {"http": {"routePrefix": ""}},
    }
    assert "-e ../../../" not in BUDGET_HOOK_REQUIREMENTS
    assert ".." not in BUDGET_HOOK_REQUIREMENTS
    assert "python scripts/package_budget_hook.py" in BUDGET_HOOK_README
    assert "func azure functionapp publish APP_NAME" in BUDGET_HOOK_README
    assert "refuses to overwrite" in BUDGET_HOOK_README
    assert "rm -rf -- build/azure-budget-hook" in BUDGET_HOOK_README
    assert "refuses to overwrite" in AZURE_README
    assert "rm -rf -- build/azure-budget-hook" in AZURE_README
    assert "shutil.copytree" in PACKAGE_HELPER
    assert "wattracker" in PACKAGE_HELPER
    assert "/budget/disable-writes" in BUDGET_HOOK_README
    assert "/budget/disable-public-api" in BUDGET_HOOK_README
    assert "/api/budget" not in BUDGET_HOOK_README


def test_budget_hook_stager_copies_the_cloud_package_without_installing_the_repo(tmp_path):
    helper_path = ROOT / "scripts" / "package_budget_hook.py"
    spec = importlib.util.spec_from_file_location("package_budget_hook", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    # pytest supplies an isolated temp directory; the helper must not mutate
    # the tracked Function project or require the repository parent at publish
    # time.
    staged = helper.stage_budget_hook(tmp_path / "budget-hook")
    assert (staged / "host.json").is_file()
    assert (staged / "requirements.txt").is_file()
    assert (staged / "wattracker" / "cloud" / "budget_hook.py").is_file()
    assert (staged / "wattracker" / "cloud" / "limits.py").is_file()
    assert (staged / "wattracker" / "cloud" / "security.py").is_file()


def test_budget_hook_stager_refuses_to_overwrite_existing_output(tmp_path):
    helper_path = ROOT / "scripts" / "package_budget_hook.py"
    spec = importlib.util.spec_from_file_location("package_budget_hook", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    output = tmp_path / "budget-hook"
    output.mkdir()
    with pytest.raises(FileExistsError, match="output already exists"):
        helper.stage_budget_hook(output)


def test_apim_and_private_endpoint_parameters_are_removed_from_the_template():
    for legacy in (
        "param publicApiEnabled",
        "param writesEnabled",
        "param tenantId",
        "param apimKeyVaultName",
        "param apimCertificateSecretUri",
        "param apimHostName",
        "param publisherEmail",
        "param apiAudience",
        "param apimProofSecret",
        "virtualNetworkType:",
        "apim-proof-secret",
    ):
        assert legacy not in BICEP


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
    assert "cannot reach the budget kill switch in\n// CloudControl" in BICEP
    assert "including the budget kill switch" not in BICEP
    # The old absolute -- "no managed identity holds a table entities/delete
    # action" -- is no longer true anywhere in the runbook.
    assert "no\nmanaged identity holds a table `entities/delete`" not in RUNBOOK
    assert "No deployed managed identity holds\na table `entities/delete`" not in RUNBOOK
