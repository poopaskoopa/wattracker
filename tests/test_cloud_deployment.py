import importlib.util
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
BICEP = (ROOT / "infra" / "azure" / "main.bicep").read_text()
PARAMS = (ROOT / "infra" / "azure" / "main.bicepparam").read_text()
DEPLOY_RUNBOOK = (ROOT / "infra" / "azure" / "DEPLOY.md").read_text()
RUNBOOK = (ROOT / "docs" / "cloud-sync.md").read_text()
BUDGET_HOOK_ROOT = ROOT / "infra" / "azure" / "budget-hook"
BUDGET_HOOK = (BUDGET_HOOK_ROOT / "function_app.py").read_text()
BUDGET_HOOK_IMPL = (ROOT / "wattracker" / "cloud" / "budget_hook.py").read_text()
BUDGET_HOOK_REQUIREMENTS = (BUDGET_HOOK_ROOT / "requirements.txt").read_text()
BUDGET_HOOK_README = (BUDGET_HOOK_ROOT / "README.md").read_text()
AZURE_README = (ROOT / "infra" / "azure" / "README.md").read_text()
BUDGET_HOOK_HOST = json.loads((BUDGET_HOOK_ROOT / "host.json").read_text())
PACKAGE_HELPER = (ROOT / "scripts" / "package_budget_hook.py").read_text()


def test_builtin_storage_data_role_ids_match_their_assignments():
    assert (
        "roleDefinitionId: subscriptionResourceId("
        "'Microsoft.Authorization/roleDefinitions', "
        "'76199698-9eea-4c19-bc75-cec21354c6b6')"
    ) in BICEP
    assert (
        "param blobReaderRoleDefinitionId = "
        "'2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'"
    ) in PARAMS


def test_budget_drill_targets_the_source_kill_switch_row():
    assert 'b"wattracker-cloud-kill-switch-v1\\x00deployment"' in DEPLOY_RUNBOOK
    assert "--partition-key '__wattracker_auth_v1__'" in DEPLOY_RUNBOOK
    assert '--row-key "$WATTRACKER_KILL_SWITCH_ROW_KEY"' in DEPLOY_RUNBOOK


def test_phase_three_publish_commands_use_the_repo_python_and_stop_on_failure():
    phase_three = DEPLOY_RUNBOOK.split(
        "## 3. Stage and publish the budget hook", 1
    )[1].split("\n## 4.", 1)[0]
    shell_blocks = re.findall(r"```sh\n(.*?)\n```", phase_three, re.DOTALL)

    staging_commands = re.findall(
        r"(?m)^\.venv/bin/python scripts/package_budget_hook\.py$", phase_three
    )
    publish_commands = re.findall(
        r'(?m)^func azure functionapp publish "\$FUNCTION_APP_NAME" --python$',
        phase_three,
    )

    assert len(staging_commands) == 2
    assert len(publish_commands) == 2
    assert not re.search(r"(?m)^python scripts/package_budget_hook\.py$", phase_three)
    assert len(shell_blocks) == 2
    assert all(block.splitlines()[0] == "set -euo pipefail" for block in shell_blocks)


def test_flex_migration_runbook_cleans_legacy_params_and_republishes_after_recreate():
    phase_one = DEPLOY_RUNBOOK.split(
        "## 1. Bootstrap or recreate the Flex Function App", 1
    )[1].split("\n## 2.", 1)[0]
    phase_two = DEPLOY_RUNBOOK.split(
        "## 2. Complete parameters and deploy the main template", 1
    )[1].split("\n## 3.", 1)[0]

    assert "--runtime-version 3.12" in phase_one
    assert "--runtime-version 3.11" not in phase_one
    assert re.search(
        r"delete the\s+entire `budgetHookIpRules` parameter block.*?"
        r"`main\.bicep` no longer declares.*?BCP259",
        phase_two,
        re.DOTALL,
    )
    assert "A recreated Function App starts empty." in phase_one
    assert re.search(r"run the Phase 3 app-settings\s+command again", phase_one)
    assert "before Phase 4" in phase_one


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
    assert "budgetHookIpRules" not in BICEP
    assert "ipRules:" not in BICEP
    assert "budgetHookIpRules" not in PARAMS
    budget_subnet = BICEP.split(
        "resource budgetHookSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01'",
        1,
    )[1].split("\nresource ", 1)[0]
    assert "name: 'budget-hook-flex'" in budget_subnet
    assert "addressPrefix: '10.42.2.0/27'" in budget_subnet
    assert "serviceName: 'Microsoft.App/environments'" in budget_subnet
    assert "service: 'Microsoft.Storage'" in budget_subnet
    uncommented_bicep = re.sub(r"//[^\n]*", "", BICEP)
    assert not re.search(r"\bresourceAccessRules\s*:", uncommented_bicep)
    virtual_network_rules = re.search(
        r"virtualNetworkRules:\s*\[(?P<body>.*?)\n\s*\]",
        uncommented_bicep,
        re.DOTALL,
    )
    assert virtual_network_rules
    rules_body = virtual_network_rules.group("body")
    assert rules_body.count("{") == rules_body.count("}") == 2
    assert re.findall(
        r"^\s*id:\s*(.+?)\s*$",
        rules_body,
        re.MULTILINE,
    ) == ["acaSubnet.id", "budgetHookSubnet.id"]
    assert "resource acaSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01'" in BICEP
    assert "id: acaSubnet.id" in BICEP
    assert "id: budgetHookSubnet.id" in BICEP
    assert "infrastructureSubnetId: acaSubnet.id" in BICEP
    assert "infrastructureSubnetId: budgetHookSubnet.id" not in BICEP
    assert "resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName" not in BICEP
    assert "Microsoft.Network/privateEndpoints" not in BICEP
    assert "Microsoft.Network/privateDnsZones" not in BICEP
    assert "storage firewall" in RUNBOOK
    assert "anonymous blobs" in RUNBOOK


def test_both_container_apps_scale_to_zero_and_never_past_one_replica():
    """#168 Layer 2: the replica count is a limit, not a cost preference.

    The per-second rate window and the backend concurrency ceiling in
    `wattracker/cloud/limits.py` are process-local by design, so a second
    replica does not share them -- it silently doubles both. `maxReplicas: 1`
    is what makes "process-local" and "deployment-wide" the same sentence, and
    nothing else in the template says so. `minReplicas: 0` is pinned with it
    because the `$2-5/mo` baseline assumes no compute charge while idle.
    """

    assert BICEP.count("maxReplicas: 1") == 2
    assert BICEP.count("minReplicas: 0") == 2
    assert not re.search(r"maxReplicas: (?!1\b)", BICEP)
    assert not re.search(r"minReplicas: (?!0\b)", BICEP)
    for app in ("readApp", "syncApp"):
        block = BICEP.split(f"resource {app} 'Microsoft.App/containerApps", 1)[1]
        block = block.split("\nresource ", 1)[0]
        assert re.search(r"scale:\s*\{\s*minReplicas: 0\s+maxReplicas: 1\s*\}", block)
    assert "minReplicas: 0" in RUNBOOK


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
    """A delete action is *held* only where something actually deletes.

    #153 grants one -- `entities/delete` on `CloudAuth`, to the read identity,
    for the expired-row sweep the read plane runs in process. That is a job
    that exists. A standalone cleanup identity with delete on blobs and on
    every table, with no job behind it, still is not.

    #170 adds the first blob delete in the template, for the operator scope
    wipe. The original assertion here was "no blob delete exists anywhere",
    and it cannot survive an issue whose entire purpose is deleting a rider's
    blobs. What survives is the rule it was protecting, restated at the only
    place it can now be checked: the blob delete exists once, in a role
    definition, and the deployment's own parameter file assigns it to nobody.
    The operator CLI that will hold it is #169 and is not built.
    """

    assert "cleanupIdentity" not in BICEP
    assert "cleanupBlobRole" not in BICEP
    assert "cleanupTableRole" not in BICEP
    assert BICEP.count("blobServices/containers/blobs/delete") == 1
    wipe_blob_role = BICEP.split(
        "resource operatorWipeBlobRoleDefinition "
        "'Microsoft.Authorization/roleDefinitions"
    )[1].split("resource ")[0]
    assert "blobServices/containers/blobs/delete" in wipe_blob_role
    # Assigned only through a parameter, and the skeleton leaves it empty.
    for assignment in (
        "operatorWipeBlobRole ",
        "operatorWipeObjectTableRole ",
        "operatorWipeAuthTableRole ",
    ):
        block = BICEP.split(f"resource {assignment}")[1].split("resource ")[0]
        assert "if (!empty(operatorWipePrincipalId))" in block
        assert "principalId: operatorWipePrincipalId" in block
        assert "readIdentity" not in block
        assert "syncIdentity" not in block
    assert "param operatorWipePrincipalId = ''" in PARAMS


def test_sync_blob_writer_role_has_only_supported_write_actions():
    role = BICEP.split(
        "resource syncBlobWriterRoleDefinition "
        "'Microsoft.Authorization/roleDefinitions"
    )[1].split("resource ")[0]
    actions = re.findall(r"'([^']+)'", role.split("dataActions: [", 1)[1].split("]", 1)[0])
    assert actions == [
        "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read",
        "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action",
        "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write",
    ]


def test_the_operator_wipe_role_cannot_reach_the_kill_switch_table():
    """#170's separation: the wipe identity is not the sync identity.

    The sync identity's storage roles carry no delete action at all, which is
    what makes a compromised writer unable to destroy data, so the wipe has to
    be a different principal. What bounds the new grant is the *scope* it is
    assignable to: `CloudObjects`, `CloudAuth` and the object container, and
    deliberately not `CloudControl`. An absent kill-switch row reads as
    ENABLED, so the switch being unreachable by the grant beats the wipe being
    trusted not to touch it. `tests/test_cloud_scope_wipe.py` covers the
    application-side exclusion by record kind, which is what protects the
    quota counters that do share `CloudAuth`.
    """

    table_role = BICEP.split(
        "resource operatorWipeTableRoleDefinition "
        "'Microsoft.Authorization/roleDefinitions"
    )[1].split("resource ")[0]
    assert "assignableScopes: [objectTable.id, authTable.id]" in table_role
    assert "controlTable" not in table_role
    assert "replayTable" not in table_role
    blob_role = BICEP.split(
        "resource operatorWipeBlobRoleDefinition "
        "'Microsoft.Authorization/roleDefinitions"
    )[1].split("resource ")[0]
    assert "assignableScopes: [objectContainer.id]" in blob_role
    # `purge_scope` ends by listing the scope's blob prefix, so a blob with no
    # row is still deleted. Azure's List Blobs is gated on the *blobs* read
    # data action, so losing this line would not lose a test elsewhere -- it
    # would 403 the wipe in production, after the credentials were gone.
    assert (
        "'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'"
        in blob_role
    )
    # Each role is referenced by exactly the operator assignments and the
    # read identity's switch-gated copies; every one of those is checked by
    # scope, principal and condition in
    # `test_every_wipe_role_assignment_is_accounted_for`.
    assert BICEP.count("operatorWipeTableRoleDefinition.id") == 4
    assert BICEP.count("operatorWipeBlobRoleDefinition.id") == 2
    for definition in ("syncBlobWriterRoleDefinition", "syncTableWriterRoleDefinition"):
        role = BICEP.split(
            f"resource {definition} 'Microsoft.Authorization/roleDefinitions"
        )[1].split("resource ")[0]
        assert "delete" not in role


def test_only_the_read_identity_may_delete_and_only_from_cloudauth():
    """`CloudAuth` grew without bound because nothing could remove a row.

    The sweep needs a delete; the budget kill switch and the quota counters
    live in the same table, and an absent kill-switch row reads as *enabled*.
    Azure table roles cannot be conditioned on a row key, so the blast radius
    of this grant is the whole table and the narrowing that matters is which
    identity holds it, on which table, and how many delete actions exist at
    all. `tests/test_cloud_security.py` covers the application-side exclusion.

    #170 adds the second table delete in the template, for the operator scope
    wipe, so "exactly one" becomes "exactly these two, each accounted for".
    The read identity is still the only *deployed workload* identity holding a
    table delete: the wipe roles are assigned only when a deployment names an
    operator principal, which the skeleton parameter file does not.
    """

    # Two delete actions in the template, in two custom roles, each named.
    assert BICEP.count("tables/entities/delete") == 2
    sweeper_role = BICEP.split(
        "resource authSweeperRoleDefinition 'Microsoft.Authorization/roleDefinitions"
    )[1].split("resource ")[0]
    wipe_table_role = BICEP.split(
        "resource operatorWipeTableRoleDefinition "
        "'Microsoft.Authorization/roleDefinitions"
    )[1].split("resource ")[0]
    assert "tables/entities/delete" in sweeper_role
    assert "tables/entities/delete" in wipe_table_role
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


# ---------------------------------------------------------------------------
# #170 item 6: one switch gives the read identity the wipe grant and the route
# ---------------------------------------------------------------------------


def _until_closing_brace(text):
    """A declaration up to its own closing ``}`` at column 0.

    Nested braces are indented, so the first column-0 ``}`` ends the
    declaration; comments written before the *next* declaration are not
    part of this one.
    """

    return text.split("\n}\n", 1)[0] + "\n}"


def _resource_block(name):
    """The text of one top-level ``resource <name>`` declaration."""

    return _until_closing_brace(BICEP.split(f"\nresource {name} ", 1)[1])


def _blocks_of_type(resource_type):
    blocks = {}
    for chunk in BICEP.split("\nresource ")[1:]:
        name, rest = chunk.split(" ", 1)
        if rest.startswith(f"'{resource_type}@"):
            blocks[name] = _until_closing_brace(rest)
    return blocks


def _role_assignment_blocks():
    """Every roleAssignments declaration, by symbolic name."""

    return _blocks_of_type("Microsoft.Authorization/roleAssignments")


def _role_definition_actions():
    """Every custom role's dataActions, by symbolic name."""

    return {
        name: re.findall(
            r"'(Microsoft\.Storage/[^']+)'",
            block.split("dataActions: [", 1)[1].split("]", 1)[0],
        )
        for name, block in _blocks_of_type(
            "Microsoft.Authorization/roleDefinitions"
        ).items()
    }


def _bicep_string_var(name):
    """A single-quoted Bicep string ``var``, with its escapes undone."""

    match = re.search(rf"(?m)^var {name} = '((?:[^'\\]|\\.)*)'$", BICEP)
    assert match, name
    return re.sub(r"\\(.)", r"\1", match.group(1))


_WIPE_ROLE_IDS = (
    "operatorWipeBlobRoleDefinition.id",
    "operatorWipeTableRoleDefinition.id",
    "wipeLockRoleDefinition.id",
)
_READ_WIPE_ASSIGNMENTS = {
    "readWipeBlobRole": ("objectContainer", "operatorWipeBlobRoleDefinition.id"),
    "readWipeObjectTableRole": ("objectTable", "operatorWipeTableRoleDefinition.id"),
    "readWipeAuthTableRole": ("authTable", "operatorWipeTableRoleDefinition.id"),
    "readWipeLockRole": ("objectContainer", "wipeLockRoleDefinition.id"),
}
_OPERATOR_WIPE_ASSIGNMENTS = {
    "operatorWipeBlobRole": ("objectContainer", "operatorWipeBlobRoleDefinition.id"),
    "operatorWipeObjectTableRole": ("objectTable", "operatorWipeTableRoleDefinition.id"),
    "operatorWipeAuthTableRole": ("authTable", "operatorWipeTableRoleDefinition.id"),
    "operatorWipeLockRole": ("objectContainer", "wipeLockRoleDefinition.id"),
}
_LOCK_ASSIGNMENTS = ("readWipeLockRole", "operatorWipeLockRole")
_BLOB_WRITE = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write"
#: The exact ABAC condition every lock assignment must carry.  Spelled out
#: here rather than read back from the template, so widening it is a diff.
_LOCK_PATH_PATTERN = "?" * 64 + ":*/__lock"
_LOCK_CONDITION = (
    "((!(ActionMatches{'" + _BLOB_WRITE + "'})) OR "
    "(@Resource[Microsoft.Storage/storageAccounts/blobServices/containers/"
    "blobs:path] StringLike '" + _LOCK_PATH_PATTERN + "'))"
)


def test_the_account_wipe_switch_defaults_off_in_template_and_skeleton():
    assert re.search(r"(?m)^param enableAccountWipe bool = false$", BICEP)
    assert re.search(r"(?m)^param enableAccountWipe = false$", PARAMS)
    assert len(re.findall(r"(?m)^param enableAccountWipe\b", BICEP)) == 1
    assert len(re.findall(r"(?m)^param enableAccountWipe\b", PARAMS)) == 1
    # The accepted risk is written down where the switch is.
    param_comment = BICEP.split("param enableAccountWipe bool", 1)[0].rsplit(
        "param operatorWipePrincipalId", 1
    )[1]
    assert "source of truth" in param_comment
    assert "replica" in param_comment
    assert "CloudControl" in param_comment


def test_the_read_identity_wipe_grants_hang_on_the_switch_and_skip_cloudcontrol():
    for name, (scope, role) in _READ_WIPE_ASSIGNMENTS.items():
        block = _resource_block(name)
        assert block.startswith(
            "'Microsoft.Authorization/roleAssignments@2022-04-01' = "
            "if (enableAccountWipe) {"
        ), name
        assert f"\n  scope: {scope}\n" in block
        assert f"roleDefinitionId: {role}\n" in block
        assert "principalId: readIdentity.properties.principalId" in block
        assert "principalType: 'ServicePrincipal'" in block
        # A name of its own: never the operator assignment's guid.
        assert f"guid({scope}.id, readIdentity.id, 'read-account-wipe-" in block
        for forbidden in (
            "syncIdentity", "controlTable", "replayTable", "operatorWipePrincipalId",
        ):
            assert forbidden not in block, (name, forbidden)
    # The operator path is unchanged.
    for name, (scope, role) in _OPERATOR_WIPE_ASSIGNMENTS.items():
        block = _resource_block(name)
        assert "= if (!empty(operatorWipePrincipalId)) {" in block
        assert f"\n  scope: {scope}\n" in block
        assert f"roleDefinitionId: {role}\n" in block
        assert "principalId: operatorWipePrincipalId" in block


def test_every_wipe_role_assignment_is_accounted_for():
    """No assignment of a wipe role exists beyond the eight named above.

    This is the check that catches a seventh: a wipe role handed to the sync
    identity, or scoped to CloudControl, shows up here whatever it is called.
    """

    holders = {
        name: block
        for name, block in _role_assignment_blocks().items()
        if any(role in block for role in _WIPE_ROLE_IDS)
    }
    assert set(holders) == set(_READ_WIPE_ASSIGNMENTS) | set(_OPERATOR_WIPE_ASSIGNMENTS)
    for name, block in holders.items():
        scope = re.search(r"\n  scope: (\w+)\n", block).group(1)
        assert scope in {"objectContainer", "objectTable", "authTable"}, name


def test_the_sync_identity_never_holds_a_wipe_role():
    sync_assignments = {
        name: block
        for name, block in _role_assignment_blocks().items()
        if "syncIdentity" in block
    }
    assert len(sync_assignments) >= 4  # the parse found them at all
    for name, block in sync_assignments.items():
        for role in _WIPE_ROLE_IDS:
            assert role not in block, name
        assert "authSweeperRoleDefinition" not in block, name
        assert "enableAccountWipe" not in block, name


def test_the_read_app_gets_the_wipe_flag_only_under_the_switch():
    from wattracker.cloud.runtime import _env_flag

    runtime_source = (ROOT / "wattracker" / "cloud" / "runtime.py").read_text()
    assert '_env_flag("WATTRACKER_CLOUD_ALLOW_ACCOUNT_WIPE")' in runtime_source

    # The flag is spelled once in the template, inside the switch-gated var.
    assert len(re.findall(r"name: 'WATTRACKER_CLOUD_ALLOW_ACCOUNT_WIPE'", BICEP)) == 1
    var = re.split(
        r"\n(?:var |//)", BICEP.split("\nvar readAccountWipeEnv = ", 1)[1], maxsplit=1
    )[0]
    assert var.startswith("enableAccountWipe ? [")
    assert var.rstrip().endswith("] : []")
    assert "name: 'WATTRACKER_CLOUD_ALLOW_ACCOUNT_WIPE'" in var
    value = re.search(r"value: '([^']*)'", var).group(1)
    # The value the template sets is one the runtime actually reads as "on".
    import os
    from unittest import mock

    with mock.patch.dict(os.environ, {"WATTRACKER_CLOUD_ALLOW_ACCOUNT_WIPE": value}):
        assert _env_flag("WATTRACKER_CLOUD_ALLOW_ACCOUNT_WIPE") is True
    read_app = _resource_block("readApp")
    assert "env: concat([" in read_app
    assert "], readAccountWipeEnv)" in read_app
    assert "WATTRACKER_CLOUD_ALLOW_ACCOUNT_WIPE" not in read_app
    assert BICEP.count("readAccountWipeEnv") == 2


def test_the_sync_app_never_gets_the_wipe_flag():
    sync_app = _resource_block("syncApp")
    assert "WATTRACKER_CLOUD_ALLOW_ACCOUNT_WIPE" not in sync_app
    assert "readAccountWipeEnv" not in sync_app
    assert "enableAccountWipe" not in sync_app


def test_the_read_app_revision_waits_for_the_wipe_grants():
    read_app = _resource_block("readApp")
    depends = read_app.split("dependsOn: [", 1)[1].split("]", 1)[0]
    assert set(depends.split()) == set(_READ_WIPE_ASSIGNMENTS)
    assert "readWipeLockRole" in depends.split()


def _account_wipe_rollback():
    """DEPLOY.md's account-wipe rollback, from its bold lead to the next section."""

    assert DEPLOY_RUNBOOK.count("**Rollback") == 1
    section = DEPLOY_RUNBOOK.split("**Rollback", 1)[1]
    for end in ("\nHave an Azure subscription", "\n## ", "\n### "):
        section = section.split(end, 1)[0]
    return section


def test_the_account_wipe_rollback_deletes_the_grants_explicitly():
    """Review round 2: a redeploy with the switch off does not revoke anything.

    ``deploy_cloud.py`` deploys in Incremental mode, where a conditional
    resource whose condition turns false is skipped, never deleted.  The
    rollback used to claim the redeploy removed the read identity's wipe
    assignments; it removes only the route flag.  Pin the doc to the explicit
    delete, naming every role the read identity is assigned under the switch.
    """

    deploy_script = (ROOT / "scripts" / "deploy_cloud.py").read_text()
    assert "--mode" not in deploy_script  # still ARM's default: Incremental

    rollback = _account_wipe_rollback()
    assert "az role assignment delete" in rollback
    assert "az role assignment list --assignee" in rollback
    assert "--all" in rollback
    read_name = re.search(r"(?m)^var readName = '([^']+)'$", BICEP).group(1)
    identity = re.search(
        r"name: '\$\{readName\}(-[^']*)'", _resource_block("readIdentity")
    ).group(1)
    assert f"az identity show -n {read_name}{identity} " in rollback
    assert "--query principalId -o tsv" in rollback
    role_names = set()
    for _scope, role in _READ_WIPE_ASSIGNMENTS.values():
        definition = _resource_block(role.removesuffix(".id"))
        role_names.add(re.search(r"roleName: '([^']+)'", definition).group(1))
    assert role_names == {
        "Wattracker Operator Scope Wipe Blob",
        "Wattracker Operator Scope Wipe Table",
        "Wattracker Scope Wipe Lock",
    }
    for role_name in role_names:
        assert f"roleDefinitionName=='{role_name}'" in rollback, role_name
    assert "must print nothing" in rollback
    assert "Incremental" in rollback
    # The false claim cannot come back.
    flat = " ".join(DEPLOY_RUNBOOK.split())
    assert "That removes the read identity's wipe assignments" not in flat
    # And the template says the same thing where the switch is.
    param_comment = BICEP.split("param enableAccountWipe bool", 1)[0].rsplit(
        "param operatorWipePrincipalId", 1
    )[1]
    assert "does NOT revoke" in param_comment
    assert "infra/azure/DEPLOY.md" in param_comment


def test_the_docs_count_the_read_identitys_wipe_assignments_right():
    assert len(_READ_WIPE_ASSIGNMENTS) == 4
    for text in (DEPLOY_RUNBOOK, RUNBOOK):
        flat = " ".join(text.split())
        assert "three role assignments" not in flat
        assert "three wipe role assignments" not in flat


def _string_like(pattern):
    """ABAC ``StringLike`` as a regex: case-sensitive, ``*`` any run, ``?`` one.

    Per https://learn.microsoft.com/en-us/azure/role-based-access-control/conditions-format#stringlike
    -- a backslash would escape a wildcard; the lock pattern uses none.
    """

    assert "\\" not in pattern
    return re.compile(
        "".join(
            ".*" if ch == "*" else "." if ch == "?" else re.escape(ch)
            for ch in pattern
        ),
        re.DOTALL,
    )


def test_the_lock_role_holds_only_blob_write_and_only_on_the_objects_container():
    actions = _role_definition_actions()
    assert actions["wipeLockRoleDefinition"] == [_BLOB_WRITE]
    block = _resource_block("wipeLockRoleDefinition")
    assert "roleName: 'Wattracker Scope Wipe Lock'" in block
    assert "assignableScopes: [objectContainer.id]" in block
    # #330: no role names a blob-level lease data action; it does not exist.
    for name, role_actions in actions.items():
        assert not any("lease" in action for action in role_actions), name


def test_every_lock_grant_is_confined_to_lock_blob_paths():
    assert _bicep_string_var("wipeLockWriteCondition") == _LOCK_CONDITION
    holders = {
        name
        for name, block in _role_assignment_blocks().items()
        if "wipeLockRoleDefinition.id" in block
    }
    assert holders == set(_LOCK_ASSIGNMENTS)
    for name in _LOCK_ASSIGNMENTS:
        block = _resource_block(name)
        assert "\n  scope: objectContainer\n" in block, name
        assert "    conditionVersion: '2.0'\n" in block, name
        assert "    condition: wipeLockWriteCondition\n" in block, name
    read_lock = _resource_block("readWipeLockRole")
    assert read_lock.startswith(
        "'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableAccountWipe) {"
    )
    assert "principalId: readIdentity.properties.principalId" in read_lock
    operator_lock = _resource_block("operatorWipeLockRole")
    assert "= if (!empty(operatorWipePrincipalId)) {" in operator_lock
    assert "principalId: operatorWipePrincipalId" in operator_lock


def test_the_lock_pattern_matches_the_lock_blob_and_never_rider_data():
    """The storage-side half of the ABAC condition.

    Every blob this package writes has one of two names: the scope lease blob
    (``_lock_blob_name``) or an object blob (``_blob_name``).  The first must
    match the condition's pattern, or wipes 503; the second must never match,
    or the lock grant could overwrite a rider's data.  The object id is the
    only client-supplied part of an object name, and ``_row_key`` limits it to
    a charset with no ``/``, so no object name can end in ``/__lock`` -- and
    every object name ends in ``.json`` anyway.
    """

    import random
    import string

    from wattracker.cloud.storage import WIPE_PROBE_BLOB_STEM, AzureTenantStore

    matcher = _string_like(_LOCK_PATH_PATTERN)
    rng = random.Random(170)
    first = string.ascii_letters + string.digits
    rest = first + "._~-"
    scopes = ["a", "rider", "Z" * 256, "r.i_d~e-r", "lock", "L__lock"]
    scopes += [
        rng.choice(first) + "".join(rng.choice(rest) for _ in range(rng.randint(0, 60)))
        for _ in range(50)
    ]
    object_ids = ["ride-1", "lock", "x.__lock", "a__lock", "L__lock"]
    object_ids += [
        rng.choice(first) + "".join(rng.choice(rest) for _ in range(rng.randint(0, 127)))
        for _ in range(200)
    ]
    for scope in scopes:
        namespace = "".join(rng.choice("0123456789abcdef") for _ in range(64))
        partition = AzureTenantStore._partition(namespace, scope)
        assert matcher.fullmatch(AzureTenantStore._lock_blob_name(partition))
        assert not matcher.fullmatch(f"{partition}/{WIPE_PROBE_BLOB_STEM}0")
        for object_id in object_ids:
            name = AzureTenantStore._blob_name(partition, object_id)
            assert not matcher.fullmatch(name), name
    # An id or scope that could end a name in ``/__lock`` is refused outright.
    for hostile in ("x/__lock", "__lock", "/__lock", "a/../__lock"):
        with pytest.raises(ValueError):
            AzureTenantStore._row_key(hostile)
        with pytest.raises(ValueError):
            AzureTenantStore._partition("a" * 64, hostile)
    # The pattern is not a loose wildcard: the 64 single-character wildcards
    # and the literal separators are doing work.
    assert not matcher.fullmatch("__lock")
    assert not matcher.fullmatch("abc:rider/__lock")
    assert not matcher.fullmatch("a" * 64 + ":rider/object:ride-1.json")
    assert not matcher.fullmatch("a" * 64 + ":rider/__lock.json")


def test_the_read_identity_holds_no_unconditioned_blob_write():
    """Blob write or create reaches the read identity only under the lock condition."""

    actions = _role_definition_actions()
    writes = {_BLOB_WRITE, _BLOB_WRITE.replace("/write", "/add/action")}
    seen = set()
    for name, block in _role_assignment_blocks().items():
        if "readIdentity" not in block:
            continue
        seen.add(name)
        role = re.search(r"roleDefinitionId: (\w+)\.id\n", block)
        if role is None:
            # Built-in roles, pinned below to the two readers.
            assert name in {"tableRole", "readBlobRole"}, name
            continue
        if writes & set(actions[role.group(1)]):
            assert "    condition: wipeLockWriteCondition\n" in block, name
            assert "    conditionVersion: '2.0'\n" in block, name
    assert "readWipeLockRole" in seen
    assert re.search(
        r"(?m)^param blobReaderRoleDefinitionId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'$",
        PARAMS,
    )
    assert "'76199698-9eea-4c19-bc75-cec21354c6b6'" in _resource_block("tableRole")


def test_the_sync_identity_roles_are_unchanged():
    sync = {
        name: (
            re.search(r"roleDefinitionId: (\w+)\.id\n", block).group(1),
            re.search(r"\n  scope: (\w+)\n", block).group(1),
        )
        for name, block in _role_assignment_blocks().items()
        if "syncIdentity" in block
    }
    assert sync == {
        "blobRole": ("syncBlobWriterRoleDefinition", "objectContainer"),
        "syncTableRole": ("syncTableWriterRoleDefinition", "objectTable"),
        "syncAuthRole": ("authReaderRoleDefinition", "authTable"),
        "syncControlRole": ("controlReaderRoleDefinition", "controlTable"),
        "syncReplayRole": ("replayWriterRoleDefinition", "replayTable"),
    }
    for name in sync:
        assert "condition" not in _resource_block(name), name
