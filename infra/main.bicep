// Cortex-Jira Sync Azure Function infrastructure
// Deploys: Storage Account, Application Insights, Key Vault, Function App (Consumption)

@description('Base name for all resources (lowercase, no special chars, max 18 chars)')
@maxLength(18)
param baseName string

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Cortex XSIAM base URL (e.g. https://api-yourorg.xdr.us.paloaltonetworks.com)')
param cortexBaseUrl string

@secure()
@description('Cortex API key')
param cortexApiKey string

@description('Cortex API key ID')
param cortexApiKeyId string

@description('Cortex console URL for deep links (e.g. https://yourorg.xdr.us.paloaltonetworks.com)')
param cortexConsoleUrl string = ''

@description('Cortex case domain to sync')
param cortexCaseDomain string = 'security'

@description('Jira site URL (e.g. https://yourorg.atlassian.net)')
param jiraSiteUrl string = ''

@description('Jira Cloud ID (alternative to site URL)')
param jiraCloudId string = ''

@description('Jira service account email')
param jiraEmail string

@secure()
@description('Jira API token')
param jiraApiToken string

@description('Jira project key')
param jiraProjectKey string

@description('Jira issue type name')
param jiraIssueType string = 'Alert'

@description('Jira custom field ID for Cortex case ID')
param jiraCaseIdField string = ''

@description('Jira custom field ID for Cortex issue ID')
param jiraIssueIdField string = ''

@description('Jira custom field ID for XDR URL')
param jiraXdrUrlField string = ''

@description('JSON map of Jira status -> Cortex resolve reason')
param resolutionTypeMap string = '{"False Positive": "Resolved - False Positive", "Duplicate": "Resolved - Duplicate Case", "Known Issue": "Resolved - Known Issue", "Security Testing": "Resolved - Security Testing", "TP Malicious": "Resolved - TP Malicious", "TP Benign": "Resolved - TP Benign", "SPAM": "Resolved - SPAM or Marketing"}'

@description('Default Cortex resolution type')
param defaultResolutionType string = 'Resolved - Other'

@description('Max cases to sync per cycle (0 = unlimited)')
param maxSyncCases string = '0'

@description('Enable standalone issue sync')
param syncIssues string = 'false'

@description('Only sync cases updated after this date (ISO format, e.g. 2026-01-01)')
param syncFromDate string = ''

// --- Resource names ---
var storageName = toLower(replace('${baseName}store', '-', ''))
var functionAppName = '${baseName}-func'
var appInsightsName = '${baseName}-insights'
var keyVaultName = '${baseName}-kv'
var hostingPlanName = '${baseName}-plan'
var stateContainerName = 'cortex-jira-sync'

// --- Storage Account ---
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource stateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: stateContainerName
}

// --- Application Insights ---
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    Request_Source: 'rest'
  }
}

// --- Key Vault ---
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource cortexApiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'cortex-api-key'
  properties: { value: cortexApiKey }
}

resource jiraApiTokenSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'jira-api-token'
  properties: { value: jiraApiToken }
}

// --- Consumption Plan ---
resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: hostingPlanName
  location: location
  sku: { name: 'Y1', tier: 'Dynamic' }
  kind: 'functionapp'
  properties: { reserved: true }  // Linux
}

// --- Function App ---
resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    siteConfig: {
      pythonVersion: '3.11'
      linuxFxVersion: 'PYTHON|3.11'
      appSettings: [
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'AzureWebJobsFeatureFlags', value: 'EnableWorkerIndexing' }
        { name: 'AzureWebJobsStorage', value: 'DefaultEndpointsProtocol=https;AccountName=${storageName};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storageAccount.listKeys().keys[0].value}' }
        { name: 'AZURE_STORAGE_CONNECTION_STRING', value: 'DefaultEndpointsProtocol=https;AccountName=${storageName};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storageAccount.listKeys().keys[0].value}' }
        { name: 'STATE_CONTAINER_NAME', value: stateContainerName }
        { name: 'APPINSIGHTS_INSTRUMENTATIONKEY', value: appInsights.properties.InstrumentationKey }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'CORTEX_BASE_URL', value: cortexBaseUrl }
        { name: 'CORTEX_API_KEY', value: '@Microsoft.KeyVault(SecretUri=${cortexApiKeySecret.properties.secretUri})' }
        { name: 'CORTEX_API_KEY_ID', value: cortexApiKeyId }
        { name: 'CORTEX_CONSOLE_URL', value: cortexConsoleUrl }
        { name: 'CORTEX_CASE_DOMAIN', value: cortexCaseDomain }
        { name: 'JIRA_SITE_URL', value: jiraSiteUrl }
        { name: 'JIRA_CLOUD_ID', value: jiraCloudId }
        { name: 'JIRA_EMAIL', value: jiraEmail }
        { name: 'JIRA_API_TOKEN', value: '@Microsoft.KeyVault(SecretUri=${jiraApiTokenSecret.properties.secretUri})' }
        { name: 'JIRA_PROJECT_KEY', value: jiraProjectKey }
        { name: 'JIRA_ISSUE_TYPE', value: jiraIssueType }
        { name: 'JIRA_CASE_ID_FIELD', value: jiraCaseIdField }
        { name: 'JIRA_ISSUE_ID_FIELD', value: jiraIssueIdField }
        { name: 'JIRA_XDR_URL_FIELD', value: jiraXdrUrlField }
        { name: 'RESOLUTION_TYPE_MAP', value: resolutionTypeMap }
        { name: 'DEFAULT_RESOLUTION_TYPE', value: defaultResolutionType }
        { name: 'MAX_SYNC_CASES', value: maxSyncCases }
        { name: 'SYNC_ISSUES', value: syncIssues }
        { name: 'SYNC_FROM_DATE', value: syncFromDate }
      ]
    }
  }
}

// --- Key Vault access for the Function App managed identity ---
// Role: Key Vault Secrets User (4633458b-17de-408a-b874-0445c86b69e6)
resource kvRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, functionApp.id, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// --- Outputs ---
output functionAppName string = functionApp.name
output functionAppUrl string = 'https://${functionApp.properties.defaultHostName}'
output storageAccountName string = storageAccount.name
output appInsightsName string = appInsights.name
output keyVaultName string = keyVault.name
