targetScope = 'resourceGroup'

@description('Deployment region.')
param location string = resourceGroup().location
@description('Globally unique storage account name; lowercase, 3-24 characters.')
param storageName string
@description('DNS hostname of the external budget-hook Function App, without a scheme, path, or query string; Bicep always constructs HTTPS URLs.')
@minLength(1)
param budgetHookHost string
@description('Name of the external budget-hook Function App in this resource group; Bicep resolves its default host key at deployment time.')
@minLength(1)
param budgetHookFunctionAppName string
@description('Exact allowed PWA origin; wildcard origins are not accepted.')
param allowedOrigin string
@description('Billing alert email.')
param billingEmail string
@description('UTC first day of the monthly budget period, for example 2026-09-01.')
param budgetStartDate string
@description('UTC end date of the monthly budget period, after budgetStartDate.')
param budgetEndDate string
@description('Object ID of the managed identity used by the external budget-hook Function App.')
param budgetHookPrincipalId string
@description('Static Web Apps repository URL; static assets never access Storage.')
param staticRepositoryUrl string = ''
@description('Static Web Apps branch.')
param staticBranch string = 'main'
@secure()
@description('Static Web Apps deployment token, supplied only at deployment time.')
param staticRepositoryToken string = ''
@description('Signed read-plane container image, including the cloud runtime entrypoint.')
param readImage string
@description('Signed sync-plane container image, including the cloud runtime entrypoint.')
param syncImage string
@secure()
@description('Base64-encoded server secret; injected only into the cloud containers.')
param cloudServerSecret string
@secure()
@description('Operator enrollment token; injected only into the cloud containers.')
@minLength(32)
param operatorToken string
@description('Built-in Storage Blob Data Reader role definition ID.')
param blobReaderRoleDefinitionId string
@description('Object ID of the operator principal that runs a scope wipe (#170). Empty deploys the wipe role definitions without assigning them to anybody, which is the default: the capability stays reviewable and nothing holds it until an operator is named. Never a container app identity.')
param operatorWipePrincipalId string = ''
// #170 item 6, owner decision 2026-09-26: the READ app's own identity holds the
// scope-wipe grant, and this one switch controls both that grant and the
// route's WATTRACKER_CLOUD_ALLOW_ACCOUNT_WIPE flag, so the two can never
// diverge -- no route without the grant, no grant without the route.
//
// The accepted risk, stated plainly: the read app is the internet-facing plane,
// and with this on, a compromise of it can delete riders' cloud copies (object
// blobs, their CloudObjects rows, and CloudAuth credential rows). That is
// accepted because rider data in the cloud is a replica -- each rider's
// desktop is the source of truth and can re-sync it. Confidentiality does not
// change: the read identity can already read all of it. What stays out of
// reach is unchanged too: no delete on CloudControl (the kill switch), and the
// sync identity gains nothing and keeps no delete anywhere.
//
// Turning this back to false does NOT revoke grants already made.
// deploy_cloud.py deploys in Incremental mode, where a resource whose condition
// becomes false is skipped, not deleted: the read identity keeps its four wipe
// role assignments and only the route flag goes. infra/azure/DEPLOY.md
// ("Rollback") gives the commands that delete the four assignments.
@description('Owner switch for rider account wipes (#170). True grants the read identity the scope-wipe delete roles on the objects container, CloudObjects and CloudAuth (never CloudControl), plus blobs/write on per-scope lease blobs only (ABAC path condition), and sets WATTRACKER_CLOUD_ALLOW_ACCOUNT_WIPE=1 on the read app only. False (the default) grants nothing, but switching an existing deployment back to false removes only the flag: incremental deploys leave the assignments, which infra/azure/DEPLOY.md says how to delete.')
param enableAccountWipe bool = false

var vnetName = 'wattracker-vnet'
var envName = 'wattracker-aca-env'
var readName = 'wattracker-read'
var syncName = 'wattracker-sync'
var staticName = 'wattracker-pwa'
resource budgetHookApp 'Microsoft.Web/sites@2022-09-01' existing = {
  name: budgetHookFunctionAppName
}
// The route flag rides on the same switch as the grant. Absent when false,
// so the app's own default (off) applies; the sync app never gets it.
var readAccountWipeEnv = enableAccountWipe ? [
  {
    name: 'WATTRACKER_CLOUD_ALLOW_ACCOUNT_WIPE'
    value: '1'
  }
] : []
// #170: the ABAC condition on every assignment of `wipeLockRoleDefinition`.
// It allows the role's one action, blobs/write, only on a blob whose path
// matches the per-scope lease blob `AzureTenantStore._lock_blob_name` builds:
// `<64-hex namespace>:<local scope>/__lock`. 64 single-character wildcards
// (`?`) cover the namespace exactly, then a literal `:`, the scope (`*`),
// and the literal `/__lock`. No rider data blob can match: object blobs are
// always `<partition>/object:<id>.json`, and neither the scope nor the id
// may contain `/` (validated in `storage.py`, pinned by a test).
// Attribute and operator per the ABAC references:
//   https://learn.microsoft.com/en-us/azure/storage/blobs/storage-auth-abac-attributes#blob-path
//     (`blobs:path` is the name inside the container, no container, no leading `/`)
//   https://learn.microsoft.com/en-us/azure/role-based-access-control/conditions-format#stringlike
//     (case-sensitive; `*` any run of characters, `?` exactly one)
// UNVERIFIED LIVE: no Azure command has evaluated this condition. If it is
// wrong, the wipe route's capability probe fails its lease step and refuses
// the wipe with 503, nothing deleted.
var wipeLockWriteCondition = '((!(ActionMatches{\'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write\'})) OR (@Resource[Microsoft.Storage/storageAccounts/blobServices/containers/blobs:path] StringLike \'????????????????????????????????????????????????????????????????:*/__lock\'))'
var budgetHookDefaultKey = listKeys('${budgetHookApp.id}/host/default', '2022-03-01').functionKeys.default
var writeShutdownWebhookUri = 'https://${budgetHookHost}/budget/disable-writes?code=${budgetHookDefaultKey}'
var publicShutdownWebhookUri = 'https://${budgetHookHost}/budget/disable-public-api?code=${budgetHookDefaultKey}'

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: { addressPrefixes: [ '10.42.0.0/16' ] }
  }
}

resource acaSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = {
  parent: vnet
  name: 'aca-infrastructure'
  properties: {
    addressPrefix: '10.42.0.0/23'
    delegations: [
      {
        name: 'aca'
        properties: {
          serviceName: 'Microsoft.App/environments'
        }
      }
    ]
    serviceEndpoints: [
      {
        service: 'Microsoft.Storage'
        locations: [location]
      }
    ]
  }
}

resource budgetHookSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = {
  parent: vnet
  name: 'budget-hook-flex'
  properties: {
    addressPrefix: '10.42.2.0/27'
    delegations: [
      {
        name: 'budget-hook-flex'
        properties: {
          serviceName: 'Microsoft.App/environments'
        }
      }
    ]
    serviceEndpoints: [
      {
        service: 'Microsoft.Storage'
        locations: [location]
      }
    ]
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    publicNetworkAccess: 'Enabled'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      virtualNetworkRules: [
        {
          id: acaSubnet.id
          action: 'Allow'
        }
        {
          id: budgetHookSubnet.id
          action: 'Allow'
        }
      ]
      // Explicit: an omitted ipRules is preserved by Azure on redeploy, leaving stale rules live (#339)
      ipRules: []
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}
resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}
resource objectContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'wattracker-objects'
  properties: { publicAccess: 'None' }
}
resource objectTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'CloudObjects'
  properties: {}
}
resource authTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'CloudAuth'
  properties: {}
}
resource controlTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'CloudControl'
  properties: {}
}
resource replayTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'CloudReplay'
  properties: {}
}

resource readIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${readName}-identity'
  location: location
}
resource syncIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${syncName}-identity'
  location: location
}
resource appEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: envName
  location: location
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: acaSubnet.id
      internal: false
    }
    workloadProfiles: [
      {
        name: 'consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource readApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: readName
  location: location
  // The revision that exposes the wipe route is created only after the
  // read identity's wipe grants exist. When enableAccountWipe is false the
  // four are not deployed and ARM drops them from the dependency list.
  // (RBAC propagation can still lag the assignment by minutes; the route's
  // delete-capability probe answers 503 with nothing deleted until it lands.)
  dependsOn: [
    readWipeBlobRole
    readWipeObjectTableRole
    readWipeAuthTableRole
    readWipeLockRole
  ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${readIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: appEnv.id
    configuration: {
      secrets: [
        {
          name: 'cloud-server-secret'
          value: cloudServerSecret
        }
        {
          name: 'operator-token'
          value: operatorToken
        }
      ]
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8000
        transport: 'http'
        clientCertificateMode: 'Ignore'
      }
    }
    template: {
      containers: [{
        name: 'read'
        image: readImage
        command: ['python']
        args: ['-m', 'wattracker.cloud.runtime']
        env: concat([
          {
            name: 'WATTRACKER_CLOUD_PLANE'
            value: 'read'
          }
          {
            name: 'WATTRACKER_STORAGE_ACCOUNT_NAME'
            value: storage.name
          }
          {
            name: 'AZURE_CLIENT_ID'
            value: readIdentity.properties.clientId
          }
          {
            name: 'WATTRACKER_ALLOWED_ORIGINS'
            value: allowedOrigin
          }
          {
            name: 'WATTRACKER_CLOUD_SERVER_SECRET'
            secretRef: 'cloud-server-secret'
          }
          {
            name: 'WATTRACKER_CLOUD_OPERATOR_TOKEN'
            secretRef: 'operator-token'
          }
        ], readAccountWipeEnv)
        resources: {
          cpu: any('0.5')
          memory: '1Gi'
        }
      }]
      scale: {
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}
resource syncApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: syncName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${syncIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: appEnv.id
    configuration: {
      secrets: [
        {
          name: 'cloud-server-secret'
          value: cloudServerSecret
        }
        {
          name: 'operator-token'
          value: operatorToken
        }
      ]
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8000
        transport: 'http'
        clientCertificateMode: 'Ignore'
      }
    }
    template: {
      containers: [{
        name: 'sync'
        image: syncImage
        command: ['python']
        args: ['-m', 'wattracker.cloud.runtime']
        env: [
          {
            name: 'WATTRACKER_CLOUD_PLANE'
            value: 'sync'
          }
          {
            name: 'WATTRACKER_STORAGE_ACCOUNT_NAME'
            value: storage.name
          }
          {
            name: 'AZURE_CLIENT_ID'
            value: syncIdentity.properties.clientId
          }
          {
            name: 'WATTRACKER_ALLOWED_ORIGINS'
            value: allowedOrigin
          }
          {
            name: 'WATTRACKER_CLOUD_SERVER_SECRET'
            secretRef: 'cloud-server-secret'
          }
          {
            name: 'WATTRACKER_CLOUD_OPERATOR_TOKEN'
            secretRef: 'operator-token'
          }
        ]
        resources: {
          cpu: any('0.5')
          memory: '1Gi'
        }
      }]
      scale: {
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}

resource syncBlobWriterRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'wattracker-sync-blob-writer')
  properties: {
    roleName: 'Wattracker Sync Blob Writer'
    description: 'Read and write sync objects; physical deletion is not granted to the sync identity.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action'
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'
    ] }]
    assignableScopes: [storage.id]
  }
}
resource syncTableWriterRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'wattracker-sync-table-writer')
  properties: {
    roleName: 'Wattracker Sync Table Writer'
    description: 'Read and upsert sync entities; physical deletion is not granted to the sync identity.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/read'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/add/action'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/update/action'
    ] }]
    assignableScopes: [storage.id]
  }
}
resource authReaderRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'wattracker-auth-reader')
  properties: {
    roleName: 'Wattracker Cloud Auth Reader'
    description: 'Read-only access to shared credential and context records.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/read'
    ] }]
    assignableScopes: [storage.id]
  }
}
resource authManagerRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'wattracker-auth-manager')
  properties: {
    roleName: 'Wattracker Cloud Auth Manager'
    description: 'Create and update shared credential and context records.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/read'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/add/action'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/update/action'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/write'
    ] }]
    assignableScopes: [storage.id]
  }
}
// The read plane serves GET /api/v1/devices and POST
// /api/v1/devices/{id}/revoke, so it needs to *write* CloudAuth -- which
// `authManagerRoleDefinition` above already grants it (read, add, update, and
// insert-or-merge write).
// What it did not have, and what the expired-row sweep needs, is a delete.
//
// It is a separate role rather than a fourth action on the manager role, so
// the grant that removes rows is legible on its own, is assignable on its own,
// and is scoped to the CloudAuth table alone -- the same shape
// `replayWriterRoleDefinition` uses for CloudReplay. The sync identity keeps
// `authReaderRoleDefinition` and holds no delete anywhere.
//
// Azure table roles cannot be conditioned on a row key, so this action reaches
// every row in CloudAuth, but cannot reach the budget kill switch in
// CloudControl. What keeps the remaining protected rows safe is in the
// application: `ExpiredRecordSweeper` deletes only the record kinds named in
// `SWEEPABLE_RECORD_KINDS`, only past their own `expires_at`, and refuses at
// construction to be pointed at `NEVER_SWEEP_RECORD_KINDS` -- which names the
// quota counters, neither of which carries an expiry at all.
resource authSweeperRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(authTable.id, 'wattracker-auth-sweeper')
  properties: {
    roleName: 'Wattracker Cloud Auth Sweeper'
    description: 'Delete expired context, invitation, pairing and replay rows in CloudAuth. Quota counter rows carry no expiry and are excluded by record kind in the app; the kill switch is in CloudControl.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/delete'
    ] }]
    assignableScopes: [authTable.id]
  }
}
// #170: deleting one rider's data is a privileged path, not a flag on the sync
// client. The sync identity holds no delete anywhere and does not gain one
// here. These two roles are held by up to two principals, each behind its own
// switch: the operator named by `operatorWipePrincipalId` (#169's CLI), and --
// only when `enableAccountWipe` is true -- the read identity, which serves the
// rider-initiated wipe route (owner decision 2026-09-26; the accepted risk is
// stated at the `enableAccountWipe` parameter).
//
// What they are scoped to is the design. Between them they reach the rider's
// objects -- the blobs in `wattracker-objects` and their rows in
// `CloudObjects` -- and the credential rows in `CloudAuth`. They are NOT
// scoped to `CloudControl`, so the budget kill switch is out of reach of the
// grant itself rather than protected by the wipe being careful: an absent
// kill-switch row reads as ENABLED, and not being able to delete it is better
// than being trusted not to.
//
// `CloudAuth` also holds the daily quota counters on the read plane, and Azure
// table roles cannot be conditioned on a row key, so that grant does reach
// them. `wattracker.cloud.wipe` excludes `quota-counter` by record kind for
// the same reason it excludes `kill-switch`: an absent counter row reads as
// zero, so a wipe that took them would hand the scope a fresh daily budget.
//
// Neither is assigned by default: not to an operator unless
// `operatorWipePrincipalId` names somebody, and not to the read identity unless
// `enableAccountWipe` is true. The default deployment defines the capability
// and gives it to no one, which is the bar
// `test_cleanup_delete_identity_is_not_deployed_without_a_cleanup_job` set: a
// delete grant is held only where something actually deletes.
//
// `purge_scope` also holds the scope's blob lease while it deletes, and
// creating or leasing that lock blob needs blobs/write, which neither of these
// roles carries. That is `wipeLockRoleDefinition` below, assigned alongside
// them to the same principals and always under `wipeLockWriteCondition`.
resource operatorWipeBlobRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(objectContainer.id, 'wattracker-operator-wipe-blob')
  properties: {
    roleName: 'Wattracker Operator Scope Wipe Blob'
    description: 'Read and delete the sync object blobs of one scope during a scope wipe. Held by the named operator principal, and by the read identity only when enableAccountWipe is true; the sync identity keeps no delete.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/delete'
    ] }]
    assignableScopes: [objectContainer.id]
  }
}
resource operatorWipeTableRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'wattracker-operator-wipe-table')
  properties: {
    roleName: 'Wattracker Operator Scope Wipe Table'
    description: 'Read and delete the object and credential rows of one scope during an operator scope wipe. Assignable only to CloudObjects and CloudAuth; the kill switch in CloudControl is out of reach.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/read'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/delete'
    ] }]
    assignableScopes: [objectTable.id, authTable.id]
  }
}
// #170 (owner decision 2026-09-26): the lease `purge_scope` takes on the
// scope's lock blob before it deletes. Per the Blob permissions table, Put
// Blob (create or replace) and Lease Blob both need exactly
// `Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write`;
// nothing further is needed to acquire or release a lease:
//   https://learn.microsoft.com/en-us/rest/api/storageservices/authorize-with-azure-active-directory#permissions-for-blob-service-operations
// There is no blob-level lease data action: `blobs/lease/action` does not
// exist, and naming it failed the first real deployment (#330).
//
// blobs/write on its own would let the holder overwrite any rider's data, so
// this role is never assigned without `wipeLockWriteCondition`, which confines
// the write to lock-blob paths. Assignable only to the objects container.
resource wipeLockRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(objectContainer.id, 'wattracker-scope-wipe-lock')
  properties: {
    roleName: 'Wattracker Scope Wipe Lock'
    description: 'Write only the per-scope lease blob <namespace>:<scope>/__lock that a scope wipe holds while it deletes. Every assignment carries an ABAC condition restricting blobs/write to that path; never rider data.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'
    ] }]
    assignableScopes: [objectContainer.id]
  }
}
resource controlReaderRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'wattracker-control-reader')
  properties: {
    roleName: 'Wattracker Cloud Control Reader'
    description: 'Read-only access to the deployment-wide durable kill-switch row.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/read'
    ] }]
    assignableScopes: [controlTable.id]
  }
}
resource budgetHookRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'wattracker-budget-hook-writer')
  properties: {
    roleName: 'Wattracker Budget Hook Writer'
    description: 'Read and upsert the deployment-wide durable kill-switch row in CloudControl; no delete or other table access.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/read'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/add/action'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/update/action'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/write'
    ] }]
    assignableScopes: [controlTable.id]
  }
}
resource replayWriterRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'wattracker-replay-writer')
  properties: {
    roleName: 'Wattracker Cloud Replay Writer'
    description: 'Create and replace expired nonce replay claims only in CloudReplay.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/read'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/add/action'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/update/action'
      'Microsoft.Storage/storageAccounts/tableServices/tables/entities/write'
    ] }]
    assignableScopes: [replayTable.id]
  }
}
resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, syncIdentity.id, 'blob-writer')
  scope: objectContainer
  properties: {
    roleDefinitionId: syncBlobWriterRoleDefinition.id
    principalId: syncIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource tableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, readIdentity.id, 'table-reader')
  scope: objectTable
  properties: {
    // Storage Table Data Reader built-in role.
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '76199698-9eea-4c19-bc75-cec21354c6b6')
    principalId: readIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource readBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, readIdentity.id, 'blob-reader')
  scope: objectContainer
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobReaderRoleDefinitionId)
    principalId: readIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource syncTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, syncIdentity.id, 'table-contributor')
  scope: objectTable
  properties: {
    roleDefinitionId: syncTableWriterRoleDefinition.id
    principalId: syncIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource readAuthRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(authTable.id, readIdentity.id, 'auth-manager')
  scope: authTable
  properties: {
    roleDefinitionId: authManagerRoleDefinition.id
    principalId: readIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource syncAuthRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(authTable.id, syncIdentity.id, 'auth-reader')
  scope: authTable
  properties: {
    roleDefinitionId: authReaderRoleDefinition.id
    principalId: syncIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource readAuthSweepRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(authTable.id, readIdentity.id, 'auth-sweeper')
  scope: authTable
  properties: {
    roleDefinitionId: authSweeperRoleDefinition.id
    principalId: readIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource readControlRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(controlTable.id, readIdentity.id, 'control-reader')
  scope: controlTable
  properties: {
    roleDefinitionId: controlReaderRoleDefinition.id
    principalId: readIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource syncControlRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(controlTable.id, syncIdentity.id, 'control-reader')
  scope: controlTable
  properties: {
    roleDefinitionId: controlReaderRoleDefinition.id
    principalId: syncIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource syncReplayRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(replayTable.id, syncIdentity.id, 'replay-writer')
  scope: replayTable
  properties: {
    roleDefinitionId: replayWriterRoleDefinition.id
    principalId: syncIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
// No `principalType` on these three. Every other assignment in this template
// names a managed identity and says 'ServicePrincipal'; the wipe principal is
// whoever the owner decides runs #169's operator CLI, which may be a user
// account. Pinning the type would refuse that, and pinning the wrong one
// fails the deployment rather than the wipe.
resource operatorWipeBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorWipePrincipalId)) {
  name: guid(objectContainer.id, operatorWipePrincipalId, 'operator-wipe-blob')
  scope: objectContainer
  properties: {
    roleDefinitionId: operatorWipeBlobRoleDefinition.id
    principalId: operatorWipePrincipalId
  }
}
resource operatorWipeObjectTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorWipePrincipalId)) {
  name: guid(objectTable.id, operatorWipePrincipalId, 'operator-wipe-object-table')
  scope: objectTable
  properties: {
    roleDefinitionId: operatorWipeTableRoleDefinition.id
    principalId: operatorWipePrincipalId
  }
}
resource operatorWipeAuthTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorWipePrincipalId)) {
  name: guid(authTable.id, operatorWipePrincipalId, 'operator-wipe-auth-table')
  scope: authTable
  properties: {
    roleDefinitionId: operatorWipeTableRoleDefinition.id
    principalId: operatorWipePrincipalId
  }
}
// The read identity's copy of the same three grants, behind `enableAccountWipe`.
// Distinct names (the read identity's id and a 'read-account-wipe-*' salt), so
// they never collide with the operator assignments above, which are unchanged.
// Same roles, same three scopes -- the objects container, CloudObjects and
// CloudAuth -- and never CloudControl.
resource readWipeBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableAccountWipe) {
  name: guid(objectContainer.id, readIdentity.id, 'read-account-wipe-blob')
  scope: objectContainer
  properties: {
    roleDefinitionId: operatorWipeBlobRoleDefinition.id
    principalId: readIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource readWipeObjectTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableAccountWipe) {
  name: guid(objectTable.id, readIdentity.id, 'read-account-wipe-object-table')
  scope: objectTable
  properties: {
    roleDefinitionId: operatorWipeTableRoleDefinition.id
    principalId: readIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource readWipeAuthTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableAccountWipe) {
  name: guid(authTable.id, readIdentity.id, 'read-account-wipe-auth-table')
  scope: authTable
  properties: {
    roleDefinitionId: operatorWipeTableRoleDefinition.id
    principalId: readIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
// The lease grant, to the same two principals behind the same two switches,
// and only ever with `wipeLockWriteCondition`: write on lock blobs, nothing
// else. The operator gets it too, so #169's CLI does not hit the same 403.
resource operatorWipeLockRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorWipePrincipalId)) {
  name: guid(objectContainer.id, operatorWipePrincipalId, 'operator-wipe-lock')
  scope: objectContainer
  properties: {
    roleDefinitionId: wipeLockRoleDefinition.id
    principalId: operatorWipePrincipalId
    conditionVersion: '2.0'
    condition: wipeLockWriteCondition
  }
}
resource readWipeLockRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableAccountWipe) {
  name: guid(objectContainer.id, readIdentity.id, 'read-account-wipe-lock')
  scope: objectContainer
  properties: {
    roleDefinitionId: wipeLockRoleDefinition.id
    principalId: readIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    conditionVersion: '2.0'
    condition: wipeLockWriteCondition
  }
}
resource budgetHookRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(controlTable.id, budgetHookPrincipalId, 'budget-hook-writer')
  scope: controlTable
  properties: {
    roleDefinitionId: budgetHookRoleDefinition.id
    principalId: budgetHookPrincipalId
    principalType: 'ServicePrincipal'
  }
}
resource staticSite 'Microsoft.Web/staticSites@2022-09-01' = if (!empty(staticRepositoryUrl)) {
  name: staticName
  location: location
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    repositoryUrl: staticRepositoryUrl
    branch: staticBranch
    repositoryToken: staticRepositoryToken
    stagingEnvironmentPolicy: 'Enabled'
    buildProperties: {
      skipGithubActionWorkflowGeneration: true
    }
  }
}

resource writeShutdownActionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'wattracker-write-shutdown'
  location: 'Global'
  properties: {
    groupShortName: 'wtrstop'
    enabled: true
    webhookReceivers: [
      {
        name: 'budget-hook-write'
        serviceUri: writeShutdownWebhookUri
        useCommonAlertSchema: true
      }
    ]
  }
}
resource publicShutdownActionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'wattracker-public-shutdown'
  location: 'Global'
  properties: {
    groupShortName: 'wtrpub'
    enabled: true
    webhookReceivers: [
      {
        name: 'budget-hook-public'
        serviceUri: publicShutdownWebhookUri
        useCommonAlertSchema: true
      }
    ]
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-05-01' = {
  name: 'wattracker-monthly-budget'
  properties: {
    amount: 10
    category: 'Cost'
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
      endDate: budgetEndDate
    }
    notifications: {
      actual50: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: [billingEmail]
      }
      actual80: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [billingEmail]
        contactGroups: [writeShutdownActionGroup.id]
      }
      actual100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: [billingEmail]
        contactGroups: [publicShutdownActionGroup.id]
      }
    }
  }
}
