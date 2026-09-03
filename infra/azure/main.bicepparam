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
// Source: az query (all possible Function App outbound IPv4 addresses, as an array).
param budgetHookIpRules = [
  'TODO_BUDGET_HOOK_OUTBOUND_IPV4'
]
// Source: owner decision (empty honestly disables the optional Static Web App).
param staticRepositoryUrl = ''
// Source: owner decision (template-safe default for an enabled optional Static Web App).
param staticBranch = 'main'
// Source: owner decision (set WATTRACKER_STATIC_REPOSITORY_TOKEN outside source control; empty is valid when staticRepositoryUrl is empty).
param staticRepositoryToken = readEnvironmentVariable('WATTRACKER_STATIC_REPOSITORY_TOKEN', '')
// Source: #217 image output (TODO immutable signed read-plane image reference; #217 did not push an image).
param readImage = 'TODO_SIGNED_IMMUTABLE_READ_IMAGE_FROM_217_OUTPUT'
// Source: #217 image output (TODO immutable signed sync-plane image reference; #217 did not push an image).
param syncImage = 'TODO_SIGNED_IMMUTABLE_SYNC_IMAGE_FROM_217_OUTPUT'
// Source: owner decision (set WATTRACKER_CLOUD_SERVER_SECRET outside source control to base64-encoded 256-bit material).
param cloudServerSecret = readEnvironmentVariable('WATTRACKER_CLOUD_SERVER_SECRET')
// Source: owner decision (set WATTRACKER_OPERATOR_TOKEN outside source control; template requires at least 32 characters).
param operatorToken = readEnvironmentVariable('WATTRACKER_OPERATOR_TOKEN')
// Source: Azure built-in role definition lookup (Storage Blob Data Reader; not tenant-specific).
param blobReaderRoleDefinitionId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
