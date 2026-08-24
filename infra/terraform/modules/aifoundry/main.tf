# Terraform module for provisioning Azure AI Foundry aligned with the ai-services deployment.

locals {
  project_id_guid = "${substr(azapi_resource.ai_foundry_project.output.properties.internalId, 0, 8)}-${substr(azapi_resource.ai_foundry_project.output.properties.internalId, 8, 4)}-${substr(azapi_resource.ai_foundry_project.output.properties.internalId, 12, 4)}-${substr(azapi_resource.ai_foundry_project.output.properties.internalId, 16, 4)}-${substr(azapi_resource.ai_foundry_project.output.properties.internalId, 20, 12)}"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azapi_resource" "ai_foundry_account" {
  type                      = "Microsoft.CognitiveServices/accounts@2025-06-01"
  name                      = var.foundry_account_name
  parent_id                 = data.azurerm_resource_group.rg.id
  location                  = var.location
  schema_validation_enabled = false
  tags                      = var.tags

  body = {
    kind = "AIServices"
    sku = {
      name = var.foundry_sku_name
    }
    identity = {
      type = "SystemAssigned"
    }
    properties = {
      allowProjectManagement = true
      disableLocalAuth       = var.disable_local_auth
      customSubDomainName    = var.foundry_custom_subdomain_name
    }
  }
}


resource "azurerm_cognitive_deployment" "model" {
  for_each = { for deployment in var.model_deployments : deployment.name => deployment }

  name                   = each.value.name
  cognitive_account_id   = azapi_resource.ai_foundry_account.id
  version_upgrade_option = "NoAutoUpgrade"

  sku {
    name     = each.value.sku_name
    capacity = each.value.capacity
  }

  model {
    format  = "OpenAI"
    name    = each.value.name
    version = each.value.version
  }
}

resource "azapi_resource" "ai_foundry_project" {
  type                      = "Microsoft.CognitiveServices/accounts/projects@2025-06-01"
  name                      = var.project_name
  parent_id                 = azapi_resource.ai_foundry_account.id
  location                  = var.location
  schema_validation_enabled = false

  body = {

    identity = {
      type = "SystemAssigned"
    }
    sku = {
      name = var.project_sku_name
    }
    properties = {
      displayName = var.project_display_name
      description = var.project_description
    }
  }
  response_export_values = [
    "identity.principalId",
    "properties.internalId"
  ]
}

resource "azurerm_role_assignment" "ai_foundry_account" {
  for_each = var.account_principal_ids

  scope                = azapi_resource.ai_foundry_account.id
  role_definition_name = var.account_principal_role_definition_name
  principal_id         = each.value
}
