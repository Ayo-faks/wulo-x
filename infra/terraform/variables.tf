# ============================================================================
# VARIABLES
# ============================================================================
variable "environment_name" {
  description = "Name of the environment that can be used as part of naming resource convention"
  type        = string
  validation {
    condition     = length(var.environment_name) >= 1 && length(var.environment_name) <= 64
    error_message = "Environment name must be between 1 and 64 characters."
  }
}

variable "name" {
  description = "Base name for the real-time audio agent application"
  type        = string
  default     = "artagent"
  validation {
    condition     = length(var.name) >= 1 && length(var.name) <= 20
    error_message = "Name must be between 1 and 20 characters."
  }
}

variable "location" {
  description = "Primary Azure region for app/data resources. Phase 0 preference order: UK first, then Sweden, then another European region, then US only if none of the preferred regions can support a required resource."
  type        = string
}

variable "enable_private_networking" {
  description = "Create the VNet/subnet foundation used by staging/prod private Container Apps, private endpoints, and WAF ingress. Existing public dev deployments keep this false until they migrate."
  type        = bool
  default     = false
}

variable "virtual_network_address_space" {
  description = "Address space for the application landing-zone virtual network."
  type        = list(string)
  default     = ["10.42.0.0/16"]
}

variable "app_gateway_subnet_address_prefix" {
  description = "CIDR prefix for the Application Gateway WAF subnet."
  type        = string
  default     = "10.42.0.0/24"
}

variable "container_apps_subnet_address_prefix" {
  description = "CIDR prefix for the Container Apps environment infrastructure subnet. /23 supports consumption-only environments; workload profiles can use smaller ranges later."
  type        = string
  default     = "10.42.2.0/23"
}

variable "private_endpoint_subnet_address_prefix" {
  description = "CIDR prefix for private endpoints to data, secrets, registry, and AI services."
  type        = string
  default     = "10.42.4.0/24"
}

variable "enable_ddos_network_protection" {
  description = "Attach an Azure DDoS Network Protection plan to the virtual network. Enable for production and regulated staging when budget-approved."
  type        = bool
  default     = false
}

variable "enable_private_endpoints" {
  description = "Create private DNS zones and private endpoints for platform services when private networking is enabled. Public access shutdown is handled by a later cutover flag after DNS and CI/VNet access are verified."
  type        = bool
  default     = true
}

variable "enable_application_gateway" {
  description = "Create an Application Gateway WAF_v2 origin in front of the private Container Apps environment. Requires private networking and an origin certificate secret ID."
  type        = bool
  default     = false
}

variable "app_gateway_hostname" {
  description = "Public hostname served at Cloudflare and sent as SNI/Host to the Application Gateway origin."
  type        = string
  default     = ""
}

variable "app_gateway_origin_certificate_secret_id" {
  description = "Key Vault secret URI for the App Gateway origin TLS certificate, for example https://<vault>.vault.azure.net/secrets/<name>/<version>. Use a Cloudflare Origin Certificate when Cloudflare is the only allowed origin client. Leave empty to skip App Gateway creation until the cert is provisioned."
  type        = string
  default     = ""
}

variable "app_gateway_trusted_root_certificate_base64" {
  description = "Base64-encoded public CA certificate used to validate the Container Apps origin. Leave empty to keep Application Gateway creation disabled."
  type        = string
  default     = ""
}

variable "app_gateway_backend_host_name" {
  description = "Custom Container Apps hostname that Application Gateway should send as Host/SNI for the backend API origin. Leave empty to keep using the generated Container Apps ingress FQDN."
  type        = string
  default     = ""
}

variable "app_gateway_frontend_host_name" {
  description = "Custom Container Apps hostname that Application Gateway should send as Host/SNI for the frontend origin. Leave empty to keep using the generated Container Apps ingress FQDN."
  type        = string
  default     = ""
}

variable "enable_container_app_custom_domains" {
  description = "Bind the backend and frontend Container Apps custom domains after the required DNS validation records are present. Keep false during the DNS-discovery preview."
  type        = bool
  default     = false
}

variable "container_app_custom_domain_private_dns_zone_name" {
  description = "Private DNS zone apex used for internal Container Apps custom-domain routing, for example example.com. Required when enable_container_app_custom_domains is true for an internal environment."
  type        = string
  default     = ""
}

variable "app_gateway_sku_capacity" {
  description = "Fixed Application Gateway WAF_v2 instance capacity for staging/prod. Autoscale can be added once traffic baselines are known."
  type        = number
  default     = 2
}

variable "app_gateway_request_timeout_seconds" {
  description = "Backend request timeout for long-lived voice/webhook requests through Application Gateway."
  type        = number
  default     = 300
}

variable "app_gateway_allowed_source_ipv4_cidrs" {
  description = "IPv4 CIDRs allowed to reach App Gateway HTTPS. Defaults to Cloudflare public proxy ranges for origin lockdown. Override only with an approved exception."
  type        = list(string)
  default = [
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22"
  ]
}

variable "app_gateway_allowed_source_ipv6_cidrs" {
  description = "IPv6 CIDRs allowed to reach App Gateway HTTPS. Defaults to Cloudflare public proxy ranges for origin lockdown. Override only with an approved exception."
  type        = list(string)
  default = [
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32"
  ]
}

variable "app_gateway_backend_probe_path" {
  description = "Health probe path for the backend API behind Application Gateway."
  type        = string
  default     = "/api/v1/health"
}

variable "app_gateway_frontend_probe_path" {
  description = "Health probe path for the frontend behind Application Gateway."
  type        = string
  default     = "/"
}

variable "private_endpoint_services" {
  description = "Private endpoint service set to create in the stable first pass. Cosmos Mongo vCore and Redis Enterprise remain out of this default set until their preview/azapi group IDs are confirmed in the target subscription."
  type        = set(string)
  default     = ["acr", "keyvault", "storage_blob", "appconfig", "postgres"]

  validation {
    condition = length(setsubtract(var.private_endpoint_services, [
      "acr",
      "keyvault",
      "storage_blob",
      "appconfig",
      "postgres",
    ])) == 0
    error_message = "private_endpoint_services can include only: acr, keyvault, storage_blob, appconfig, postgres."
  }
}

variable "container_apps_dedicated_workload_profile_name" {
  description = "Dedicated workload profile name for the real-time voice backend when private Container Apps networking is enabled."
  type        = string
  default     = "voice-d4"
}

variable "container_apps_dedicated_workload_profile_type" {
  description = "Dedicated workload profile type for the real-time voice backend. D4 is the smallest general-purpose dedicated profile."
  type        = string
  default     = "D4"
}

variable "container_apps_dedicated_workload_min_nodes" {
  description = "Minimum dedicated workload-profile nodes for low-latency voice workloads in private staging/prod."
  type        = number
  default     = 1
}

variable "container_apps_dedicated_workload_max_nodes" {
  description = "Maximum dedicated workload-profile nodes for low-latency voice workloads in private staging/prod."
  type        = number
  default     = 3
}

variable "openai_location" {
  description = "Optional secondary Azure OpenAI location to use if defined; will be prioritized over var.location for OpenAI resources."
  type        = string
  default     = null
}

variable "cosmosdb_location" {
  description = "Optional secondary Azure Cosmos DB location to use if defined; will be prioritized over var.location for Cosmos DB resources."
  type        = string
  default     = null
}

variable "cosmosdb_sku" {
  description = "SKU for Azure Cosmos DB (MongoDB Cluster)"
  type        = string
  default     = "M30"
}

variable "cosmosdb_public_network_access_enabled" {
  description = "Enable public network access for Cosmos DB (required for non-VNet deployments)"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Additional tags merged onto every resource. Regulated staging/prod should keep this free of policy-exemption tags unless an approved risk exception exists."
  type        = map(string)
  default     = {}
}

variable "principal_id" {
  description = "Principal ID of the user or service principal to assign application roles"
  type        = string
  default     = null
  sensitive   = true
}

variable "principal_type" {
  description = "Type of principal (User or ServicePrincipal)"
  type        = string
  default     = "User"
  validation {
    condition     = contains(["User", "ServicePrincipal"], var.principal_type)
    error_message = "Principal type must be either 'User' or 'ServicePrincipal'."
  }
}

variable "deployed_by" {
  description = "Identifier of the deployer (e.g., 'Full Name <email@domain>' or UPN). Used to tag resources for traceability."
  type        = string
  default     = null
}

variable "acs_data_location" {
  description = "Data location for Azure Communication Services"
  type        = string
  default     = "UK"
  validation {
    condition = contains([
      "United States", "Europe", "Asia Pacific", "Australia", "Brazil", "Canada",
      "France", "Germany", "India", "Japan", "Korea", "Norway", "Switzerland", "UAE", "UK"
    ], var.acs_data_location)
    error_message = "ACS data location must be a valid Azure Communication Services data location."
  }
}

variable "enable_acs_email" {
  description = "Enable Azure Communication Services Email integration (optional, not required for voice)"
  type        = bool
  default     = true # Backwards compatible - existing deployments have email resources
}

variable "disable_local_auth" {
  description = "Disable local authentication and use Azure AD/managed identity only"
  type        = bool
  default     = true
}

variable "enable_redis_ha" {
  description = "Enable Redis Enterprise High Availability for production workloads"
  type        = bool
  default     = true
}

variable "redis_sku" {
  description = "SKU for Azure Managed Redis (Enterprise) optimized for performance"
  type        = string
  default     = "MemoryOptimized_M10"
  validation {
    condition = contains([
      "MemoryOptimized_M10", "MemoryOptimized_M20", "MemoryOptimized_M50",
      "MemoryOptimized_M100", "ComputeOptimized_X5", "ComputeOptimized_X10"
    ], var.redis_sku)
    error_message = "Redis SKU must be a valid Enterprise tier SKU."
  }
}

variable "redis_port" {
  description = "Port for Azure Managed Redis"
  type        = number
  default     = 10000
}
variable "enable_voice_live" {
  description = "Enable Azure Voice Live service for real-time speech capabilities"
  type        = bool
  default     = true
}

variable "voice_live_location" {
  description = <<-EOT
    Azure region for Voice Live resources.
    Supported regions: eastus2, westus2, swedencentral, southeastasia, uksouth
    Phase 0 chose swedencentral (Voice Live was not yet in UK regions). uksouth
    verified 2026-07-09: gpt-realtime-1.5 + HD DragonHDOmni + gpt-4o-transcribe
    sidecar all render with full parity. Verify current regional availability before deployment.
    See: https://learn.microsoft.com/azure/ai-services/speech-service/regions?tabs=voice-live
  EOT
  type        = string
  default     = "swedencentral"
  validation {
    condition     = contains(["eastus2", "westus2", "swedencentral", "southeastasia", "uksouth"], var.voice_live_location)
    error_message = "Voice Live location must be one of: eastus2, westus2, swedencentral, southeastasia, uksouth. See https://learn.microsoft.com/azure/ai-services/speech-service/regions?tabs=voice-live"
  }
}

variable "voice_live_realtime_location" {
  description = <<-EOT
    Optional blue/green region for a SECOND VoiceLive realtime account (e.g. uksouth).
    When set, a new '-avlr' AIServices account is created in this region and the
    AZURE_VOICELIVE_* outputs point at it, while the original '-avl' account
    (which also hosts the governed Foundry agents) is left untouched for rollback.
    Leave empty ("") to keep the single-account layout.
  EOT
  type        = string
  default     = ""
  validation {
    condition     = var.voice_live_realtime_location == "" || contains(["eastus2", "westus2", "swedencentral", "southeastasia", "uksouth"], var.voice_live_realtime_location)
    error_message = "voice_live_realtime_location must be empty or one of: eastus2, westus2, swedencentral, southeastasia, uksouth."
  }
}

variable "voice_live_model_deployments" {
  description = "Azure OpenAI model deployments for Voice Live (real-time speech)"
  type = list(object({
    name     = string
    version  = string
    sku_name = string
    capacity = number
  }))
  default = [
    {
      name     = "gpt-realtime"
      version  = "2025-08-28"
      sku_name = "GlobalStandard"
      capacity = 4
    },
    {
      name     = "gpt-4o-transcribe"
      version  = "2025-03-20"
      sku_name = "GlobalStandard"
      capacity = 150
    },
    {
      name     = "gpt-4o-mini"
      version  = "2024-07-18"
      sku_name = "GlobalStandard"
      capacity = 10
    }
  ]
}

variable "model_deployments" {
  description = "Azure OpenAI model deployments optimized for high performance"
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
      sku_name = "DataZoneStandard"
      capacity = 150
    },
    {
      name     = "gpt-4o-mini"
      version  = "2024-07-18"
      sku_name = "DataZoneStandard"
      capacity = 150
    },
    {
      name     = "o3-mini"
      version  = "2025-01-31"
      sku_name = "DataZoneStandard"
      capacity = 50
    },
    {
      name     = "gpt-5.1"
      version  = "2025-11-13"
      sku_name = "DataZoneStandard"
      capacity = 150
    },
    {
      name     = "text-embedding-3-large"
      version  = "1"
      sku_name = "GlobalStandard"
      capacity = 100
    },
  ]
}

variable "mongo_database_name" {
  description = "Name of the MongoDB database"
  type        = string
  default     = "audioagentdb"
  validation {
    condition     = length(var.mongo_database_name) >= 1 && length(var.mongo_database_name) <= 64
    error_message = "MongoDB database name must be between 1 and 64 characters."
  }
}

variable "mongo_collection_name" {
  description = "Name of the MongoDB collection"
  type        = string
  default     = "audioagentcollection"
  validation {
    condition     = length(var.mongo_collection_name) >= 1 && length(var.mongo_collection_name) <= 64
    error_message = "MongoDB collection name must be between 1 and 64 characters."
  }
}

variable "postgres_database_name" {
  description = "Name of the Phase 0 Clinic Recall PostgreSQL database"
  type        = string
  default     = "clinic_recall_spike"
  validation {
    condition     = can(regex("^[a-zA-Z_][a-zA-Z0-9_]{0,62}$", var.postgres_database_name))
    error_message = "PostgreSQL database name must start with a letter or underscore and contain only letters, numbers, and underscores."
  }
}

variable "postgres_admin_login" {
  description = "Administrator login for the Phase 0 PostgreSQL Flexible Server"
  type        = string
  default     = "clinicrecalladmin"
  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{2,62}$", var.postgres_admin_login))
    error_message = "PostgreSQL admin login must start with a letter and be 3-63 characters."
  }
}

variable "postgres_sku_name" {
  description = "SKU for the Phase 0 PostgreSQL Flexible Server"
  type        = string
  default     = "B_Standard_B1ms"
}

variable "postgres_backup_retention_days" {
  description = "Backup retention (days) for the PostgreSQL Flexible Server. Use >= 30 with prod-grade SKUs."
  type        = number
  default     = 7
}

variable "postgres_public_network_access_enabled" {
  description = "Allow public network access to PostgreSQL. Disable in production (private networking required)."
  type        = bool
  default     = true
}

variable "log_retention_in_days" {
  description = "Log Analytics workspace retention in days."
  type        = number
  default     = 30
}

variable "postgres_storage_mb" {
  description = "Storage in MB for the Phase 0 PostgreSQL Flexible Server"
  type        = number
  default     = 32768
}

variable "enable_cardapi_cosmos_user" {
  description = "Create an Entra ID MongoDB user for the optional CardAPI MCP demo service. Disabled for Phase 0 because Clinic Recall does not depend on CardAPI and Mongo vCore user creation can fail for managed identities."
  type        = bool
  default     = false
}

variable "container_app_min_replicas" {
  description = "Minimum number of container app replicas for high availability"
  type        = number
  default     = 5
  validation {
    condition     = var.container_app_min_replicas >= 1 && var.container_app_min_replicas <= 25
    error_message = "Container app min replicas must be between 1 and 25."
  }
}

variable "container_app_max_replicas" {
  description = "Maximum number of container app replicas for auto-scaling"
  type        = number
  default     = 50
  validation {
    condition     = var.container_app_max_replicas >= 1 && var.container_app_max_replicas <= 300
    error_message = "Container app max replicas must be between 1 and 300."
  }
}

variable "container_cpu_cores" {
  description = "CPU cores allocated to each container instance"
  type        = number
  default     = 2
  validation {
    condition     = contains([0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2], var.container_cpu_cores)
    error_message = "Container CPU cores must be one of: 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2"
  }
}

variable "container_memory_gb" {
  description = "Memory in GB allocated to each container instance"
  type        = string
  default     = "4Gi"
  validation {
    condition     = contains(["0.5Gi", "1Gi", "1.5Gi", "2Gi", "2.5Gi", "3Gi", "3.5Gi", "4Gi"], var.container_memory_gb)
    error_message = "Container memory must be between 0.5Gi and 4.0Gi in 0.5Gi increments."
  }
}

variable "aoai_pool_size" {
  description = "Size of the Azure OpenAI client pool for optimal performance"
  type        = number
  default     = 50
  validation {
    condition     = var.aoai_pool_size >= 5 && var.aoai_pool_size <= 200
    error_message = "AOAI pool size must be between 5 and 200."
  }
}

variable "tts_pool_size" {
  description = "Size of the TTS client pool for optimal performance"
  type        = number
  default     = 100
  validation {
    condition     = var.tts_pool_size >= 10 && var.tts_pool_size <= 500
    error_message = "TTS pool size must be between 10 and 500."
  }
}

variable "stt_pool_size" {
  description = "Size of the STT client pool for optimal performance"
  type        = number
  default     = 100
  validation {
    condition     = var.stt_pool_size >= 10 && var.stt_pool_size <= 500
    error_message = "STT pool size must be between 10 and 500."
  }

}

variable "monitor_ci_principal_id" {
  description = "Object ID of the GitHub Actions OIDC service principal allowed to publish aggregate metrics to the data collection rule. Leave null to create the ingestion resources without granting a publisher."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.monitor_ci_principal_id == null || can(regex("^[0-9a-fA-F-]{36}$", var.monitor_ci_principal_id))
    error_message = "monitor_ci_principal_id must be a service-principal object ID formatted as a UUID."
  }
}

variable "monitor_metrics_retention_in_days" {
  description = "Interactive retention for aggregate CI, evaluation, probe, and release metrics."
  type        = number
  default     = 90

  validation {
    condition     = var.monitor_metrics_retention_in_days >= 30 && var.monitor_metrics_retention_in_days <= 730
    error_message = "monitor_metrics_retention_in_days must be between 30 and 730 days."
  }
}

variable "monitor_metrics_total_retention_in_days" {
  description = "Total retention, including archive, for aggregate observability metrics."
  type        = number
  default     = 365

  validation {
    condition     = var.monitor_metrics_total_retention_in_days >= var.monitor_metrics_retention_in_days && var.monitor_metrics_total_retention_in_days <= 4383
    error_message = "monitor_metrics_total_retention_in_days must be at least the interactive retention and no more than 4383 days."
  }
}

variable "monitor_alert_email_receivers" {
  description = "Email receivers for the Azure Monitor action group. Empty keeps alert rules active without external notifications."
  type = list(object({
    name          = string
    email_address = string
  }))
  default = []

  validation {
    condition = alltrue([
      for receiver in var.monitor_alert_email_receivers :
      length(receiver.name) >= 1 && length(receiver.name) <= 128 && can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", receiver.email_address))
    ]) && length(distinct([for receiver in var.monitor_alert_email_receivers : receiver.name])) == length(var.monitor_alert_email_receivers)
    error_message = "Each monitor alert receiver needs a unique 1-128 character name and a valid email address."
  }
}

variable "monitor_daily_token_budget" {
  description = "Optional combined daily input/output token budget. Null disables the alert."
  type        = number
  default     = null
  nullable    = true

  validation {
    condition     = var.monitor_daily_token_budget == null || var.monitor_daily_token_budget > 0
    error_message = "monitor_daily_token_budget must be null or greater than zero."
  }
}

variable "monitor_daily_estimated_cost_usd" {
  description = "Optional daily estimated AI cost budget in USD. Requires runtime token-rate environment variables. Null disables the alert."
  type        = number
  default     = null
  nullable    = true

  validation {
    condition     = var.monitor_daily_estimated_cost_usd == null || var.monitor_daily_estimated_cost_usd > 0
    error_message = "monitor_daily_estimated_cost_usd must be null or greater than zero."
  }
}
