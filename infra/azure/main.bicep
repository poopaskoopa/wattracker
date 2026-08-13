targetScope = 'resourceGroup'

@description('Deployment region.')
param location string = resourceGroup().location
@description('Globally unique storage account name; lowercase, 3-24 characters.')
param storageName string
@description('Set false to return 503 from the public API during an incident.')
param publicApiEnabled bool = true
@description('Set false to reject new sync writes while reads remain available.')
param writesEnabled bool = true
@description('Directory tenant allowed to issue enrollment/pairing tokens.')
param tenantId string
@description('Key Vault name containing the APIM certificate. The secret value is never in this file.')
param apimKeyVaultName string
@description('Resource-group name containing the APIM Key Vault.')
param apimKeyVaultResourceGroup string = resourceGroup().name
@description('Full Key Vault secret URI for the APIM certificate version.')
param apimCertificateSecretUri string
@description('Exact APIM hostname covered by the Key Vault certificate.')
param apimHostName string
@secure()
@description('Authenticated automation endpoint that disables APIM at cost thresholds.')
param writeShutdownWebhookUri string
@secure()
@description('Authenticated automation endpoint that disables all public APIM access.')
param publicShutdownWebhookUri string
@description('APIM publisher email.')
param publisherEmail string
@description('Exact allowed PWA origin; wildcard origins are not accepted.')
param allowedOrigin string
@description('Entra API audience/client ID.')
param apiAudience string
@description('APIM-installed client certificate ID used for outbound Container App mTLS.')
param apimBackendCertificateId string
@description('Billing alert email.')
param billingEmail string
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
param operatorToken string
@secure()
@description('Shared APIM-to-container proof value; never exposed to clients.')
param apimProofSecret string
@description('Built-in Storage Blob Data Reader role definition ID.')
param blobReaderRoleDefinitionId string
@description('Built-in Storage Table Data Contributor role definition ID.')
param tableContributorRoleDefinitionId string

var vnetName = 'wattracker-vnet'
var envName = 'wattracker-aca-env'
var apimName = 'wattracker-apim'
var readName = 'wattracker-read'
var syncName = 'wattracker-sync'
var staticName = 'wattracker-pwa'
var blobZone = 'privatelink.blob.core.windows.net'
var tableZone = 'privatelink.table.core.windows.net'

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: { addressPrefixes: [ '10.42.0.0/16' ] }
    subnets: [
      { name: 'aca-infrastructure'; properties: { addressPrefix: '10.42.0.0/23'; delegations: [{ name: 'aca'; properties: { serviceName: 'Microsoft.App/environments' } }] } }
      { name: 'private-endpoints'; properties: { addressPrefix: '10.42.2.0/24'; privateEndpointNetworkPolicies: 'Disabled' } }
    ]
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    publicNetworkAccess: 'Disabled'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    networkAcls: { defaultAction: 'Deny'; bypass: 'None' }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: { deleteRetentionPolicy: { enabled: true; days: 7 } }
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

resource blobZoneResource 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: blobZone
  location: 'global'
}
resource tableZoneResource 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: tableZone
  location: 'global'
}
resource blobDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: blobZoneResource
  name: 'wattracker-vnet-link'
  properties: { virtualNetwork: { id: vnet.id }; registrationEnabled: false }
}
resource tableDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: tableZoneResource
  name: 'wattracker-vnet-link'
  properties: { virtualNetwork: { id: vnet.id }; registrationEnabled: false }
}

resource blobEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: '${storageName}-blob-pe'
  location: location
  properties: {
    subnet: { id: resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'private-endpoints') }
    privateLinkServiceConnections: [{ name: 'blob'; properties: { privateLinkServiceId: storage.id; groupIds: ['blob'] } }]
  }
}
resource tableEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: '${storageName}-table-pe'
  location: location
  properties: {
    subnet: { id: resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'private-endpoints') }
    privateLinkServiceConnections: [{ name: 'table'; properties: { privateLinkServiceId: storage.id; groupIds: ['table'] } }]
  }
}
resource blobZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: blobEndpoint
  name: 'default'
  properties: { privateDnsZoneConfigs: [{ name: 'blob'; properties: { privateDnsZoneId: blobZoneResource.id } }] }
}
resource tableZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: tableEndpoint
  name: 'default'
  properties: { privateDnsZoneConfigs: [{ name: 'table'; properties: { privateDnsZoneId: tableZoneResource.id } }] }
}

resource readIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = { name: '${readName}-identity'; location: location }
resource syncIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = { name: '${syncName}-identity'; location: location }
resource cleanupIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = { name: 'wattracker-cleanup-identity'; location: location }
resource appEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: envName
  location: location
  properties: {
    vnetConfiguration: { infrastructureSubnetId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'aca-infrastructure'); internal: false }
    workloadProfiles: [{ name: 'consumption'; workloadProfileType: 'Consumption' }]
  }
}

resource readApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: readName
  location: location
  identity: { type: 'UserAssigned'; userAssignedIdentities: { '${readIdentity.id}': {} } }
  properties: {
    managedEnvironmentId: appEnv.id
    configuration: {
      secrets: [
        { name: 'cloud-server-secret'; value: cloudServerSecret }
        { name: 'operator-token'; value: operatorToken }
        { name: 'apim-proof-secret'; value: apimProofSecret }
      ]
      ingress: { external: true; targetPort: 8000; transport: 'http'; clientCertificateMode: 'Require' }
    }
    template: {
      containers: [{
        name: 'read'
        image: readImage
        command: ['python']
        args: ['-m', 'wattracker.cloud.runtime']
        env: [
          { name: 'WATTRACKER_CLOUD_PLANE'; value: 'read' }
          { name: 'WATTRACKER_STORAGE_ACCOUNT_NAME'; value: storage.name }
          { name: 'WATTRACKER_ALLOWED_ORIGINS'; value: allowedOrigin }
          { name: 'WATTRACKER_CLOUD_SERVER_SECRET'; secretRef: 'cloud-server-secret' }
          { name: 'WATTRACKER_CLOUD_OPERATOR_TOKEN'; secretRef: 'operator-token' }
          { name: 'WATTRACKER_APIM_PROOF_VALUE'; secretRef: 'apim-proof-secret' }
        ]
        resources: { cpu: 0.5; memory: '1Gi' }
      }]
      scale: { minReplicas: 0; maxReplicas: 1 }
    }
  }
}
resource syncApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: syncName
  location: location
  identity: { type: 'UserAssigned'; userAssignedIdentities: { '${syncIdentity.id}': {} } }
  properties: {
    managedEnvironmentId: appEnv.id
    configuration: {
      secrets: [
        { name: 'cloud-server-secret'; value: cloudServerSecret }
        { name: 'operator-token'; value: operatorToken }
        { name: 'apim-proof-secret'; value: apimProofSecret }
      ]
      ingress: { external: true; targetPort: 8000; transport: 'http'; clientCertificateMode: 'Require' }
    }
    template: {
      containers: [{
        name: 'sync'
        image: syncImage
        command: ['python']
        args: ['-m', 'wattracker.cloud.runtime']
        env: [
          { name: 'WATTRACKER_CLOUD_PLANE'; value: 'sync' }
          { name: 'WATTRACKER_STORAGE_ACCOUNT_NAME'; value: storage.name }
          { name: 'WATTRACKER_ALLOWED_ORIGINS'; value: allowedOrigin }
          { name: 'WATTRACKER_CLOUD_SERVER_SECRET'; secretRef: 'cloud-server-secret' }
          { name: 'WATTRACKER_CLOUD_OPERATOR_TOKEN'; secretRef: 'operator-token' }
          { name: 'WATTRACKER_APIM_PROOF_VALUE'; secretRef: 'apim-proof-secret' }
        ]
        resources: { cpu: 0.5; memory: '1Gi' }
      }]
      scale: { minReplicas: 0; maxReplicas: 1 }
    }
  }
}

resource syncBlobWriterRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'wattracker-sync-blob-writer')
  properties: {
    roleName: 'Wattracker Sync Blob Writer'
    description: 'Read and write sync objects; physical deletion is cleanup-only.'
    type: 'CustomRole'
    permissions: [{ dataActions: [
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action'
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/lease/action'
    ] }]
    assignableScopes: [storage.id]
  }
}
resource syncTableWriterRoleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'wattracker-sync-table-writer')
  properties: {
    roleName: 'Wattracker Sync Table Writer'
    description: 'Read and upsert sync entities; physical deletion is cleanup-only.'
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
    ] }]
    assignableScopes: [storage.id]
  }
}
resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, syncIdentity.id, 'blob-writer')
  scope: objectContainer
  properties: { roleDefinitionId: syncBlobWriterRoleDefinition.id; principalId: syncIdentity.properties.principalId; principalType: 'ServicePrincipal' }
}
resource tableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, readIdentity.id, 'table-reader')
  scope: objectTable
  properties: { roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '76199698-9eea-4c19-bc75-cec2138a0c8f'); principalId: readIdentity.properties.principalId; principalType: 'ServicePrincipal' }
}
resource readBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, readIdentity.id, 'blob-reader')
  scope: objectContainer
  properties: { roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobReaderRoleDefinitionId); principalId: readIdentity.properties.principalId; principalType: 'ServicePrincipal' }
}
resource syncTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, syncIdentity.id, 'table-contributor')
  scope: objectTable
  properties: { roleDefinitionId: syncTableWriterRoleDefinition.id; principalId: syncIdentity.properties.principalId; principalType: 'ServicePrincipal' }
}
resource readAuthRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(authTable.id, readIdentity.id, 'auth-manager')
  scope: authTable
  properties: { roleDefinitionId: authManagerRoleDefinition.id; principalId: readIdentity.properties.principalId; principalType: 'ServicePrincipal' }
}
resource syncAuthRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(authTable.id, syncIdentity.id, 'auth-reader')
  scope: authTable
  properties: { roleDefinitionId: authReaderRoleDefinition.id; principalId: syncIdentity.properties.principalId; principalType: 'ServicePrincipal' }
}
resource cleanupBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, cleanupIdentity.id, 'blob-cleanup')
  scope: objectContainer
  properties: { roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'); principalId: cleanupIdentity.properties.principalId; principalType: 'ServicePrincipal' }
}
resource cleanupTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, cleanupIdentity.id, 'table-cleanup')
  scope: objectTable
  properties: { roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', tableContributorRoleDefinitionId); principalId: cleanupIdentity.properties.principalId; principalType: 'ServicePrincipal' }
}

resource staticSite 'Microsoft.Web/staticSites@2022-09-01' = if (!empty(staticRepositoryUrl)) {
  name: staticName
  location: location
  sku: { name: 'Free'; tier: 'Free' }
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

resource apimKeyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: apimKeyVaultName
  scope: resourceGroup(subscription().subscriptionId, apimKeyVaultResourceGroup)
}

resource apim 'Microsoft.ApiManagement/service@2023-05-01-preview' = {
  name: apimName
  location: location
  sku: { name: 'Consumption'; capacity: 0 }
  identity: { type: 'SystemAssigned' }
  properties: { publisherEmail: publisherEmail; publisherName: 'wattracker'; virtualNetworkType: 'None'; publicNetworkAccess: 'Enabled'; hostnameConfigurations: [{ type: 'Proxy'; hostName: apimHostName; certificateSource: 'KeyVault'; keyVaultId: apimCertificateSecretUri }] }
}
resource apimKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apimKeyVault.id, apim.id, 'key-vault-secrets-user')
  scope: apimKeyVault
  properties: { roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6'); principalId: apim.identity.principalId; principalType: 'ServicePrincipal' }
}
resource api 'Microsoft.ApiManagement/service/apis@2023-05-01-preview' = {
  parent: apim
  name: 'cloud-sync'
  properties: { displayName: 'Cloud sync'; path: 'api/v1'; protocols: ['https']; subscriptionRequired: true; serviceUrl: 'https://${readApp.properties.configuration.ingress.fqdn}/api/v1' }
}
resource product 'Microsoft.ApiManagement/service/products@2023-05-01-preview' = {
  parent: apim
  name: 'sync-client'
  properties: { displayName: 'Sync client'; subscriptionRequired: true; approvalRequired: true; state: 'published'; subscriptionsLimit: 1 }
}
resource productApi 'Microsoft.ApiManagement/service/products/apis@2023-05-01-preview' = { parent: product; name: api.name; properties: {} }
var readOperations = [
  { name: 'context'; method: 'GET'; urlTemplate: '/context' }
  { name: 'calendar'; method: 'GET'; urlTemplate: '/context/calendar' }
  { name: 'activities'; method: 'GET'; urlTemplate: '/context/activities' }
  { name: 'activity-detail'; method: 'GET'; urlTemplate: '/context/activities/{id}' }
  { name: 'races'; method: 'GET'; urlTemplate: '/context/races' }
  { name: 'enrollment-start'; method: 'POST'; urlTemplate: '/enrollment/start' }
  { name: 'enrollment-complete'; method: 'POST'; urlTemplate: '/enrollment/complete' }
]
resource readOperationResources 'Microsoft.ApiManagement/service/apis/operations@2023-05-01-preview' = [for operation in readOperations: {
  parent: api
  name: operation.name
  properties: {
    displayName: operation.name
    method: operation.method
    urlTemplate: operation.urlTemplate
    request: { queryParameters: []; headers: []; representations: [] }
    responses: [{ statusCode: 200; description: 'Success' }]
  }
}]
resource writesNamedValue 'Microsoft.ApiManagement/service/namedValues@2023-05-01-preview' = {
  parent: apim
  name: 'writes-enabled'
  properties: { displayName: 'writes-enabled'; value: string(writesEnabled); secret: false }
}
resource apimProofNamedValue 'Microsoft.ApiManagement/service/namedValues@2023-05-01-preview' = {
  parent: apim
  name: 'apim-proof-secret'
  properties: { displayName: 'apim-proof-secret'; value: apimProofSecret; secret: true }
}
resource publicNamedValue 'Microsoft.ApiManagement/service/namedValues@2023-05-01-preview' = {
  parent: apim
  name: 'public-api-enabled'
  properties: { displayName: 'public-api-enabled'; value: string(publicApiEnabled); secret: false }
}

resource syncBatchOperation 'Microsoft.ApiManagement/service/apis/operations@2023-05-01-preview' = {
  parent: api
  name: 'sync-batch'
  properties: {
    displayName: 'Upload sync batch'
    method: 'POST'
    urlTemplate: '/sync/batches'
    request: { queryParameters: []; headers: []; representations: [{ contentType: 'application/json' }] }
    responses: [{ statusCode: 200; description: 'Accepted' }]
  }
}
resource syncStatusOperation 'Microsoft.ApiManagement/service/apis/operations@2023-05-01-preview' = {
  parent: api
  name: 'sync-status'
  properties: {
    displayName: 'Sync status'
    method: 'GET'
    urlTemplate: '/sync/status'
    request: { queryParameters: []; headers: []; representations: [] }
    responses: [{ statusCode: 200; description: 'Status' }]
  }
}
resource syncBatchPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2023-05-01-preview' = {
  parent: syncBatchOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: '''
<policies>
  <inbound>
    <base />
    <set-backend-service base-url="https://${syncApp.properties.configuration.ingress.fqdn}/api/v1" />
    <validate-client-certificate validate-revocation="true" />
    <choose>
      <when condition="@('{{writes-enabled}}' != 'true')">
        <return-response><set-status code="403" reason="writes disabled" /></return-response>
      </when>
    </choose>
  </inbound>
  <backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error>
</policies>
'''
  }
}
resource syncStatusPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2023-05-01-preview' = {
  parent: syncStatusOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: '''
<policies>
  <inbound>
    <base />
    <set-backend-service base-url="https://${syncApp.properties.configuration.ingress.fqdn}/api/v1" />
  </inbound>
  <backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error>
</policies>
'''
  }
}
resource corsPolicy 'Microsoft.ApiManagement/service/apis/policies@2023-05-01-preview' = {
  parent: api
  name: 'policy'
  properties: { format: 'rawxml'; value: '''
<policies>
  <inbound>
    <base />
    <choose>
      <when condition="@('{{public-api-enabled}}' != 'true')">
        <return-response><set-status code="503" reason="public API disabled" /></return-response>
      </when>
      <when condition="@(context.Request.OriginalUrl.Path.Contains('/sync/'))">
        <validate-client-certificate validate-revocation="true" />
        <set-header name="X-APIM-Client-Certificate-Verified" exists-action="override"><value>true</value></set-header>
        <set-header name="X-APIM-Request-Proof" exists-action="override"><value>{{apim-proof-secret}}</value></set-header>
        <set-header name="X-APIM-Request-Verified" exists-action="override"><value>true</value></set-header>
      </when>
      <otherwise>
        <validate-jwt header-name="Authorization" require-scheme="Bearer" failed-validation-httpcode="404" failed-validation-error-message="not found">
          <openid-config url="https://login.microsoftonline.com/${tenantId}/v2.0/.well-known/openid-configuration" />
          <audiences><audience>${apiAudience}</audience></audiences>
        </validate-jwt>
        <set-header name="X-Verified-Entra-Subject" exists-action="override"><value>@(context.Request.Headers.GetValueOrDefault("Authorization", "").AsJwt()?.Claims.GetValueOrDefault("sub", "") ?? "")</value></set-header>
        <set-header name="X-APIM-Client-Certificate-Verified" exists-action="override"><value>true</value></set-header>
        <set-header name="X-APIM-Request-Proof" exists-action="override"><value>{{apim-proof-secret}}</value></set-header>
        <set-header name="X-APIM-Request-Verified" exists-action="override"><value>true</value></set-header>
      </otherwise>
    </choose>
    <authentication-certificate certificate-id="${apimBackendCertificateId}" />
    <cors allow-credentials="false">
      <allowed-origins><origin>${allowedOrigin}</origin></allowed-origins>
      <allowed-methods><method>GET</method><method>HEAD</method><method>POST</method></allowed-methods>
      <allowed-headers><header>authorization</header><header>content-type</header><header>ocp-apim-subscription-key</header><header>x-writer-credential</header><header>x-writer-timestamp</header><header>x-writer-nonce</header><header>x-writer-idempotency-key</header><header>x-writer-revision</header><header>x-writer-signature</header></allowed-headers>
    </cors>
    <rate-limit calls="100" renewal-period="1" />
    <rate-limit-by-key calls="60" renewal-period="60" counter-key="@(context.Subscription?.Id ?? context.Request.IpAddress)" />
    <quota-by-key calls="1000" renewal-period="86400" counter-key="@(context.Subscription?.Id ?? context.Request.IpAddress)" />
  </inbound>
  <backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error>
</policies>
''' }
}

resource writeShutdownActionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'wattracker-write-shutdown'
  location: 'Global'
  properties: {
    groupShortName: 'wtrstop'
    enabled: true
    webhookReceivers: [{ name: 'write-shutdown'; serviceUri: writeShutdownWebhookUri; useCommonAlertSchema: true }]
  }
}
resource publicShutdownActionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'wattracker-public-shutdown'
  location: 'Global'
  properties: {
    groupShortName: 'wtrpub'
    enabled: true
    webhookReceivers: [{ name: 'public-shutdown'; serviceUri: publicShutdownWebhookUri; useCommonAlertSchema: true }]
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-05-01' = {
  name: 'wattracker-monthly-budget'
  properties: { amount: 50; timeGrain: 'Monthly'; timePeriod: { startDate: '2026-01-01'; endDate: '2036-01-01' }; notifications: { actual50: { enabled: true; operator: 'GreaterThan'; threshold: 50; thresholdType: 'Actual'; contactEmails: [billingEmail] }; actual80: { enabled: true; operator: 'GreaterThan'; threshold: 80; thresholdType: 'Actual'; contactEmails: [billingEmail]; contactGroups: [writeShutdownActionGroup.id] }; actual100: { enabled: true; operator: 'GreaterThan'; threshold: 100; thresholdType: 'Actual'; contactEmails: [billingEmail]; contactGroups: [publicShutdownActionGroup.id] } } }
}
