variable "resource_group_name" {
  description = "Resource group name (used for diagnostics configuration)."
  type        = string
}

variable "location" {
  description = "Azure region for AI Foundry resources."
  type        = string
}

variable "tags" {
  description = "Tags to inherit from the core deployment."
  type        = map(string)
  default     = {}
}

variable "disable_local_auth" {
  description = "Disable local (key-based) authentication for the AI Foundry account."
  type        = bool
  default     = true
}

variable "foundry_account_name" {
  description = "Name for the Azure AI Foundry account (3-24 lowercase alphanumeric characters)."
  type        = string
  validation {
    condition     = length(trimspace(var.foundry_account_name)) >= 3 && length(trimspace(var.foundry_account_name)) <= 24
    error_message = "Foundry account name must be between 3 and 24 characters."
  }
}

variable "foundry_custom_subdomain_name" {
  description = "Optional custom subdomain for the AI Foundry endpoint. Defaults to the account name."
  type        = string
  default     = null
}

variable "foundry_sku_name" {
  description = "SKU for the AI Foundry account."
  type        = string
  default     = "S0"
}

variable "project_name" {
  description = "Name for the AI Foundry project. Defaults to <account-name>-project."
  type        = string
  default     = null
}

variable "project_display_name" {
  description = "Display name for the AI Foundry project."
  type        = string
  default     = null
}

variable "project_description" {
  description = "Description for the AI Foundry project."
  type        = string
  default     = null
}

variable "project_sku_name" {
  description = "SKU for the AI Foundry project."
  type        = string
  default     = "S0"
}

variable "model_deployments" {
  description = "Model deployments to create within the AI Foundry account."
  type = list(object({
    name     = string
    version  = string
    sku_name = string
    capacity = number
  }))
  default = [
    {
      name     = "gpt-4o"
      version  = "2024-11-20"
      sku_name = "GlobalStandard"
      capacity = 1
    }
  ]
}

variable "log_analytics_workspace_id" {
  description = "Optional Log Analytics workspace ID used for diagnostics."
  type        = string
  default     = null
}

variable "account_principal_ids" {
  description = "Map of principals to assign Cognitive Services access to the AI Foundry account (keys should be stable labels)."
  type        = map(string)
  default     = {}
}

variable "account_principal_role_definition_name" {
  description = "Role definition to use for AI Foundry account assignments (defaults to Cognitive Services User)."
  type        = string
  default     = "Cognitive Services User"
}

# ============================================================================

variable "cosmosdb_account_id" {
  description = "Optional Cosmos DB account ID for AI Foundry to use for storage."
  type        = string
  default     = null
}

variable "cosmosdb_account_endpoint" {
  description = "Optional Cosmos DB account endpoint for AI Foundry to use for storage."
  type        = string
  default     = null
}

variable "storage_account_id" {
  description = "Optional Storage account ID for AI Foundry to use for storage."
  type        = string
  default     = null
}

variable "storage_account_primary_blob_endpoint" {
  description = "Optional Storage account primary blob endpoint for AI Foundry to use for storage."
  type        = string
  default     = null
}

variable "ai_search_id" {
  description = "Optional Azure AI Search resource ID for AI Foundry to use for search capabilities."
  type        = string
  default     = null
}

variable "ai_search_endpoint" {
  description = "Optional Azure AI Search resource endpoint for AI Foundry to use for search capabilities."
  type        = string
  default     = null
}
