
## Wait 10 seconds for the AI Foundry project system-assigned managed identity to be created and to replicate
## through Entra ID
resource "time_sleep" "wait_project_identities" {
  depends_on = [
    azapi_resource.ai_foundry_project
  ]
  create_duration = "10s"
}
## Create AI Foundry project connections (only if id and endpoint vars are provided)
locals {
  cosmos_name_from_id = ((var.cosmosdb_account_id != null && var.cosmosdb_account_id != "")
    ? try(element(split("/", var.cosmosdb_account_id), length(split("/", var.cosmosdb_account_id)) - 1), "")
  : "")

  storage_name_from_id = ((var.storage_account_id != null && var.storage_account_id != "")
    ? try(element(split("/", var.storage_account_id), length(split("/", var.storage_account_id)) - 1), "")
  : "")

  aisearch_name_from_id = ((var.ai_search_id != null && var.ai_search_id != "")
    ? try(element(split("/", var.ai_search_id), length(split("/", var.ai_search_id)) - 1), "")
  : "")
}

resource "azapi_resource" "conn_cosmosdb" {
  count                     = (var.cosmosdb_account_id != null && var.cosmosdb_account_id != "" && var.cosmosdb_account_endpoint != null && var.cosmosdb_account_endpoint != "") ? 1 : 0
  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01"
  name                      = local.cosmos_name_from_id
  parent_id                 = azapi_resource.ai_foundry_project.id
  schema_validation_enabled = false

  depends_on = [
    azapi_resource.ai_foundry_project
  ]

  body = {
    name = local.cosmos_name_from_id
    properties = {
      category = "CosmosDb"
      target   = var.cosmosdb_account_endpoint
      authType = "AAD"
      metadata = {
        ApiType    = "Azure"
        ResourceId = var.cosmosdb_account_id
        location   = var.location
      }
    }
  }
}

resource "azapi_resource" "conn_storage" {
  count                     = (var.storage_account_id != null && var.storage_account_id != "" && var.storage_account_primary_blob_endpoint != null && var.storage_account_primary_blob_endpoint != "") ? 1 : 0
  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01"
  name                      = local.storage_name_from_id
  parent_id                 = azapi_resource.ai_foundry_project.id
  schema_validation_enabled = false

  depends_on = [
    azapi_resource.ai_foundry_project
  ]

  body = {
    name = local.storage_name_from_id
    properties = {
      category = "AzureStorageAccount"
      target   = var.storage_account_primary_blob_endpoint
      authType = "AAD"
      metadata = {
        ApiType    = "Azure"
        ResourceId = var.storage_account_id
        location   = var.location
      }
    }
  }

  response_export_values = [
    "identity.principalId"
  ]
}

resource "azapi_resource" "conn_aisearch" {
  count                     = (var.ai_search_id != null && var.ai_search_id != "" && var.ai_search_endpoint != null && var.ai_search_endpoint != "") ? 1 : 0
  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01"
  name                      = local.aisearch_name_from_id
  parent_id                 = azapi_resource.ai_foundry_project.id
  schema_validation_enabled = false

  depends_on = [
    azapi_resource.ai_foundry_project
  ]

  body = {
    name = local.aisearch_name_from_id
    properties = {
      category = "CognitiveSearch"
      target   = var.ai_search_endpoint
      authType = "AAD"
      metadata = {
        ApiType    = "Azure"
        ApiVersion = "2025-05-01-preview"
        ResourceId = var.ai_search_id
        location   = var.location
      }
    }
  }

  response_export_values = [
    "identity.principalId"
  ]
}

resource "azurerm_role_assignment" "cosmosdb_operator_ai_foundry_project" {
  count = (var.cosmosdb_account_id != null && var.cosmosdb_account_id != "") ? 1 : 0

  depends_on = [
    resource.time_sleep.wait_project_identities
  ]
  name                 = uuidv5("dns", "${azapi_resource.ai_foundry_project.name}${azapi_resource.ai_foundry_project.output.identity.principalId}${local.cosmos_name_from_id}cosmosdboperator")
  scope                = var.cosmosdb_account_id
  role_definition_name = "Cosmos DB Operator"
  principal_id         = azapi_resource.ai_foundry_project.output.identity.principalId
}

resource "azurerm_role_assignment" "storage_blob_data_contributor_ai_foundry_project" {
  count = (var.storage_account_id != null && var.storage_account_id != "") ? 1 : 0

  depends_on = [
    resource.time_sleep.wait_project_identities
  ]
  name                 = uuidv5("dns", "${azapi_resource.ai_foundry_project.name}${azapi_resource.ai_foundry_project.output.identity.principalId}${local.storage_name_from_id}storageblobdatacontributor")
  scope                = var.storage_account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azapi_resource.ai_foundry_project.output.identity.principalId
}

resource "azurerm_role_assignment" "search_index_data_contributor_ai_foundry_project" {
  count = (var.ai_search_id != null && var.ai_search_id != "") ? 1 : 0

  depends_on = [
    resource.time_sleep.wait_project_identities
  ]
  name                 = uuidv5("dns", "${azapi_resource.ai_foundry_project.name}${azapi_resource.ai_foundry_project.output.identity.principalId}${local.aisearch_name_from_id}searchindexdatacontributor")
  scope                = var.ai_search_id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = azapi_resource.ai_foundry_project.output.identity.principalId
}

resource "azurerm_role_assignment" "search_service_contributor_ai_foundry_project" {
  count = (var.ai_search_id != null && var.ai_search_id != "") ? 1 : 0

  depends_on = [
    resource.time_sleep.wait_project_identities
  ]
  name                 = uuidv5("dns", "${azapi_resource.ai_foundry_project.name}${azapi_resource.ai_foundry_project.output.identity.principalId}${local.aisearch_name_from_id}searchservicecontributor")
  scope                = var.ai_search_id
  role_definition_name = "Search Service Contributor"
  principal_id         = azapi_resource.ai_foundry_project.output.identity.principalId
}

## Pause 60 seconds to allow for role assignments to propagate
##
resource "time_sleep" "wait_rbac" {
  depends_on = [
    azurerm_role_assignment.cosmosdb_operator_ai_foundry_project,
    azurerm_role_assignment.storage_blob_data_contributor_ai_foundry_project,
    azurerm_role_assignment.search_index_data_contributor_ai_foundry_project,
    azurerm_role_assignment.search_service_contributor_ai_foundry_project
  ]
  create_duration = "60s"
}
