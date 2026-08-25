# ============================================================================
# TERRAFORM CONFIGURATION
# ============================================================================

terraform {
  required_version = ">= 1.1.7, < 2.0.0"

  # Backend is configured separately via backend-azurerm.tf or backend-local.tf
  # The preprovision script selects the appropriate backend based on LOCAL_STATE env var

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.2"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.10"
    }
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
    app_configuration {
      purge_soft_delete_on_destroy = true
      recover_soft_deleted         = true
    }
    cognitive_account {
      purge_soft_delete_on_destroy = true

    }
  }
  storage_use_azuread = true
}

provider "azuread" {}

provider "azapi" {
}

# ============================================================================
# DATA SOURCES
# ============================================================================

data "azuread_client_config" "current" {}

data "external" "git_commit" {
  program     = ["sh", "-c", "printf '{\"commit\": \"%s\"}' \"$(git rev-parse --short HEAD)\""]
  working_dir = path.module
}

# ============================================================================
# RANDOM RESOURCES
# ============================================================================

resource "random_string" "resource_token" {
  length  = 8
  upper   = false
  special = false
}

# ============================================================================
# LOCALS & VARIABLES
# ============================================================================

locals {
  principal_id   = var.principal_id != null ? var.principal_id : data.azuread_client_config.current.object_id
  principal_type = var.principal_type
  # Generate a unique resource token
  resource_token = random_string.resource_token.result

  email_sender_username     = "noreply"
  email_sender_display_name = "Real-Time Voice Notifications"

  # Common tags (excludes volatile values to prevent unnecessary resource updates).
  # var.tags is merged last so environment-specific ownership/compliance tags can
  # be added from param files without embedding policy exemptions in the module.
  tags = merge({
    "azd-env-name" = var.environment_name
    "hidden-title" = "Azure Real-Time Audio ${var.environment_name}"
    "project"      = "ART Voice Agent Accelerator"
    "environment"  = var.environment_name
    "deployment"   = "terraform"
    "deployed_by"  = coalesce(var.deployed_by, local.principal_id)
  }, var.tags)

  voice_live_available_regions = ["eastus2", "westus2", "swedencentral", "southeastasia"]

  # Voice Live-only model names to exclude from base deployments when using a separate Voice Live account.
  # Keep general chat/eval models (for example gpt-4o-mini) deployed in the base Foundry account too.
  voice_live_model_names = ["gpt-realtime", "gpt-4o-transcribe"]

  # Resource naming with Azure standard abbreviations
  # Following Azure Cloud Adoption Framework: https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/azure-best-practices/resource-abbreviations
  resource_names = {
    resource_group             = "rg-${var.name}-${var.environment_name}"
    app_service_plan           = "asp-${var.name}-${var.environment_name}-${local.resource_token}"
    key_vault                  = "kv-${local.resource_token}"
    speech                     = "spch-${var.environment_name}-${local.resource_token}"
    openai                     = "oai-${local.resource_token}"
    cosmos                     = "cosmos-cluster-${local.resource_token}"
    storage                    = "st${local.resource_token}"
    postgres                   = "psql-${var.name}-${var.environment_name}-${local.resource_token}"
    redis                      = "redis${local.resource_token}"
    acs                        = "acs-${var.name}-${var.environment_name}-${local.resource_token}"
    container_registry         = "cr${var.name}${local.resource_token}"
    log_analytics              = "log-${local.resource_token}"
    app_insights               = "ai-${local.resource_token}"
    container_env              = "cae-${var.name}-${var.environment_name}-${local.resource_token}"
    virtual_network            = "vnet-${var.name}-${var.environment_name}-${local.resource_token}"
    subnet_app_gateway         = "snet-appgw-${var.environment_name}"
    subnet_container_apps      = "snet-aca-${var.environment_name}"
    subnet_private_endpoints   = "snet-pe-${var.environment_name}"
    nsg_app_gateway            = "nsg-appgw-${var.environment_name}-${local.resource_token}"
    nsg_container_apps         = "nsg-aca-${var.environment_name}-${local.resource_token}"
    nsg_private_endpoints      = "nsg-pe-${var.environment_name}-${local.resource_token}"
    ddos_plan                  = "ddos-${var.name}-${var.environment_name}-${local.resource_token}"
    app_gateway                = "agw-${var.name}-${var.environment_name}-${local.resource_token}"
    app_gateway_public_ip      = "pip-agw-${var.name}-${var.environment_name}-${local.resource_token}"
    app_gateway_identity       = "id-agw-${var.name}-${var.environment_name}-${local.resource_token}"
    app_gateway_waf_policy     = "waf-agw-${var.name}-${var.environment_name}-${local.resource_token}"
    pe_acr                     = "pe-acr-${var.environment_name}-${local.resource_token}"
    pe_keyvault                = "pe-kv-${var.environment_name}-${local.resource_token}"
    pe_storage_blob            = "pe-stblob-${var.environment_name}-${local.resource_token}"
    pe_appconfig               = "pe-appcfg-${var.environment_name}-${local.resource_token}"
    pe_postgres                = "pe-psql-${var.environment_name}-${local.resource_token}"
    email_service              = "email-${var.name}-${var.environment_name}-${local.resource_token}"
    email_domain               = "AzureManagedDomain"
    foundry_account            = substr(replace("${var.name}-${local.resource_token}-aif", "/[^a-zA-Z0-9]/", ""), 0, 24)
    foundry_project            = "${var.name}-${local.resource_token}-aif-proj"
    voice_live_foundry_account = substr(replace("${var.name}-${local.resource_token}-avl", "/[^a-zA-Z0-9]/", ""), 0, 24)
    voice_live_foundry_project = "${var.name}-${local.resource_token}-avl-proj"
  }

  foundry_project_display = "AI Foundry ${var.environment_name}"
  foundry_project_desc    = "AI Foundry project for ${var.environment_name} environment"

  voice_live_supported_region      = contains(local.voice_live_available_regions, azurerm_resource_group.main.location)
  voice_live_primary_region        = var.voice_live_location
  should_enable_voice_live_here    = var.enable_voice_live && local.voice_live_supported_region
  should_create_voice_live_account = var.enable_voice_live && !local.voice_live_supported_region

  # Blue/green second realtime account (e.g. uksouth co-location, 2026-07-09).
  # The original -avl account is preserved: it hosts the governed Foundry agents
  # and stays as the rollback target.
  should_create_voice_live_realtime_account = var.enable_voice_live && var.voice_live_realtime_location != ""

  base_model_deployments_map = {
    for deployment in var.model_deployments :
    deployment.name => deployment
    if !(local.should_create_voice_live_account && contains(local.voice_live_model_names, deployment.name))
  }

  # Convert voice_live_model_deployments variable to map
  voice_live_model_deployments_map = {
    for deployment in var.voice_live_model_deployments :
    deployment.name => deployment
  }

  combined_model_deployments_map = local.should_enable_voice_live_here ? merge(local.base_model_deployments_map, local.voice_live_model_deployments_map) : local.base_model_deployments_map
  combined_model_deployments     = [for deployment in values(local.combined_model_deployments_map) : deployment]
  voice_live_model_deployments   = var.voice_live_model_deployments

  voice_live_project_display = "AI Foundry Voice Live ${var.environment_name}"
  voice_live_project_desc    = "AI Foundry Voice Live project for ${var.environment_name} environment"

  should_create_application_gateway                     = var.enable_private_networking && var.enable_application_gateway && var.app_gateway_hostname != "" && var.app_gateway_origin_certificate_secret_id != "" && var.app_gateway_trusted_root_certificate_base64 != ""
  should_create_container_app_origin_certificate        = local.should_create_application_gateway && var.app_gateway_backend_host_name != "" && var.app_gateway_frontend_host_name != ""
  should_bind_container_app_custom_domains              = local.should_create_container_app_origin_certificate && var.enable_container_app_custom_domains
  should_create_container_app_custom_domain_private_dns = local.should_bind_container_app_custom_domains && var.container_app_custom_domain_private_dns_zone_name != ""
  should_create_private_endpoints                       = var.enable_private_networking && var.enable_private_endpoints
}
