# Clinic Recall PR-10: finite privacy-rights dispatch/reconciliation and
# retention scheduling. Resource creation and runtime activation are separate,
# default-false gates. No policy duration or approval evidence has a default.

variable "clinic_recall_rights_job_enabled" {
  description = "Create the scheduled Clinic Recall rights Job. Keep false until preparation and review are complete."
  type        = bool
  default     = false
}

variable "clinic_recall_privacy_db_role_ready" {
  description = "Set true only after postprovision verifies the dedicated PostgreSQL role is LOGIN, NOSUPERUSER, and NOBYPASSRLS."
  type        = bool
  default     = false
}

variable "clinic_recall_privacy_db_role_ready_epoch" {
  description = "Password rotation epoch last applied and verified by postprovision. Empty fails closed."
  type        = string
  default     = ""
}

variable "clinic_recall_privacy_db_password_rotation_epoch" {
  description = "Explicit rotation marker for the dedicated privacy Job database credential. Bump only with a reviewed rollout."
  type        = string
  default     = "v1"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$", var.clinic_recall_privacy_db_password_rotation_epoch))
    error_message = "clinic_recall_privacy_db_password_rotation_epoch is invalid."
  }
}

variable "clinic_recall_rights_dispatch_enabled" {
  description = "Permit the rights Job to claim, delete, minimize, and reconcile targets. Keep false through dark validation."
  type        = bool
  default     = false
}

variable "clinic_recall_rights_twilio_enabled" {
  description = "Make the Twilio rights adapter available to the finite Job. This does not activate the Job."
  type        = bool
  default     = false
}

variable "clinic_recall_rights_blob_enabled" {
  description = "Make the Azure Blob rights adapter available to the finite Job. This does not activate the Job."
  type        = bool
  default     = false
}

variable "clinic_recall_rights_worker_image" {
  description = "Exact reviewed backend image reference for the rights Job. Required only when Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_rights_worker_version" {
  description = "Exact source version represented by the rights Job image."
  type        = string
  default     = ""
}

variable "clinic_recall_rights_clinic_id" {
  description = "Internal one-clinic scope for the finite rights Job."
  type        = string
  default     = ""
}

variable "clinic_recall_rights_job_timeout_seconds" {
  description = "Maximum duration of one finite rights Job replica."
  type        = number
  default     = 600

  validation {
    condition     = var.clinic_recall_rights_job_timeout_seconds >= 60 && var.clinic_recall_rights_job_timeout_seconds <= 900
    error_message = "clinic_recall_rights_job_timeout_seconds must be between 60 and 900."
  }
}

variable "clinic_recall_rights_batch_limit" {
  description = "Maximum rights targets inspected by each dispatch and reconciliation pass."
  type        = number
  default     = 10

  validation {
    condition     = var.clinic_recall_rights_batch_limit >= 1 && var.clinic_recall_rights_batch_limit <= 50
    error_message = "clinic_recall_rights_batch_limit must be between 1 and 50."
  }
}

variable "clinic_recall_rights_max_target_attempts" {
  description = "Finite maximum destructive attempts per target after read-before-retry reconciliation."
  type        = number
  default     = 2

  validation {
    condition     = var.clinic_recall_rights_max_target_attempts >= 1 && var.clinic_recall_rights_max_target_attempts <= 10
    error_message = "clinic_recall_rights_max_target_attempts must be between 1 and 10."
  }
}

variable "clinic_recall_rights_schedule_utc" {
  description = "UTC cron schedule for one finite dispatch pass followed by one reconciliation pass."
  type        = string
  default     = "*/5 * * * *"

  validation {
    condition     = length(split(" ", trimspace(var.clinic_recall_rights_schedule_utc))) == 5
    error_message = "clinic_recall_rights_schedule_utc must contain five cron fields."
  }
}

variable "clinic_recall_rights_hmac_key_version" {
  description = "Version of the pre-provisioned clinic-recall-rights-hmac-key Key Vault secret. Empty blocks activation."
  type        = string
  default     = ""
}

variable "clinic_recall_rights_policy_version" {
  description = "Reviewed rights policy version bound to new requests. No default authority is supplied."
  type        = string
  default     = ""
}

variable "clinic_recall_rights_approval_evidence_sha256" {
  description = "Lowercase SHA-256 digest of reviewed rights-policy approval evidence."
  type        = string
  default     = ""
}

variable "clinic_recall_rights_request_due_seconds" {
  description = "Explicit reviewed request deadline in seconds. Zero means unconfigured and blocks activation."
  type        = number
  default     = 0

  validation {
    condition     = var.clinic_recall_rights_request_due_seconds >= 0
    error_message = "clinic_recall_rights_request_due_seconds cannot be negative."
  }
}

variable "clinic_recall_rights_residual_approvals_json" {
  description = "Reviewed residual approvals keyed by the closed residual category enum. Empty means no approval and blocks dispatch activation."
  type        = string
  default     = ""
}

variable "clinic_recall_retention_job_enabled" {
  description = "Create the scheduled Clinic Recall retention inventory Job. Keep false until policy review is complete."
  type        = bool
  default     = false
}

variable "clinic_recall_retention_scheduler_enabled" {
  description = "Permit the retention Job to inventory due content into durable rights effects."
  type        = bool
  default     = false
}

variable "clinic_recall_retention_worker_image" {
  description = "Exact reviewed backend image reference for the retention Job. Required only when Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_retention_worker_version" {
  description = "Exact source version represented by the retention Job image."
  type        = string
  default     = ""
}

variable "clinic_recall_retention_clinic_id" {
  description = "Internal one-clinic scope for the finite retention scheduler."
  type        = string
  default     = ""
}

variable "clinic_recall_retention_job_timeout_seconds" {
  description = "Maximum duration of one finite retention Job replica."
  type        = number
  default     = 300

  validation {
    condition     = var.clinic_recall_retention_job_timeout_seconds >= 60 && var.clinic_recall_retention_job_timeout_seconds <= 900
    error_message = "clinic_recall_retention_job_timeout_seconds must be between 60 and 900."
  }
}

variable "clinic_recall_retention_batch_limit" {
  description = "Maximum due interactions inventoried by one finite retention Job execution."
  type        = number
  default     = 100

  validation {
    condition     = var.clinic_recall_retention_batch_limit >= 1 && var.clinic_recall_retention_batch_limit <= 1000
    error_message = "clinic_recall_retention_batch_limit must be between 1 and 1000."
  }
}

variable "clinic_recall_retention_schedule_utc" {
  description = "UTC cron schedule for durable retention inventory."
  type        = string
  default     = "15 1 * * *"

  validation {
    condition     = length(split(" ", trimspace(var.clinic_recall_retention_schedule_utc))) == 5
    error_message = "clinic_recall_retention_schedule_utc must contain five cron fields."
  }
}

variable "clinic_recall_retention_policy_version" {
  description = "Reviewed retention policy version. No default legal authority is supplied."
  type        = string
  default     = ""
}

variable "clinic_recall_retention_approval_evidence_sha256" {
  description = "Lowercase SHA-256 digest of reviewed retention-policy approval evidence."
  type        = string
  default     = ""
}

variable "clinic_recall_retention_policy_approved_at" {
  description = "Timezone-aware RFC3339 approval timestamp for the reviewed retention policy."
  type        = string
  default     = ""
}

variable "clinic_recall_retention_policy_effective_at" {
  description = "Timezone-aware RFC3339 effective timestamp for the reviewed retention policy."
  type        = string
  default     = ""
}

variable "clinic_recall_retention_policy_expires_at" {
  description = "Timezone-aware RFC3339 expiry timestamp for the reviewed retention policy."
  type        = string
  default     = ""
}

variable "clinic_recall_retention_retain_for_seconds" {
  description = "Explicit reviewed content-retention duration in seconds. Zero means unconfigured."
  type        = number
  default     = 0

  validation {
    condition     = var.clinic_recall_retention_retain_for_seconds >= 0
    error_message = "clinic_recall_retention_retain_for_seconds cannot be negative."
  }
}

variable "clinic_recall_retention_request_due_seconds" {
  description = "Explicit reviewed deadline for each generated minimization request. Zero means unconfigured."
  type        = number
  default     = 0

  validation {
    condition     = var.clinic_recall_retention_request_due_seconds >= 0
    error_message = "clinic_recall_retention_request_due_seconds cannot be negative."
  }
}

locals {
  clinic_recall_privacy_jobs_enabled = (
    var.clinic_recall_rights_job_enabled || var.clinic_recall_retention_job_enabled
  )
  clinic_recall_privacy_job_common_env = {
    AZURE_APPCONFIG_ENDPOINT              = module.appconfig.endpoint
    AZURE_APPCONFIG_LABEL                 = var.environment_name
    APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.main.connection_string
    SERVICE_NAMESPACE                     = "clinic-recall"
    ENVIRONMENT                           = var.environment_name
    PYTHONUNBUFFERED                      = "1"
  }
}

resource "random_password" "clinic_recall_privacy_db" {
  count = local.clinic_recall_privacy_jobs_enabled ? 1 : 0

  length           = 32
  special          = true
  override_special = "!#$^*()-_=+"
  keepers = {
    rotation_epoch = var.clinic_recall_privacy_db_password_rotation_epoch
  }
}

resource "azurerm_key_vault_secret" "clinic_recall_privacy_db_password" {
  count = local.clinic_recall_privacy_jobs_enabled ? 1 : 0

  name         = "clinic-recall-privacy-db-password"
  value        = random_password.clinic_recall_privacy_db[0].result
  key_vault_id = azurerm_key_vault.main.id
  content_type = "text/plain"

  depends_on = [azurerm_role_assignment.keyvault_admin]
}

resource "azurerm_key_vault_secret" "clinic_recall_privacy_db_connection" {
  count = local.clinic_recall_privacy_jobs_enabled ? 1 : 0

  name = "clinic-recall-privacy-db-connection-string"
  value = format(
    "host=%s port=5432 dbname=%s user=clinic_recall_privacy password=%s sslmode=require",
    azurerm_postgresql_flexible_server.clinic_recall.fqdn,
    azurerm_postgresql_flexible_server_database.clinic_recall.name,
    random_password.clinic_recall_privacy_db[0].result,
  )
  key_vault_id = azurerm_key_vault.main.id
  content_type = "text/plain"

  depends_on = [
    azurerm_key_vault_secret.clinic_recall_privacy_db_password,
    azurerm_postgresql_flexible_server_database.clinic_recall,
  ]
}

resource "azurerm_user_assigned_identity" "clinic_recall_rights" {
  count = var.clinic_recall_rights_job_enabled ? 1 : 0

  name                = "${var.name}-rights-${local.resource_token}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_user_assigned_identity" "clinic_recall_retention" {
  count = var.clinic_recall_retention_job_enabled ? 1 : 0

  name                = "${var.name}-retention-${local.resource_token}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_role_assignment" "clinic_recall_rights_acr_pull" {
  count = var.clinic_recall_rights_job_enabled ? 1 : 0

  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.clinic_recall_rights[0].principal_id
}

resource "azurerm_role_assignment" "clinic_recall_rights_appconfig_reader" {
  count = var.clinic_recall_rights_job_enabled ? 1 : 0

  scope                = module.appconfig.id
  role_definition_name = "App Configuration Data Reader"
  principal_id         = azurerm_user_assigned_identity.clinic_recall_rights[0].principal_id
}

resource "azurerm_role_assignment" "clinic_recall_rights_keyvault_secrets" {
  count = var.clinic_recall_rights_job_enabled ? 1 : 0

  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.clinic_recall_rights[0].principal_id
}

resource "azurerm_role_assignment" "clinic_recall_rights_recordings_owner" {
  count = var.clinic_recall_rights_job_enabled ? 1 : 0

  scope                = azurerm_storage_container.call_recordings.id
  role_definition_name = "Storage Blob Data Owner"
  principal_id         = azurerm_user_assigned_identity.clinic_recall_rights[0].principal_id
}

resource "azurerm_role_assignment" "clinic_recall_retention_acr_pull" {
  count = var.clinic_recall_retention_job_enabled ? 1 : 0

  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.clinic_recall_retention[0].principal_id
}

resource "azurerm_role_assignment" "clinic_recall_retention_appconfig_reader" {
  count = var.clinic_recall_retention_job_enabled ? 1 : 0

  scope                = module.appconfig.id
  role_definition_name = "App Configuration Data Reader"
  principal_id         = azurerm_user_assigned_identity.clinic_recall_retention[0].principal_id
}

resource "azurerm_role_assignment" "clinic_recall_retention_keyvault_secrets" {
  count = var.clinic_recall_retention_job_enabled ? 1 : 0

  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.clinic_recall_retention[0].principal_id
}

resource "azurerm_container_app_job" "clinic_recall_rights" {
  count = var.clinic_recall_rights_job_enabled ? 1 : 0

  name                         = "cr-rights-${local.resource_token}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = var.enable_private_networking ? "Consumption" : null

  replica_timeout_in_seconds = var.clinic_recall_rights_job_timeout_seconds
  replica_retry_limit        = 0

  schedule_trigger_config {
    cron_expression          = var.clinic_recall_rights_schedule_utc
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.clinic_recall_rights[0].id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.clinic_recall_rights[0].id
  }

  secret {
    name                = "privacy-postgres"
    identity            = azurerm_user_assigned_identity.clinic_recall_rights[0].id
    key_vault_secret_id = azurerm_key_vault_secret.clinic_recall_privacy_db_connection[0].versionless_id
  }

  template {
    container {
      name    = "clinic-recall-rights-worker"
      image   = var.clinic_recall_rights_worker_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "src.clinic_recall.durable.rights_worker"]
      args = [
        "--clinic-id",
        var.clinic_recall_rights_clinic_id,
        "--mode",
        "both",
        "--limit",
        tostring(var.clinic_recall_rights_batch_limit),
        "--max-target-attempts",
        tostring(var.clinic_recall_rights_max_target_attempts),
      ]

      env {
        name  = "CLINIC_RECALL_DURABLE_RIGHTS_ENABLED"
        value = tostring(var.clinic_recall_rights_dispatch_enabled)
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.clinic_recall_rights[0].client_id
      }

      env {
        name        = "CLINIC_RECALL_PRIVACY_DATABASE_URL"
        secret_name = "privacy-postgres"
      }

      env {
        name  = "SERVICE_NAME"
        value = "clinic-recall-rights-worker"
      }

      env {
        name  = "SERVICE_VERSION"
        value = var.clinic_recall_rights_worker_version
      }

      env {
        name  = "GIT_SHA"
        value = var.clinic_recall_rights_worker_version
      }

      dynamic "env" {
        for_each = local.clinic_recall_privacy_job_common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = merge(local.tags, {
    "clinic-recall-role" = "durable-rights-worker"
  })

  lifecycle {
    precondition {
      condition     = trimspace(var.clinic_recall_rights_worker_image) != "" && startswith(var.clinic_recall_rights_worker_image, "${azurerm_container_registry.main.login_server}/")
      error_message = "Rights Job creation requires an exact image from this environment's Azure Container Registry."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_rights_worker_version) != "" && trimspace(var.clinic_recall_rights_clinic_id) != ""
      error_message = "Rights Job creation requires an exact source version and reviewed clinic scope."
    }

    precondition {
      condition     = !var.clinic_recall_rights_dispatch_enabled || (var.clinic_recall_rights_twilio_enabled && var.clinic_recall_rights_blob_enabled)
      error_message = "Rights dispatch requires both reviewed Twilio and Azure Blob adapters."
    }

    precondition {
      condition = !var.clinic_recall_rights_dispatch_enabled || (
        var.clinic_recall_privacy_db_role_ready &&
        var.clinic_recall_privacy_db_role_ready_epoch == var.clinic_recall_privacy_db_password_rotation_epoch &&
        can(regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$", var.clinic_recall_rights_hmac_key_version)) &&
        can(regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", var.clinic_recall_rights_policy_version)) &&
        can(regex("^[0-9a-f]{64}$", var.clinic_recall_rights_approval_evidence_sha256)) &&
        var.clinic_recall_rights_request_due_seconds > 0 &&
        try(length(jsondecode(var.clinic_recall_rights_residual_approvals_json)), 0) > 0
      )
      error_message = "Rights dispatch requires versioned HMAC and reviewed rights-policy metadata; the HMAC value must already exist in Key Vault."
    }
  }

  depends_on = [
    azurerm_role_assignment.clinic_recall_rights_acr_pull,
    azurerm_role_assignment.clinic_recall_rights_appconfig_reader,
    azurerm_role_assignment.clinic_recall_rights_keyvault_secrets,
    azurerm_role_assignment.clinic_recall_rights_recordings_owner,
  ]
}

resource "azurerm_container_app_job" "clinic_recall_retention" {
  count = var.clinic_recall_retention_job_enabled ? 1 : 0

  name                         = "cr-retention-${local.resource_token}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = var.enable_private_networking ? "Consumption" : null

  replica_timeout_in_seconds = var.clinic_recall_retention_job_timeout_seconds
  replica_retry_limit        = 0

  schedule_trigger_config {
    cron_expression          = var.clinic_recall_retention_schedule_utc
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.clinic_recall_retention[0].id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.clinic_recall_retention[0].id
  }

  secret {
    name                = "privacy-postgres"
    identity            = azurerm_user_assigned_identity.clinic_recall_retention[0].id
    key_vault_secret_id = azurerm_key_vault_secret.clinic_recall_privacy_db_connection[0].versionless_id
  }

  template {
    container {
      name    = "clinic-recall-retention-scheduler"
      image   = var.clinic_recall_retention_worker_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "src.clinic_recall.durable.retention_scheduler"]
      args = [
        "--clinic-id",
        var.clinic_recall_retention_clinic_id,
        "--limit",
        tostring(var.clinic_recall_retention_batch_limit),
      ]

      env {
        name  = "CLINIC_RECALL_RETENTION_SCHEDULER_ENABLED"
        value = tostring(var.clinic_recall_retention_scheduler_enabled)
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.clinic_recall_retention[0].client_id
      }

      env {
        name        = "CLINIC_RECALL_PRIVACY_DATABASE_URL"
        secret_name = "privacy-postgres"
      }

      env {
        name  = "SERVICE_NAME"
        value = "clinic-recall-retention-scheduler"
      }

      env {
        name  = "SERVICE_VERSION"
        value = var.clinic_recall_retention_worker_version
      }

      env {
        name  = "GIT_SHA"
        value = var.clinic_recall_retention_worker_version
      }

      dynamic "env" {
        for_each = local.clinic_recall_privacy_job_common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = merge(local.tags, {
    "clinic-recall-role" = "retention-scheduler"
  })

  lifecycle {
    precondition {
      condition     = trimspace(var.clinic_recall_retention_worker_image) != "" && startswith(var.clinic_recall_retention_worker_image, "${azurerm_container_registry.main.login_server}/")
      error_message = "Retention Job creation requires an exact image from this environment's Azure Container Registry."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_retention_worker_version) != "" && trimspace(var.clinic_recall_retention_clinic_id) != ""
      error_message = "Retention Job creation requires an exact source version and reviewed clinic scope."
    }

    precondition {
      condition = !var.clinic_recall_retention_scheduler_enabled || (
        var.clinic_recall_privacy_db_role_ready &&
        var.clinic_recall_privacy_db_role_ready_epoch == var.clinic_recall_privacy_db_password_rotation_epoch &&
        can(regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$", var.clinic_recall_rights_hmac_key_version)) &&
        can(regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", var.clinic_recall_retention_policy_version)) &&
        can(regex("^[0-9a-f]{64}$", var.clinic_recall_retention_approval_evidence_sha256)) &&
        can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.clinic_recall_retention_policy_approved_at)) &&
        can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.clinic_recall_retention_policy_effective_at)) &&
        can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.clinic_recall_retention_policy_expires_at)) &&
        var.clinic_recall_retention_retain_for_seconds > 0 &&
        var.clinic_recall_retention_request_due_seconds > 0
      )
      error_message = "Retention activation requires versioned HMAC metadata and a complete timezone-aware reviewed retention policy; the HMAC value must already exist in Key Vault."
    }
  }

  depends_on = [
    azurerm_role_assignment.clinic_recall_retention_acr_pull,
    azurerm_role_assignment.clinic_recall_retention_appconfig_reader,
    azurerm_role_assignment.clinic_recall_retention_keyvault_secrets,
  ]
}

output "CLINIC_RECALL_RIGHTS_JOB_NAME" {
  description = "Scheduled rights Job name, or null while resource creation is disabled."
  value       = try(azurerm_container_app_job.clinic_recall_rights[0].name, null)
}

output "CLINIC_RECALL_RETENTION_JOB_NAME" {
  description = "Scheduled retention Job name, or null while resource creation is disabled."
  value       = try(azurerm_container_app_job.clinic_recall_retention[0].name, null)
}

output "CLINIC_RECALL_DURABLE_RIGHTS_TWILIO_ENABLED" {
  description = "Non-secret Twilio adapter availability synchronized to App Configuration."
  value       = tostring(var.clinic_recall_rights_twilio_enabled)
}

output "CLINIC_RECALL_DURABLE_RIGHTS_BLOB_ENABLED" {
  description = "Non-secret Blob adapter availability synchronized to App Configuration."
  value       = tostring(var.clinic_recall_rights_blob_enabled)
}

output "CLINIC_RECALL_RIGHTS_HMAC_KEY_VERSION" {
  description = "Version of the rights HMAC secret; the secret value remains only in Key Vault."
  value       = var.clinic_recall_rights_hmac_key_version
}

output "CLINIC_RECALL_RIGHTS_POLICY_VERSION" {
  description = "Reviewed rights policy version synchronized to App Configuration."
  value       = var.clinic_recall_rights_policy_version
}

output "CLINIC_RECALL_RIGHTS_APPROVAL_EVIDENCE_SHA256" {
  description = "Digest of reviewed rights-policy approval evidence."
  value       = var.clinic_recall_rights_approval_evidence_sha256
}

output "CLINIC_RECALL_RIGHTS_REQUEST_DUE_SECONDS" {
  description = "Explicit reviewed rights-request deadline in seconds."
  value       = var.clinic_recall_rights_request_due_seconds > 0 ? tostring(var.clinic_recall_rights_request_due_seconds) : ""
}

output "CLINIC_RECALL_RIGHTS_RESIDUAL_APPROVALS_JSON" {
  description = "Reviewed bounded residual approvals synchronized to App Configuration."
  value       = var.clinic_recall_rights_residual_approvals_json
}

output "CLINIC_RECALL_RETENTION_POLICY_VERSION" {
  description = "Reviewed retention policy version synchronized to App Configuration."
  value       = var.clinic_recall_retention_policy_version
}

output "CLINIC_RECALL_RETENTION_APPROVAL_EVIDENCE_SHA256" {
  description = "Digest of reviewed retention-policy approval evidence."
  value       = var.clinic_recall_retention_approval_evidence_sha256
}

output "CLINIC_RECALL_RETENTION_POLICY_APPROVED_AT" {
  description = "Reviewed retention-policy approval timestamp."
  value       = var.clinic_recall_retention_policy_approved_at
}

output "CLINIC_RECALL_RETENTION_POLICY_EFFECTIVE_AT" {
  description = "Reviewed retention-policy effective timestamp."
  value       = var.clinic_recall_retention_policy_effective_at
}

output "CLINIC_RECALL_RETENTION_POLICY_EXPIRES_AT" {
  description = "Reviewed retention-policy expiry timestamp."
  value       = var.clinic_recall_retention_policy_expires_at
}

output "CLINIC_RECALL_RETENTION_RETAIN_FOR_SECONDS" {
  description = "Explicit reviewed content-retention duration in seconds."
  value       = var.clinic_recall_retention_retain_for_seconds > 0 ? tostring(var.clinic_recall_retention_retain_for_seconds) : ""
}

output "CLINIC_RECALL_RETENTION_REQUEST_DUE_SECONDS" {
  description = "Explicit reviewed deadline for generated retention requests."
  value       = var.clinic_recall_retention_request_due_seconds > 0 ? tostring(var.clinic_recall_retention_request_due_seconds) : ""
}

output "CLINIC_RECALL_PRIVACY_DB_PASSWORD_ROTATION_EPOCH" {
  description = "Credential rotation epoch that postprovision must apply before activation."
  value       = var.clinic_recall_privacy_db_password_rotation_epoch
}