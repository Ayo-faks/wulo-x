# ============================================================================
# OUTPUTS FOR AZD INTEGRATION AND APPLICATION CONFIGURATION
# ============================================================================
output "ENVIRONMENT_NAME" {
  description = "Deployment environment name (e.g., dev, staging, prod)"
  value       = var.environment_name
}

output "AZURE_RESOURCE_GROUP" {
  description = "Azure Resource Group name"
  value       = azurerm_resource_group.main.name
}

output "RESOURCE_GROUP_ID" {
  description = "Azure Resource Group resource ID"
  value       = azurerm_resource_group.main.id
}

output "AZURE_LOCATION" {
  description = "Azure region location"
  value       = azurerm_resource_group.main.location
}

output "VIRTUAL_NETWORK_ID" {
  description = "Application landing-zone virtual network ID when private networking is enabled"
  value       = var.enable_private_networking ? azurerm_virtual_network.main[0].id : ""
}

output "CONTAINER_APPS_SUBNET_ID" {
  description = "Container Apps environment infrastructure subnet ID when private networking is enabled"
  value       = var.enable_private_networking ? azurerm_subnet.container_apps[0].id : ""
}

output "PRIVATE_ENDPOINT_SUBNET_ID" {
  description = "Private endpoint subnet ID when private networking is enabled"
  value       = var.enable_private_networking ? azurerm_subnet.private_endpoints[0].id : ""
}

output "PRIVATE_DNS_ZONE_IDS" {
  description = "Private DNS zone IDs keyed by private endpoint service"
  value = merge(
    { for service, zone in azurerm_private_dns_zone.main : service => zone.id },
    var.enable_private_networking ? {
      container_apps_internal = azurerm_private_dns_zone.container_apps[0].id
      container_apps_default  = azurerm_private_dns_zone.container_apps_default[0].id
      container_apps_custom   = length(azurerm_private_dns_zone.container_app_custom_domain) > 0 ? azurerm_private_dns_zone.container_app_custom_domain[0].id : ""
    } : {}
  )
}

output "PRIVATE_ENDPOINT_IDS" {
  description = "Private endpoint resource IDs for the stable first-pass service set"
  value = {
    acr          = length(azurerm_private_endpoint.acr) > 0 ? azurerm_private_endpoint.acr[0].id : ""
    keyvault     = length(azurerm_private_endpoint.keyvault) > 0 ? azurerm_private_endpoint.keyvault[0].id : ""
    storage_blob = length(azurerm_private_endpoint.storage_blob) > 0 ? azurerm_private_endpoint.storage_blob[0].id : ""
    appconfig    = length(azurerm_private_endpoint.appconfig) > 0 ? azurerm_private_endpoint.appconfig[0].id : ""
    postgres     = length(azurerm_private_endpoint.postgres) > 0 ? azurerm_private_endpoint.postgres[0].id : ""
  }
}

output "APPLICATION_GATEWAY_ID" {
  description = "Application Gateway WAF origin ID when enabled"
  value       = local.should_create_application_gateway ? azurerm_application_gateway.main[0].id : ""
}

output "APPLICATION_GATEWAY_PUBLIC_IP" {
  description = "Application Gateway public origin IP for Cloudflare DNS when enabled"
  value       = local.should_create_application_gateway ? azurerm_public_ip.app_gateway[0].ip_address : ""
}

output "APPLICATION_GATEWAY_HOSTNAME" {
  description = "Hostname expected by the Application Gateway listener"
  value       = var.app_gateway_hostname
}

output "APPLICATION_GATEWAY_BACKEND_HOST_NAME" {
  description = "Container Apps custom hostname that Application Gateway should send as backend Host/SNI when custom domains are enabled"
  value       = var.app_gateway_backend_host_name
}

output "APPLICATION_GATEWAY_FRONTEND_HOST_NAME" {
  description = "Container Apps custom hostname that Application Gateway should send as frontend Host/SNI when custom domains are enabled"
  value       = var.app_gateway_frontend_host_name
}

output "CONTAINER_APP_CUSTOM_DOMAIN_DNS_RECORDS" {
  description = "Non-secret DNS validation records required before enabling Container Apps custom-domain bindings"
  value = {
    backend = {
      hostname                 = var.app_gateway_backend_host_name
      cname_record_name        = var.app_gateway_backend_host_name
      cname_record_value       = azurerm_container_app.backend.ingress[0].fqdn
      txt_record_name          = var.app_gateway_backend_host_name != "" ? "asuid.${var.app_gateway_backend_host_name}" : ""
      txt_record_value         = nonsensitive(azurerm_container_app.backend.custom_domain_verification_id)
      container_app_static_ip  = azurerm_container_app_environment.main.static_ip_address
      validation_record_status = var.app_gateway_backend_host_name != "" ? "create TXT plus CNAME before setting enable_container_app_custom_domains=true" : "not configured"
    }
    frontend = {
      hostname                 = var.app_gateway_frontend_host_name
      cname_record_name        = var.app_gateway_frontend_host_name
      cname_record_value       = azurerm_container_app.frontend.ingress[0].fqdn
      txt_record_name          = var.app_gateway_frontend_host_name != "" ? "asuid.${var.app_gateway_frontend_host_name}" : ""
      txt_record_value         = nonsensitive(azurerm_container_app.frontend.custom_domain_verification_id)
      container_app_static_ip  = azurerm_container_app_environment.main.static_ip_address
      validation_record_status = var.app_gateway_frontend_host_name != "" ? "create TXT plus CNAME before setting enable_container_app_custom_domains=true" : "not configured"
    }
  }
}

output "CONTAINER_APP_ORIGIN_CERTIFICATE_ID" {
  description = "Container Apps environment certificate resource ID for the Cloudflare origin certificate when configured"
  value       = local.should_create_container_app_origin_certificate ? azapi_resource.container_app_origin_certificate[0].id : ""
}

# AI Services
output "AZURE_OPENAI_ENDPOINT" {
  description = "Azure OpenAI endpoint"
  value       = module.ai_foundry.openai_endpoint
}

output "AZURE_OPENAI_CHAT_DEPLOYMENT_ID" {
  description = "Azure OpenAI Chat Deployment ID. Default chat model to use if not specified by the agent config."
  value       = "gpt-4o"
}

output "AZURE_OPENAI_API_VERSION" {
  description = "Azure OpenAI API version"
  value       = "2025-01-01-preview"
}

output "AZURE_SPEECH_ENDPOINT" {
  description = "Azure Speech Services endpoint"
  value       = module.ai_foundry.endpoint
}

output "AZURE_SPEECH_RESOURCE_ID" {
  description = "Azure Speech Services resource ID"
  value       = module.ai_foundry.account_id
}

output "AZURE_SPEECH_REGION" {
  description = "Azure Speech Services location"
  value       = module.ai_foundry.location
}

# Communication Services
output "ACS_ENDPOINT" {
  description = "Azure Communication Services endpoint"
  value       = "https://${azapi_resource.acs.output.properties.hostName}"
}

output "ACS_IMMUTABLE_ID" {
  description = "Azure Communication Services immutable ID"
  value       = azapi_resource.acs.output.properties.immutableResourceId
}

output "ACS_RESOURCE_ID" {
  description = "Azure Communication Services resource ID"
  value       = azapi_resource.acs.id
}

output "ACS_EVENTGRID_SYSTEM_TOPIC_NAME" {
  description = "Event Grid system topic name for Azure Communication Services"
  value       = azurerm_eventgrid_system_topic.acs.name
}

output "AZURE_EMAIL_SENDER_ADDRESS" {
  description = "Azure Email Communication Services sender address (e.g., noreply@domain.azurecomm.net)"
  value       = var.enable_acs_email ? "${local.email_sender_username}@${azurerm_email_communication_service_domain.managed[0].mail_from_sender_domain}" : ""
}

# output "ACS_MANAGED_IDENTITY_PRINCIPAL_ID" {
#   description = "Azure Communication Services system-assigned managed identity principal ID"
#   value = data.azapi_resource.acs_identity_details.identity.principalId
# }

# Data Services
output "AZURE_STORAGE_ACCOUNT_NAME" {
  description = "Azure Storage Account name"
  value       = azurerm_storage_account.main.name
}

output "AZURE_STORAGE_BLOB_ENDPOINT" {
  description = "Azure Storage Blob endpoint"
  value       = azurerm_storage_account.main.primary_blob_endpoint
}

output "AZURE_STORAGE_CONTAINER_URL" {
  description = "Azure Storage Container URL"
  value       = "${azurerm_storage_account.main.primary_blob_endpoint}${azurerm_storage_container.audioagent.name}"
}

output "RECORDINGS_BLOB_ACCOUNT_URL" {
  description = "Blob account URL used by the consented recording persistence pipeline"
  value       = azurerm_storage_account.main.primary_blob_endpoint
}

output "RECORDINGS_BLOB_CONTAINER" {
  description = "Private container used by the consented recording persistence pipeline"
  value       = azurerm_storage_container.call_recordings.name
}

output "AZURE_COSMOS_DATABASE_NAME" {
  description = "Azure Cosmos DB database name"
  value       = var.mongo_database_name
}

output "AZURE_COSMOS_COLLECTION_NAME" {
  description = "Azure Cosmos DB collection name"
  value       = var.mongo_collection_name
}

output "AZURE_COSMOS_CONNECTION_STRING" {
  description = "Azure Cosmos DB connection string"
  value = replace(
    data.azapi_resource.mongo_cluster_info.output.properties.connectionString,
    "/mongodb\\+srv:\\/\\/[^@]+@([^?]+)\\?(.*)$/",
    "mongodb+srv://$1?tls=true&authMechanism=MONGODB-OIDC&retrywrites=false&maxIdleTimeMS=120000"
  )
  sensitive = true
}

output "POSTGRES_SERVER_NAME" {
  description = "Phase 0 PostgreSQL Flexible Server name"
  value       = azurerm_postgresql_flexible_server.clinic_recall.name
}

output "POSTGRES_HOST" {
  description = "Phase 0 PostgreSQL Flexible Server host"
  value       = azurerm_postgresql_flexible_server.clinic_recall.fqdn
}

output "POSTGRES_DATABASE_NAME" {
  description = "Phase 0 Clinic Recall PostgreSQL database name"
  value       = azurerm_postgresql_flexible_server_database.clinic_recall.name
}

output "POSTGRES_ADMIN_LOGIN" {
  description = "Phase 0 PostgreSQL administrator login"
  value       = var.postgres_admin_login
}

output "POSTGRES_CONNECTION_STRING" {
  description = "Phase 0 PostgreSQL connection string"
  value       = azurerm_key_vault_secret.postgres_connection_string.value
  sensitive   = true
}

# Redis
output "REDIS_HOSTNAME" {
  description = "Redis Enterprise hostname"
  value       = data.azapi_resource.redis_enterprise_fetched.output.properties.hostName
}

output "REDIS_PORT" {
  description = "Redis Enterprise port"
  value       = var.redis_port
}

# Key Vault
output "AZURE_KEY_VAULT_NAME" {
  description = "Azure Key Vault name"
  value       = azurerm_key_vault.main.name
}

output "AZURE_KEY_VAULT_ENDPOINT" {
  description = "Azure Key Vault endpoint"
  value       = azurerm_key_vault.main.vault_uri
}

# Managed Identities
output "BACKEND_UAI_CLIENT_ID" {
  description = "Backend User Assigned Identity Client ID"
  value       = azurerm_user_assigned_identity.backend.client_id
}

output "BACKEND_UAI_PRINCIPAL_ID" {
  description = "Backend User Assigned Identity Principal ID"
  value       = azurerm_user_assigned_identity.backend.principal_id
}

output "FRONTEND_UAI_CLIENT_ID" {
  description = "Frontend User Assigned Identity Client ID"
  value       = azurerm_user_assigned_identity.frontend.client_id
}

output "FRONTEND_UAI_PRINCIPAL_ID" {
  description = "Frontend User Assigned Identity Principal ID"
  value       = azurerm_user_assigned_identity.frontend.principal_id
}

# Container Registry
output "AZURE_CONTAINER_REGISTRY_ENDPOINT" {
  description = "Azure Container Registry endpoint"
  value       = azurerm_container_registry.main.login_server
}

# Monitoring
output "APPLICATIONINSIGHTS_CONNECTION_STRING" {
  description = "Application Insights connection string"
  value       = azurerm_application_insights.main.connection_string
  sensitive   = true
}

output "LOG_ANALYTICS_WORKSPACE_ID" {
  description = "Log Analytics workspace ID"
  value       = azurerm_log_analytics_workspace.main.id
}

output "AZURE_MONITOR_INGESTION_ENDPOINT" {
  description = "Logs Ingestion API endpoint for aggregate CI and evaluation metrics"
  value       = azurerm_monitor_data_collection_endpoint.clinic_recall_metrics.logs_ingestion_endpoint
}

output "AZURE_MONITOR_DCR_RULE_ID" {
  description = "Immutable data collection rule ID used by the Logs Ingestion API"
  value       = azurerm_monitor_data_collection_rule.clinic_recall_metrics.immutable_id
}

output "CLINIC_RECALL_UNIFIED_WORKBOOK_ID" {
  description = "Azure Monitor Workbook resource ID for the unified Clinic Recall operations dashboard"
  value       = azurerm_application_insights_workbook.clinic_recall_unified.id
}

# Performance Configuration Outputs
output "AOAI_POOL_SIZE" {
  description = "Azure OpenAI pool size for performance optimization"
  value       = var.aoai_pool_size
}

output "TTS_POOL_SIZE" {
  description = "TTS pool size for concurrent session handling"
  value       = var.tts_pool_size
}

output "STT_POOL_SIZE" {
  description = "STT pool size for concurrent session handling"
  value       = var.stt_pool_size
}

output "CONTAINER_CPU_CORES" {
  description = "CPU cores allocated per container instance"
  value       = var.container_cpu_cores
}

output "CONTAINER_MEMORY_GB" {
  description = "Memory allocated per container instance"
  value       = replace(var.container_memory_gb, "/^([0-9]+)(Gi)$/", "$1.0$2")
}

output "CONTAINER_MIN_REPLICAS" {
  description = "Minimum container replicas for high availability"
  value       = var.container_app_min_replicas
}

output "CONTAINER_MAX_REPLICAS" {
  description = "Maximum container replicas for auto-scaling"
  value       = var.container_app_max_replicas
}

output "REDIS_SKU_OPTIMIZED" {
  description = "Redis Enterprise SKU for optimal performance"
  value       = var.redis_sku
}


output "ai_foundry_account_id" {
  description = "Resource ID of the AI Foundry account"
  value       = module.ai_foundry.account_id
}

output "ai_foundry_account_endpoint" {
  description = "Endpoint URI for the AI Foundry account"
  value       = module.ai_foundry.endpoint
}

output "ai_foundry_project_id" {
  description = "Resource ID of the AI Foundry project"
  value       = module.ai_foundry.project_id
}

output "ai_foundry_project_endpoint" {
  description = "Endpoint URI for the AI Foundry project (used for Evaluations SDK)"
  value       = module.ai_foundry.project_endpoint
}

output "ai_foundry_project_identity_principal_id" {
  description = "Managed identity principal ID assigned to the AI Foundry project"
  value       = module.ai_foundry.project_identity_principal_id
}

output "AZURE_VOICELIVE_ENDPOINT" {
  description = "Azure Voice Live endpoint (prefers the blue/green realtime account when enabled)"
  value       = var.enable_voice_live ? (length(module.ai_foundry_voice_live_realtime) > 0 ? module.ai_foundry_voice_live_realtime[0].endpoint : (length(module.ai_foundry_voice_live) > 0 ? module.ai_foundry_voice_live[0].endpoint : module.ai_foundry.endpoint)) : ""
}

output "AZURE_VOICELIVE_RESOURCE_ID" {
  description = "Azure Voice Live resource ID (prefers the blue/green realtime account when enabled)"
  value       = var.enable_voice_live ? (length(module.ai_foundry_voice_live_realtime) > 0 ? module.ai_foundry_voice_live_realtime[0].account_id : (length(module.ai_foundry_voice_live) > 0 ? module.ai_foundry_voice_live[0].account_id : module.ai_foundry.account_id)) : ""
}

output "AZURE_VOICELIVE_MODEL" {
  description = "Azure Voice Live model deployment name"
  value       = var.enable_voice_live && length(local.voice_live_model_names) > 0 ? local.voice_live_model_names[0] : ""
}

# ============================================================================
# APP CONFIGURATION
# ============================================================================

output "AZURE_APPCONFIG_ENDPOINT" {
  description = "Azure App Configuration endpoint for centralized config management"
  value       = module.appconfig.endpoint
}

output "AZURE_APPCONFIG_NAME" {
  description = "Azure App Configuration resource name"
  value       = module.appconfig.name
}

output "AZURE_APPCONFIG_LABEL" {
  description = "Environment label used in App Configuration"
  value       = module.appconfig.label
}
