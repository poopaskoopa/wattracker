// DEPLOYMENT SKELETON ONLY: replace every TODO before deployment.
// This file deliberately contains no tenant values or literal secrets.
using './main.bicep'

// Source: resource-group/portal (or the target resource group's location).
param location = 'TODO_RESOURCE_GROUP_LOCATION'
// Source: owner decision (globally unique lowercase storage-account name).
param storageName = 'TODO_STORAGE_NAME'
// Source: Function App output (defaultHostName, with no scheme, path, or query).
param budgetHookHost = 'TODO_BUDGET_HOOK_HOST'
// Source: Function App output (existing Function App resource name).
param budgetHookFunctionAppName = 'TODO_BUDGET_HOOK_FUNCTION_APP_NAME'
// Source: owner decision (the exact PWA HTTPS origin; no wildcard).
param allowedOrigin = 'TODO_ALLOWED_ORIGIN'
// Source: owner decision (billing-alert recipient).
param billingEmail = 'TODO_BILLING_EMAIL'
// Source: owner decision (UTC first day of the selected budget period).
param budgetStartDate = 'TODO_BUDGET_START_DATE'
// Source: owner decision (UTC end date after budgetStartDate).
param budgetEndDate = 'TODO_BUDGET_END_DATE'
// Source: Function App output (system-assigned identity principalId).
param budgetHookPrincipalId = 'TODO_BUDGET_HOOK_PRINCIPAL_ID'
// Source: owner decision (empty honestly disables the optional Static Web App).
param staticRepositoryUrl = ''
// Source: owner decision (template-safe default for an enabled optional Static Web App).
param staticBranch = 'main'
// Source: owner decision (set WATTRACKER_STATIC_REPOSITORY_TOKEN outside source control; empty is valid when staticRepositoryUrl is empty).
param staticRepositoryToken = readEnvironmentVariable('WATTRACKER_STATIC_REPOSITORY_TOKEN', '')
// Source: successful #316 `.github/workflows/cloud-publish.yml` run for the deployment commit; copy its full GHCR @sha256 digest (same value used for syncImage).
param readImage = 'TODO_SIGNED_IMMUTABLE_READ_IMAGE_FROM_217_OUTPUT'
// Source: same successful #316 workflow run and digest as readImage; both planes use one image and differ only by WATTRACKER_CLOUD_PLANE.
param syncImage = 'TODO_SIGNED_IMMUTABLE_SYNC_IMAGE_FROM_217_OUTPUT'
// Source: owner decision (set WATTRACKER_CLOUD_SERVER_SECRET outside source control to base64-encoded 256-bit material).
param cloudServerSecret = readEnvironmentVariable('WATTRACKER_CLOUD_SERVER_SECRET')
// Source: owner decision (set WATTRACKER_OPERATOR_TOKEN outside source control; template requires at least 32 characters).
param operatorToken = readEnvironmentVariable('WATTRACKER_OPERATOR_TOKEN')
// Source: Azure built-in role definition lookup (Storage Blob Data Reader; not tenant-specific).
param blobReaderRoleDefinitionId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
// Source: owner decision (object ID of the operator principal allowed to run a scope wipe; empty assigns the wipe roles to nobody, which is the intended default until #169 exists).
param operatorWipePrincipalId = ''
// Source: owner decision (TODO: set true in your untracked main.local.bicepparam to let riders wipe their own cloud scope; one switch grants the read identity the scope-wipe deletes AND turns the route on; false is the safe default; switching back to false removes only the route flag, not the grants, so follow the Rollback steps in DEPLOY.md).
param enableAccountWipe = false
