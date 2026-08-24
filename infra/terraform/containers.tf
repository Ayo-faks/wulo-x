# ============================================================================
# CONTAINER REGISTRY
# ============================================================================

resource "azurerm_container_registry" "main" {
  name                = local.resource_names.container_registry
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = local.should_create_private_endpoints && contains(var.private_endpoint_services, "acr") ? "Premium" : "Basic"
  admin_enabled       = false

  public_network_access_enabled = true

  tags = local.tags
}

# RBAC assignments for Container Registry
resource "azurerm_role_assignment" "acr_principal_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = local.principal_id
  principal_type       = local.principal_type
}

resource "azurerm_role_assignment" "acr_principal_push" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPush"
  principal_id         = local.principal_id
  principal_type       = local.principal_type
}

resource "azurerm_role_assignment" "acr_frontend_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.frontend.principal_id
}

resource "azurerm_role_assignment" "acr_backend_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.backend.principal_id
}

resource "azurerm_role_assignment" "acr_ai_foundry_project_pull" {
  scope                            = azurerm_container_registry.main.id
  role_definition_name             = "AcrPull"
  principal_id                     = module.ai_foundry.project_identity_principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "acr_voice_live_foundry_project_pull" {
  count = local.should_create_voice_live_account ? 1 : 0

  scope                            = azurerm_container_registry.main.id
  role_definition_name             = "AcrPull"
  principal_id                     = module.ai_foundry_voice_live[count.index].project_identity_principal_id
  skip_service_principal_aad_check = true
}


# ============================================================================
# CONTAINER APPS ENVIRONMENT
# ============================================================================

resource "azurerm_container_app_environment" "main" {
  name                = local.resource_names.container_env
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  infrastructure_subnet_id       = var.enable_private_networking ? azurerm_subnet.container_apps[0].id : null
  internal_load_balancer_enabled = var.enable_private_networking ? true : null
  public_network_access          = var.enable_private_networking ? "Disabled" : "Enabled"

  dynamic "identity" {
    for_each = local.should_create_container_app_origin_certificate ? [azurerm_user_assigned_identity.app_gateway[0].id] : []

    content {
      type         = "UserAssigned"
      identity_ids = [identity.value]
    }
  }

  dynamic "workload_profile" {
    for_each = var.enable_private_networking ? [
      {
        name    = "Consumption"
        type    = "Consumption"
        minimum = null
        maximum = null
      },
      {
        name    = var.container_apps_dedicated_workload_profile_name
        type    = var.container_apps_dedicated_workload_profile_type
        minimum = var.container_apps_dedicated_workload_min_nodes
        maximum = var.container_apps_dedicated_workload_max_nodes
      }
    ] : []

    content {
      name                  = workload_profile.value.name
      workload_profile_type = workload_profile.value.type
      minimum_count         = workload_profile.value.minimum
      maximum_count         = workload_profile.value.maximum
    }
  }

  tags = local.tags
}

resource "azapi_resource" "container_app_origin_certificate" {
  count = local.should_create_container_app_origin_certificate ? 1 : 0

  type      = "Microsoft.App/managedEnvironments/certificates@2026-01-01"
  name      = "cloudflare-origin-${var.environment_name}"
  parent_id = azurerm_container_app_environment.main.id
  location  = azurerm_resource_group.main.location
  tags      = local.tags

  body = {
    properties = {
      certificateKeyVaultProperties = {
        identity    = azurerm_user_assigned_identity.app_gateway[0].id
        keyVaultUrl = var.app_gateway_origin_certificate_secret_id
      }
    }
  }

  schema_validation_enabled = false
  response_export_values    = ["*"]

  depends_on = [
    azurerm_role_assignment.keyvault_app_gateway_secrets
  ]
}

# ============================================================================
# CONTAINER APPS
# ============================================================================

# Normalize integer memory values to the Azure API's canonical form.
locals {
  normalized_backend_memory = replace(
    var.container_memory_gb,
    "/^([0-9]+)\\.0(Gi)$/",
    "$1$2"
  )
  normalized_frontend_memory = "1Gi"
}

# Frontend Container App
resource "azurerm_container_app" "frontend" {
  name                         = "${var.name}-frontend-${local.resource_token}"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = var.enable_private_networking ? "Consumption" : null

  // Image is managed outside of terraform (i.e azd deploy)
  // EasyAuth configs are managed outside of terraform
  // Note: env vars are now managed via Azure App Configuration (apps read at runtime)
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      ingress[0].cors,
      ingress[0].client_certificate_mode,
      ingress[0].ip_security_restriction
    ]
  }

  identity {
    type         = "SystemAssigned, UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.frontend.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.frontend.id
  }

  ingress {
    # In an internal Container Apps environment this remains private to the VNet
    # through the environment ILB; it is required for Application Gateway ingress.
    external_enabled = true
    target_port      = 8080
    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  template {
    min_replicas = 1
    max_replicas = 10

    container {
      name   = "main"
      image  = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"
      cpu    = 0.5
      memory = local.normalized_frontend_memory

      # Azure App Configuration (PRIMARY CONFIG SOURCE)
      env {
        name  = "AZURE_APPCONFIG_ENDPOINT"
        value = module.appconfig.endpoint
      }

      env {
        name  = "AZURE_APPCONFIG_LABEL"
        value = var.environment_name
      }

      # Managed Identity for authentication
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.frontend.client_id
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }

      env {
        name  = "SERVICE_NAME"
        value = "rtaudio-client"
      }

      env {
        name  = "SERVICE_NAMESPACE"
        value = "clinic-recall"
      }

      env {
        name  = "ENVIRONMENT"
        value = var.environment_name
      }

      env {
        name  = "SERVICE_VERSION"
        value = data.external.git_commit.result["commit"]
      }

      env {
        name  = "GIT_SHA"
        value = data.external.git_commit.result["commit"]
      }

      env {
        name  = "CLINIC_RECALL_PILOT_ENVIRONMENT"
        value = var.clinic_recall_pilot_environment
      }

      env {
        name  = "CLINIC_RECALL_PILOT_RELEASE_IDENTITY"
        value = var.clinic_recall_pilot_release_identity
      }

      env {
        name  = "PORT"
        value = "8080"
      }
    }
  }

  tags = merge(local.tags, {
    "azd-service-name" = "rtaudio-client"
  })
}

# Backend Container App
resource "azurerm_container_app" "backend" {
  name                         = "${var.name}-backend-${local.resource_token}"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = var.enable_private_networking ? var.container_apps_dedicated_workload_profile_name : null

  identity {
    type         = "SystemAssigned, UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.backend.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.backend.id
  }

  ingress {
    # In an internal Container Apps environment this remains private to the VNet
    # through the environment ILB; it is required for Application Gateway ingress.
    external_enabled = true
    target_port      = 8000
    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  template {
    min_replicas = var.container_app_min_replicas
    max_replicas = var.container_app_max_replicas

    container {
      name   = "main"
      image  = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"
      cpu    = var.container_cpu_cores
      memory = local.normalized_backend_memory

      # ======================================================================
      # BOOTSTRAP ENVIRONMENT VARIABLES
      # ======================================================================
      # Only essential vars for app startup. All other configuration
      # (including secrets via Key Vault references) is fetched from
      # Azure App Configuration at runtime.
      # ======================================================================

      # Azure App Configuration (PRIMARY CONFIG SOURCE)
      env {
        name  = "AZURE_APPCONFIG_ENDPOINT"
        value = module.appconfig.endpoint
      }

      env {
        name  = "AZURE_APPCONFIG_LABEL"
        value = var.environment_name
      }

      # Managed Identity for authentication to Azure services
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.backend.client_id
      }

      # Application port
      env {
        name  = "PORT"
        value = "8000"
      }

      # Application Insights (needed early for telemetry)
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }

      env {
        name  = "SERVICE_NAME"
        value = "artagent-backend"
      }

      env {
        name  = "SERVICE_NAMESPACE"
        value = "clinic-recall"
      }

      env {
        name  = "ENVIRONMENT"
        value = var.environment_name
      }

      env {
        name  = "SERVICE_VERSION"
        value = data.external.git_commit.result["commit"]
      }

      env {
        name  = "GIT_SHA"
        value = data.external.git_commit.result["commit"]
      }

      env {
        name  = "CLINIC_RECALL_PILOT_ENVIRONMENT"
        value = var.clinic_recall_pilot_environment
      }

      env {
        name  = "CLINIC_RECALL_PILOT_RELEASE_IDENTITY"
        value = var.clinic_recall_pilot_release_identity
      }

      # Python runtime
      env {
        name  = "PYTHONUNBUFFERED"
        value = "1"
      }
    }
  }

  tags = merge(local.tags, {
    "azd-service-name" = "rtaudio-server"
  })

  // Image is managed outside of terraform (i.e azd deploy)
  lifecycle {
    ignore_changes = [
      template[0].container[0].image
    ]
  }
  depends_on = [
    azurerm_key_vault_secret.acs_connection_string,
    azurerm_role_assignment.keyvault_backend_secrets
  ]
}

# ============================================================================
# STICKY SESSIONS
# ============================================================================
# The azurerm provider does not support sticky sessions natively.
# Use azapi_update_resource to enable session affinity so that requests
# from the same client are routed to the same replica.

resource "azapi_update_resource" "frontend_sticky_sessions" {
  type        = "Microsoft.App/containerApps@2024-03-01"
  resource_id = azurerm_container_app.frontend.id

  body = {
    properties = {
      configuration = {
        ingress = merge(
          {
            stickySessions = {
              affinity = "sticky"
            }
          },
          local.should_bind_container_app_custom_domains ? {
            customDomains = [
              {
                name          = var.app_gateway_hostname
                bindingType   = "SniEnabled"
                certificateId = azapi_resource.container_app_origin_certificate[0].id
              },
              {
                name          = var.app_gateway_frontend_host_name
                bindingType   = "SniEnabled"
                certificateId = azapi_resource.container_app_origin_certificate[0].id
              }
            ]
          } : {}
        )
      }
    }
  }

  depends_on = [
    azapi_resource.container_app_origin_certificate
  ]
}

resource "azapi_update_resource" "backend_sticky_sessions" {
  type        = "Microsoft.App/containerApps@2024-03-01"
  resource_id = azurerm_container_app.backend.id

  body = {
    properties = {
      configuration = {
        ingress = merge(
          {
            stickySessions = {
              affinity = "sticky"
            }
            # azurerm_container_app has no cors_policy argument, so the ingress
            # CORS policy is set via the Container Apps REST API. Container Apps
            # ingress (Envoy) answers the CORS preflight (OPTIONS) at the edge
            # using these values; without it, browser cross-origin calls from the
            # frontend Container App fail their preflight with "No
            # 'Access-Control-Allow-Origin' header is present".
            corsPolicy = {
              allowedOrigins   = ["https://${azurerm_container_app.frontend.ingress[0].fqdn}"]
              allowedMethods   = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
              allowedHeaders   = ["Content-Type", "Authorization", "X-Requested-With", "Accept"]
              exposeHeaders    = ["Content-Length", "Content-Range"]
              allowCredentials = true
              maxAge           = 86400
            }
          },
          local.should_bind_container_app_custom_domains ? {
            customDomains = [
              {
                name          = var.app_gateway_backend_host_name
                bindingType   = "SniEnabled"
                certificateId = azapi_resource.container_app_origin_certificate[0].id
              }
            ]
          } : {}
        )
      }
    }
  }

  depends_on = [
    azapi_resource.container_app_origin_certificate
  ]
}

# ============================================================================
# ROLE ASSIGNMENTS: Monitoring Metrics Publisher for system-assigned identities
# ============================================================================

# Grant the frontend Container App's system-assigned identity permission to publish metrics
resource "azurerm_role_assignment" "frontend_metrics_publisher_system" {
  scope                = azurerm_application_insights.main.id
  role_definition_name = "Monitoring Metrics Publisher"
  principal_id         = azurerm_container_app.frontend.identity[0].principal_id
}

# Grant the backend Container App's system-assigned identity permission to publish metrics
resource "azurerm_role_assignment" "backend_metrics_publisher_system" {
  scope                = azurerm_application_insights.main.id
  role_definition_name = "Monitoring Metrics Publisher"
  principal_id         = azurerm_container_app.backend.identity[0].principal_id
}

# Container Apps Environment
output "CONTAINER_APPS_ENVIRONMENT_ID" {
  description = "Container Apps Environment resource ID"
  value       = azurerm_container_app_environment.main.id
}

output "CONTAINER_APPS_ENVIRONMENT_NAME" {
  description = "Container Apps Environment name"
  value       = azurerm_container_app_environment.main.name
}

# Container Apps
output "FRONTEND_CONTAINER_APP_NAME" {
  description = "Frontend Container App name"
  value       = azurerm_container_app.frontend.name
}

output "BACKEND_CONTAINER_APP_NAME" {
  description = "Backend Container App name"
  value       = azurerm_container_app.backend.name
}

output "FRONTEND_CONTAINER_APP_FQDN" {
  description = "Frontend Container App FQDN"
  value       = azurerm_container_app.frontend.ingress[0].fqdn
}

output "BACKEND_CONTAINER_APP_FQDN" {
  description = "Backend Container App FQDN"
  value       = azurerm_container_app.backend.ingress[0].fqdn
}

output "FRONTEND_CONTAINER_APP_URL" {
  description = "Frontend Container App URL"
  value       = "https://${azurerm_container_app.frontend.ingress[0].fqdn}"
}

output "BACKEND_CONTAINER_APP_URL" {
  description = "Backend Container App URL"
  value       = "https://${azurerm_container_app.backend.ingress[0].fqdn}"
}


output "BACKEND_API_URL" {
  description = "Backend API URL"
  value       = "https://${azurerm_container_app.backend.ingress[0].fqdn}"
}
