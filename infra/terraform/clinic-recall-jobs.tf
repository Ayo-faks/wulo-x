# Clinic Recall PR-01: one finite durable SMS worker.
# Resource creation and provider dispatch are independent, off-by-default gates.

variable "clinic_recall_pilot_outreach_enabled" {
  description = "Operational outreach stop layer. Keep false until a reviewed cumulative wave is active."
  type        = bool
  default     = false
}

variable "clinic_recall_pilot_voice_enabled" {
  description = "Independent operational voice stop layer. Keep false through dark validation."
  type        = bool
  default     = false
}

variable "clinic_recall_pilot_recording_enabled" {
  description = "Independent recording stop layer. PR-13 keeps this false; PR-09 owns later enablement."
  type        = bool
  default     = false
}

variable "clinic_recall_pilot_config_max_age_seconds" {
  description = "Maximum accepted age of a successful pilot App Configuration load."
  type        = number
  default     = 300

  validation {
    condition     = var.clinic_recall_pilot_config_max_age_seconds >= 1 && var.clinic_recall_pilot_config_max_age_seconds <= 3600
    error_message = "clinic_recall_pilot_config_max_age_seconds must be between 1 and 3600."
  }
}

variable "clinic_recall_pilot_environment" {
  description = "Exact environment bound to the pilot programme. Empty fails closed."
  type        = string
  default     = ""
}

variable "clinic_recall_pilot_release_identity" {
  description = "Exact reviewed release identity bound to the active pilot programme. Empty fails closed."
  type        = string
  default     = ""
}

variable "clinic_recall_durable_sms_job_enabled" {
  description = "Create the manual Clinic Recall durable SMS Job. Keep false until a reviewed staging plan is explicitly approved."
  type        = bool
  default     = false
}

variable "clinic_recall_durable_sms_dispatch_enabled" {
  description = "Permit the manual Job to claim and dispatch SMS effects. Keep false for provisioning and dark validation."
  type        = bool
  default     = false
}

variable "clinic_recall_durable_sms_worker_image" {
  description = "Exact reviewed backend image reference for the durable SMS Job. Required only when Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_durable_sms_worker_version" {
  description = "Exact source version represented by the durable SMS Job image. Required when Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_durable_sms_clinic_id" {
  description = "Internal clinic scope for the one-clinic pilot. Required only when Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_durable_sms_job_timeout_seconds" {
  description = "Maximum duration of one finite durable SMS Job replica."
  type        = number
  default     = 300

  validation {
    condition     = var.clinic_recall_durable_sms_job_timeout_seconds >= 60 && var.clinic_recall_durable_sms_job_timeout_seconds <= 900
    error_message = "clinic_recall_durable_sms_job_timeout_seconds must be between 60 and 900."
  }
}

variable "clinic_recall_durable_sms_batch_limit" {
  description = "Maximum effects claimed by one finite durable SMS Job execution."
  type        = number
  default     = 10

  validation {
    condition     = var.clinic_recall_durable_sms_batch_limit >= 1 && var.clinic_recall_durable_sms_batch_limit <= 50
    error_message = "clinic_recall_durable_sms_batch_limit must be between 1 and 50."
  }
}

resource "azurerm_container_app_job" "clinic_recall_sms" {
  count = var.clinic_recall_durable_sms_job_enabled ? 1 : 0

  name                         = "cr-sms-${local.resource_token}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = var.enable_private_networking ? "Consumption" : null

  replica_timeout_in_seconds = var.clinic_recall_durable_sms_job_timeout_seconds
  replica_retry_limit        = 0

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.backend.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.backend.id
  }

  template {
    container {
      name    = "clinic-recall-sms-worker"
      image   = var.clinic_recall_durable_sms_worker_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "src.clinic_recall.durable.worker"]
      args = [
        "--clinic-id",
        var.clinic_recall_durable_sms_clinic_id,
        "--limit",
        tostring(var.clinic_recall_durable_sms_batch_limit),
      ]

      env {
        name  = "CLINIC_RECALL_DURABLE_SMS_ENABLED"
        value = tostring(var.clinic_recall_durable_sms_dispatch_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_OUTREACH_ENABLED"
        value = tostring(var.clinic_recall_pilot_outreach_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_VOICE_ENABLED"
        value = tostring(var.clinic_recall_pilot_voice_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_RECORDING_ENABLED"
        value = tostring(var.clinic_recall_pilot_recording_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS"
        value = tostring(var.clinic_recall_pilot_config_max_age_seconds)
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
        name  = "AZURE_APPCONFIG_ENDPOINT"
        value = module.appconfig.endpoint
      }

      env {
        name  = "AZURE_APPCONFIG_LABEL"
        value = var.environment_name
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.backend.client_id
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }

      env {
        name  = "SERVICE_NAME"
        value = "clinic-recall-sms-worker"
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
        value = var.clinic_recall_durable_sms_worker_version
      }

      env {
        name  = "GIT_SHA"
        value = var.clinic_recall_durable_sms_worker_version
      }

      env {
        name  = "PYTHONUNBUFFERED"
        value = "1"
      }
    }
  }

  tags = merge(local.tags, {
    "clinic-recall-role" = "durable-sms-worker"
  })

  lifecycle {
    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_worker_image) != "" && startswith(var.clinic_recall_durable_sms_worker_image, "${azurerm_container_registry.main.login_server}/")
      error_message = "Job creation requires an exact image from this environment's Azure Container Registry."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_worker_version) != ""
      error_message = "Job creation requires the exact source version represented by the reviewed image."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_clinic_id) != ""
      error_message = "Job creation requires the reviewed internal one-clinic pilot scope."
    }

    precondition {
      condition     = !var.clinic_recall_durable_sms_dispatch_enabled || (trimspace(var.clinic_recall_pilot_environment) != "" && trimspace(var.clinic_recall_pilot_release_identity) != "")
      error_message = "SMS dispatch requires an exact pilot environment and release identity."
    }
  }

  depends_on = [
    azurerm_role_assignment.acr_backend_pull,
    azurerm_role_assignment.keyvault_backend_secrets,
    module.appconfig,
  ]
}

variable "clinic_recall_cadence_job_enabled" {
  description = "Create the scheduled Clinic Recall planner Job. Keep false until a separately reviewed plan is approved."
  type        = bool
  default     = false
}

variable "clinic_recall_cadence_execution_enabled" {
  description = "Permit the scheduled Job to plan durable effects. Independent from resource creation and SMS dispatch."
  type        = bool
  default     = false
}

variable "clinic_recall_cadence_schedule_utc" {
  description = "Five-field UTC cron expression for the finite cadence planner."
  type        = string
  default     = "0 * * * *"

  validation {
    condition     = can(regex("^\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S+$", trimspace(var.clinic_recall_cadence_schedule_utc)))
    error_message = "clinic_recall_cadence_schedule_utc must be a five-field UTC cron expression."
  }
}

variable "clinic_recall_cadence_config_refreshed_at" {
  description = "Reviewed RFC3339 UTC timestamp for the cadence operational configuration. Empty fails closed."
  type        = string
  default     = ""

  validation {
    condition     = trimspace(var.clinic_recall_cadence_config_refreshed_at) == "" || can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.clinic_recall_cadence_config_refreshed_at))
    error_message = "clinic_recall_cadence_config_refreshed_at must be empty or a valid RFC3339 timestamp."
  }
}

variable "clinic_recall_cadence_config_max_age_seconds" {
  description = "Maximum accepted age of the cadence operational configuration."
  type        = number
  default     = 300

  validation {
    condition     = var.clinic_recall_cadence_config_max_age_seconds >= 1 && var.clinic_recall_cadence_config_max_age_seconds <= 3600
    error_message = "clinic_recall_cadence_config_max_age_seconds must be between 1 and 3600."
  }
}

variable "clinic_recall_cadence_job_timeout_seconds" {
  description = "Maximum duration of one finite cadence planner replica."
  type        = number
  default     = 300

  validation {
    condition     = var.clinic_recall_cadence_job_timeout_seconds >= 60 && var.clinic_recall_cadence_job_timeout_seconds <= 900
    error_message = "clinic_recall_cadence_job_timeout_seconds must be between 60 and 900."
  }
}

variable "clinic_recall_cadence_batch_limit" {
  description = "Maximum jobs inspected by each planner in one execution."
  type        = number
  default     = 50

  validation {
    condition     = var.clinic_recall_cadence_batch_limit >= 1 && var.clinic_recall_cadence_batch_limit <= 100
    error_message = "clinic_recall_cadence_batch_limit must be between 1 and 100."
  }
}

variable "clinic_recall_cadence_window_minutes" {
  description = "Maximum UTC cursor catch-up window for one execution."
  type        = number
  default     = 60

  validation {
    condition     = var.clinic_recall_cadence_window_minutes >= 1 && var.clinic_recall_cadence_window_minutes <= 1440
    error_message = "clinic_recall_cadence_window_minutes must be between 1 and 1440."
  }
}

resource "azurerm_container_app_job" "clinic_recall_planner" {
  count = var.clinic_recall_cadence_job_enabled ? 1 : 0

  name                         = "cr-cadence-${local.resource_token}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = var.enable_private_networking ? "Consumption" : null

  replica_timeout_in_seconds = var.clinic_recall_cadence_job_timeout_seconds
  replica_retry_limit        = 0

  schedule_trigger_config {
    cron_expression          = var.clinic_recall_cadence_schedule_utc
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.backend.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.backend.id
  }

  template {
    container {
      name    = "clinic-recall-cadence-planner"
      image   = var.clinic_recall_durable_sms_worker_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "src.clinic_recall.durable.planner"]
      args = [
        "--clinic-id",
        var.clinic_recall_durable_sms_clinic_id,
        "--batch-size",
        tostring(var.clinic_recall_cadence_batch_limit),
        "--window-minutes",
        tostring(var.clinic_recall_cadence_window_minutes),
      ]

      env {
        name  = "CLINIC_RECALL_CADENCE_PLANNING_ENABLED"
        value = tostring(var.clinic_recall_cadence_execution_enabled)
      }

      env {
        name  = "CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT"
        value = var.clinic_recall_cadence_config_refreshed_at
      }

      env {
        name  = "CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS"
        value = tostring(var.clinic_recall_cadence_config_max_age_seconds)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_OUTREACH_ENABLED"
        value = tostring(var.clinic_recall_pilot_outreach_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_VOICE_ENABLED"
        value = tostring(var.clinic_recall_pilot_voice_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_RECORDING_ENABLED"
        value = tostring(var.clinic_recall_pilot_recording_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS"
        value = tostring(var.clinic_recall_pilot_config_max_age_seconds)
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
        name  = "AZURE_APPCONFIG_ENDPOINT"
        value = module.appconfig.endpoint
      }

      env {
        name  = "AZURE_APPCONFIG_LABEL"
        value = var.environment_name
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.backend.client_id
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }

      env {
        name  = "SERVICE_NAME"
        value = "clinic-recall-cadence-planner"
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
        value = var.clinic_recall_durable_sms_worker_version
      }

      env {
        name  = "GIT_SHA"
        value = var.clinic_recall_durable_sms_worker_version
      }

      env {
        name  = "PYTHONUNBUFFERED"
        value = "1"
      }
    }
  }

  tags = merge(local.tags, {
    "clinic-recall-role" = "cadence-planner"
  })

  lifecycle {
    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_worker_image) != "" && startswith(var.clinic_recall_durable_sms_worker_image, "${azurerm_container_registry.main.login_server}/")
      error_message = "Planner Job creation requires an exact image from this environment's Azure Container Registry."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_worker_version) != ""
      error_message = "Planner Job creation requires the exact source version represented by the reviewed image."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_clinic_id) != ""
      error_message = "Planner Job creation requires the reviewed internal one-clinic pilot scope."
    }

    precondition {
      condition     = !var.clinic_recall_cadence_execution_enabled || trimspace(var.clinic_recall_cadence_config_refreshed_at) != ""
      error_message = "Cadence execution requires an explicit reviewed configuration refresh timestamp."
    }

    precondition {
      condition     = !var.clinic_recall_cadence_execution_enabled || (trimspace(var.clinic_recall_pilot_environment) != "" && trimspace(var.clinic_recall_pilot_release_identity) != "")
      error_message = "Cadence execution requires an exact pilot environment and release identity."
    }
  }

  depends_on = [
    azurerm_role_assignment.acr_backend_pull,
    azurerm_role_assignment.keyvault_backend_secrets,
    module.appconfig,
  ]
}

variable "clinic_recall_durable_call_job_enabled" {
  description = "Create the manual Clinic Recall durable CALL Job. Keep false until a separately reviewed staging plan is approved."
  type        = bool
  default     = false
}

variable "clinic_recall_durable_call_dispatch_enabled" {
  description = "Permit the manual CALL Job to claim and dispatch CALL effects. Independent from resource creation and every other worker gate."
  type        = bool
  default     = false
}

variable "clinic_recall_durable_call_worker_image" {
  description = "Exact reviewed backend image reference for the durable CALL Job. Required only when CALL Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_durable_call_worker_version" {
  description = "Exact source version represented by the durable CALL Job image. Required when CALL Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_durable_call_clinic_id" {
  description = "Internal clinic scope for the durable CALL worker. Required only when CALL Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_durable_call_job_timeout_seconds" {
  description = "Maximum duration of one finite durable CALL Job replica."
  type        = number
  default     = 300

  validation {
    condition     = var.clinic_recall_durable_call_job_timeout_seconds >= 60 && var.clinic_recall_durable_call_job_timeout_seconds <= 900
    error_message = "clinic_recall_durable_call_job_timeout_seconds must be between 60 and 900."
  }
}

variable "clinic_recall_durable_call_batch_limit" {
  description = "Maximum CALL effects claimed by one finite Job execution."
  type        = number
  default     = 10

  validation {
    condition     = var.clinic_recall_durable_call_batch_limit >= 1 && var.clinic_recall_durable_call_batch_limit <= 50
    error_message = "clinic_recall_durable_call_batch_limit must be between 1 and 50."
  }
}

resource "azurerm_container_app_job" "clinic_recall_call" {
  count = var.clinic_recall_durable_call_job_enabled ? 1 : 0

  name                         = "cr-call-${local.resource_token}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = var.enable_private_networking ? "Consumption" : null

  replica_timeout_in_seconds = var.clinic_recall_durable_call_job_timeout_seconds
  replica_retry_limit        = 0

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.backend.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.backend.id
  }

  template {
    container {
      name    = "clinic-recall-call-worker"
      image   = var.clinic_recall_durable_call_worker_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "src.clinic_recall.durable.call_worker"]
      args = [
        "--clinic-id",
        var.clinic_recall_durable_call_clinic_id,
        "--limit",
        tostring(var.clinic_recall_durable_call_batch_limit),
      ]

      env {
        name  = "CLINIC_RECALL_DURABLE_CALL_ENABLED"
        value = tostring(var.clinic_recall_durable_call_dispatch_enabled)
      }

      env {
        name  = "CLINIC_RECALL_DURABLE_CALL_PROVIDER"
        value = "twilio"
      }

      env {
        name  = "CLINIC_RECALL_PILOT_OUTREACH_ENABLED"
        value = tostring(var.clinic_recall_pilot_outreach_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_VOICE_ENABLED"
        value = tostring(var.clinic_recall_pilot_voice_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_RECORDING_ENABLED"
        value = tostring(var.clinic_recall_pilot_recording_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS"
        value = tostring(var.clinic_recall_pilot_config_max_age_seconds)
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
        name  = "AZURE_APPCONFIG_ENDPOINT"
        value = module.appconfig.endpoint
      }

      env {
        name  = "AZURE_APPCONFIG_LABEL"
        value = var.environment_name
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.backend.client_id
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }

      env {
        name  = "SERVICE_NAME"
        value = "clinic-recall-call-worker"
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
        value = var.clinic_recall_durable_call_worker_version
      }

      env {
        name  = "GIT_SHA"
        value = var.clinic_recall_durable_call_worker_version
      }

      env {
        name  = "PYTHONUNBUFFERED"
        value = "1"
      }
    }
  }

  tags = merge(local.tags, {
    "clinic-recall-role" = "durable-call-worker"
  })

  lifecycle {
    precondition {
      condition     = trimspace(var.clinic_recall_durable_call_worker_image) != "" && startswith(var.clinic_recall_durable_call_worker_image, "${azurerm_container_registry.main.login_server}/")
      error_message = "CALL Job creation requires an exact image from this environment's Azure Container Registry."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_call_worker_version) != ""
      error_message = "CALL Job creation requires the exact source version represented by the reviewed image."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_call_clinic_id) != ""
      error_message = "CALL Job creation requires the reviewed internal one-clinic pilot scope."
    }

    precondition {
      condition     = !var.clinic_recall_durable_call_dispatch_enabled || (trimspace(var.clinic_recall_pilot_environment) != "" && trimspace(var.clinic_recall_pilot_release_identity) != "")
      error_message = "CALL dispatch requires an exact pilot environment and release identity."
    }
  }

  depends_on = [
    azurerm_role_assignment.acr_backend_pull,
    azurerm_role_assignment.keyvault_backend_secrets,
    module.appconfig,
  ]
}

variable "clinic_recall_durable_recording_job_enabled" {
  description = "Create the manual Clinic Recall durable recording Job. Keep false pending wording, privacy, carrier, and deployment approval."
  type        = bool
  default     = false
}

variable "clinic_recall_durable_recording_dispatch_enabled" {
  description = "Permit the manual recording Job to claim start and stop effects. Independent from resource creation and every other worker gate."
  type        = bool
  default     = false
}

variable "clinic_recall_durable_recording_worker_image" {
  description = "Exact reviewed backend image reference for the durable recording Job. Required only when Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_durable_recording_worker_version" {
  description = "Exact source version represented by the durable recording Job image. Required when Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_durable_recording_clinic_id" {
  description = "Internal clinic scope for the durable recording worker. Required only when Job creation is enabled."
  type        = string
  default     = ""
}

variable "clinic_recall_durable_recording_job_timeout_seconds" {
  description = "Maximum duration of one finite durable recording Job replica."
  type        = number
  default     = 300

  validation {
    condition     = var.clinic_recall_durable_recording_job_timeout_seconds >= 60 && var.clinic_recall_durable_recording_job_timeout_seconds <= 900
    error_message = "clinic_recall_durable_recording_job_timeout_seconds must be between 60 and 900."
  }
}

variable "clinic_recall_durable_recording_batch_limit" {
  description = "Maximum recording effects claimed by one finite Job execution."
  type        = number
  default     = 10

  validation {
    condition     = var.clinic_recall_durable_recording_batch_limit >= 1 && var.clinic_recall_durable_recording_batch_limit <= 50
    error_message = "clinic_recall_durable_recording_batch_limit must be between 1 and 50."
  }
}

variable "clinic_recall_durable_recording_schedule_utc" {
  description = "UTC cron schedule for finite recording recovery and switch-off enforcement."
  type        = string
  default     = "* * * * *"

  validation {
    condition     = length(split(" ", trimspace(var.clinic_recall_durable_recording_schedule_utc))) == 5
    error_message = "clinic_recall_durable_recording_schedule_utc must contain five cron fields."
  }
}

resource "azurerm_container_app_job" "clinic_recall_recording" {
  count = var.clinic_recall_durable_recording_job_enabled ? 1 : 0

  name                         = "cr-recording-${local.resource_token}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = var.enable_private_networking ? "Consumption" : null

  replica_timeout_in_seconds = var.clinic_recall_durable_recording_job_timeout_seconds
  replica_retry_limit        = 0

  schedule_trigger_config {
    cron_expression          = var.clinic_recall_durable_recording_schedule_utc
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.backend.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.backend.id
  }

  template {
    container {
      name    = "clinic-recall-recording-worker"
      image   = var.clinic_recall_durable_recording_worker_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "src.clinic_recall.durable.recording_worker"]
      args = [
        "--clinic-id",
        var.clinic_recall_durable_recording_clinic_id,
        "--limit",
        tostring(var.clinic_recall_durable_recording_batch_limit),
      ]

      env {
        name  = "CLINIC_RECALL_DURABLE_RECORDING_ENABLED"
        value = tostring(var.clinic_recall_durable_recording_dispatch_enabled)
      }

      env {
        name  = "CLINIC_RECALL_DURABLE_RECORDING_PROVIDER"
        value = "twilio"
      }

      env {
        name  = "CLINIC_RECALL_PILOT_OUTREACH_ENABLED"
        value = tostring(var.clinic_recall_pilot_outreach_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_VOICE_ENABLED"
        value = tostring(var.clinic_recall_pilot_voice_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_RECORDING_ENABLED"
        value = tostring(var.clinic_recall_pilot_recording_enabled)
      }

      env {
        name  = "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS"
        value = tostring(var.clinic_recall_pilot_config_max_age_seconds)
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
        name  = "AZURE_APPCONFIG_ENDPOINT"
        value = module.appconfig.endpoint
      }

      env {
        name  = "AZURE_APPCONFIG_LABEL"
        value = var.environment_name
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.backend.client_id
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }

      env {
        name  = "SERVICE_NAME"
        value = "clinic-recall-recording-worker"
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
        value = var.clinic_recall_durable_recording_worker_version
      }

      env {
        name  = "GIT_SHA"
        value = var.clinic_recall_durable_recording_worker_version
      }

      env {
        name  = "PYTHONUNBUFFERED"
        value = "1"
      }
    }
  }

  tags = merge(local.tags, {
    "clinic-recall-role" = "durable-recording-worker"
  })

  lifecycle {
    precondition {
      condition     = trimspace(var.clinic_recall_durable_recording_worker_image) != "" && startswith(var.clinic_recall_durable_recording_worker_image, "${azurerm_container_registry.main.login_server}/")
      error_message = "Recording Job creation requires an exact image from this environment's Azure Container Registry."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_recording_worker_version) != ""
      error_message = "Recording Job creation requires the exact source version represented by the reviewed image."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_recording_clinic_id) != ""
      error_message = "Recording Job creation requires the reviewed internal one-clinic pilot scope."
    }

    precondition {
      condition     = !var.clinic_recall_durable_recording_dispatch_enabled || (trimspace(var.clinic_recall_pilot_environment) != "" && trimspace(var.clinic_recall_pilot_release_identity) != "")
      error_message = "Recording dispatch requires an exact pilot environment and release identity."
    }
  }

  depends_on = [
    azurerm_role_assignment.acr_backend_pull,
    azurerm_role_assignment.keyvault_backend_secrets,
    module.appconfig,
  ]
}

variable "clinic_recall_handoff_ageing_job_enabled" {
  description = "Create the scheduled PR-12 handoff ageing Job. Keep false until clinic SLA and on-call authority are approved."
  type        = bool
  default     = false
}

variable "clinic_recall_handoff_ageing_execution_enabled" {
  description = "Permit the ageing Job to request alternate paging and pause breached programmes. Independent from resource creation."
  type        = bool
  default     = false
}

variable "clinic_recall_handoff_ageing_schedule_utc" {
  description = "Five-field UTC cron expression for the finite PR-12 ageing pass."
  type        = string
  default     = "*/5 * * * *"

  validation {
    condition     = can(regex("^\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S+$", trimspace(var.clinic_recall_handoff_ageing_schedule_utc)))
    error_message = "clinic_recall_handoff_ageing_schedule_utc must be a five-field UTC cron expression."
  }
}

variable "clinic_recall_handoff_ageing_job_timeout_seconds" {
  description = "Maximum duration of one finite handoff ageing Job replica."
  type        = number
  default     = 300

  validation {
    condition     = var.clinic_recall_handoff_ageing_job_timeout_seconds >= 60 && var.clinic_recall_handoff_ageing_job_timeout_seconds <= 900
    error_message = "clinic_recall_handoff_ageing_job_timeout_seconds must be between 60 and 900."
  }
}

variable "clinic_recall_handoff_ageing_batch_limit" {
  description = "Maximum due receipts locked by one finite ageing pass."
  type        = number
  default     = 50

  validation {
    condition     = var.clinic_recall_handoff_ageing_batch_limit >= 1 && var.clinic_recall_handoff_ageing_batch_limit <= 250
    error_message = "clinic_recall_handoff_ageing_batch_limit must be between 1 and 250."
  }
}

resource "azurerm_container_app_job" "clinic_recall_handoff_ageing" {
  count = var.clinic_recall_handoff_ageing_job_enabled ? 1 : 0

  name                         = "cr-handoff-age-${local.resource_token}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = var.enable_private_networking ? "Consumption" : null

  replica_timeout_in_seconds = var.clinic_recall_handoff_ageing_job_timeout_seconds
  replica_retry_limit        = 0

  schedule_trigger_config {
    cron_expression          = var.clinic_recall_handoff_ageing_schedule_utc
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.backend.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.backend.id
  }

  template {
    container {
      name    = "clinic-recall-handoff-ageing"
      image   = var.clinic_recall_durable_sms_worker_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "src.clinic_recall.handoff_ageing"]
      args = [
        "--clinic-id",
        var.clinic_recall_durable_sms_clinic_id,
        "--limit",
        tostring(var.clinic_recall_handoff_ageing_batch_limit),
      ]

      env {
        name  = "CLINIC_RECALL_HANDOFF_AGEING_ENABLED"
        value = tostring(var.clinic_recall_handoff_ageing_execution_enabled)
      }

      env {
        name  = "AZURE_APPCONFIG_ENDPOINT"
        value = module.appconfig.endpoint
      }

      env {
        name  = "AZURE_APPCONFIG_LABEL"
        value = var.environment_name
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.backend.client_id
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }

      env {
        name  = "SERVICE_NAME"
        value = "clinic-recall-handoff-ageing"
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
        value = var.clinic_recall_durable_sms_worker_version
      }

      env {
        name  = "GIT_SHA"
        value = var.clinic_recall_durable_sms_worker_version
      }

      env {
        name  = "PYTHONUNBUFFERED"
        value = "1"
      }
    }
  }

  tags = merge(local.tags, {
    "clinic-recall-role" = "handoff-ageing"
  })

  lifecycle {
    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_worker_image) != "" && startswith(var.clinic_recall_durable_sms_worker_image, "${azurerm_container_registry.main.login_server}/")
      error_message = "Handoff ageing Job creation requires an exact image from this environment's Azure Container Registry."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_worker_version) != ""
      error_message = "Handoff ageing Job creation requires the exact source version represented by the reviewed image."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_clinic_id) != ""
      error_message = "Handoff ageing Job creation requires the reviewed internal one-clinic pilot scope."
    }
  }

  depends_on = [
    azurerm_role_assignment.acr_backend_pull,
    azurerm_role_assignment.keyvault_backend_secrets,
    module.appconfig,
  ]
}

variable "clinic_recall_operational_snapshot_job_enabled" {
  description = "Create the scheduled PR-14 read-only operational snapshot Job. Keep false until observability deployment is approved."
  type        = bool
  default     = false
}

variable "clinic_recall_operational_snapshot_execution_enabled" {
  description = "Permit the PR-14 Job to query tenant-scoped aggregates and emit telemetry. Independent from resource creation."
  type        = bool
  default     = false
}

variable "clinic_recall_operational_snapshot_schedule_utc" {
  description = "Five-field UTC cron expression for the finite PR-14 snapshot pass."
  type        = string
  default     = "*/5 * * * *"

  validation {
    condition     = can(regex("^\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S+$", trimspace(var.clinic_recall_operational_snapshot_schedule_utc)))
    error_message = "clinic_recall_operational_snapshot_schedule_utc must be a five-field UTC cron expression."
  }
}

variable "clinic_recall_operational_snapshot_job_timeout_seconds" {
  description = "Maximum duration of one finite PR-14 snapshot Job replica."
  type        = number
  default     = 300

  validation {
    condition     = var.clinic_recall_operational_snapshot_job_timeout_seconds >= 60 && var.clinic_recall_operational_snapshot_job_timeout_seconds <= 900
    error_message = "clinic_recall_operational_snapshot_job_timeout_seconds must be between 60 and 900."
  }
}

variable "clinic_recall_operational_snapshot_lookback_hours" {
  description = "Bounded recency window for terminal-state evidence in one PR-14 snapshot."
  type        = number
  default     = 24

  validation {
    condition     = var.clinic_recall_operational_snapshot_lookback_hours >= 1 && var.clinic_recall_operational_snapshot_lookback_hours <= 168
    error_message = "clinic_recall_operational_snapshot_lookback_hours must be between 1 and 168."
  }
}

resource "azurerm_container_app_job" "clinic_recall_operational_snapshot" {
  count = var.clinic_recall_operational_snapshot_job_enabled ? 1 : 0

  name                         = "cr-ops-snapshot-${local.resource_token}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = var.enable_private_networking ? "Consumption" : null

  replica_timeout_in_seconds = var.clinic_recall_operational_snapshot_job_timeout_seconds
  replica_retry_limit        = 0

  schedule_trigger_config {
    cron_expression          = var.clinic_recall_operational_snapshot_schedule_utc
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.backend.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.backend.id
  }

  template {
    container {
      name    = "clinic-recall-operational-snapshot"
      image   = var.clinic_recall_durable_sms_worker_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "src.clinic_recall.operational_snapshot"]
      args = [
        "--clinic-id",
        var.clinic_recall_durable_sms_clinic_id,
        "--lookback-hours",
        tostring(var.clinic_recall_operational_snapshot_lookback_hours),
      ]

      env {
        name  = "CLINIC_RECALL_OPERATIONAL_SNAPSHOT_ENABLED"
        value = tostring(var.clinic_recall_operational_snapshot_execution_enabled)
      }

      env {
        name  = "AZURE_APPCONFIG_ENDPOINT"
        value = module.appconfig.endpoint
      }

      env {
        name  = "AZURE_APPCONFIG_LABEL"
        value = var.environment_name
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.backend.client_id
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }

      env {
        name  = "SERVICE_NAME"
        value = "clinic-recall-operational-snapshot"
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
        value = var.clinic_recall_durable_sms_worker_version
      }

      env {
        name  = "GIT_SHA"
        value = var.clinic_recall_durable_sms_worker_version
      }

      env {
        name  = "PYTHONUNBUFFERED"
        value = "1"
      }
    }
  }

  tags = merge(local.tags, {
    "clinic-recall-role" = "operational-snapshot"
  })

  lifecycle {
    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_worker_image) != "" && startswith(var.clinic_recall_durable_sms_worker_image, "${azurerm_container_registry.main.login_server}/")
      error_message = "Operational snapshot Job creation requires an exact image from this environment's Azure Container Registry."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_worker_version) != ""
      error_message = "Operational snapshot Job creation requires the exact source version represented by the reviewed image."
    }

    precondition {
      condition     = trimspace(var.clinic_recall_durable_sms_clinic_id) != ""
      error_message = "Operational snapshot Job creation requires the reviewed internal one-clinic pilot scope."
    }
  }

  depends_on = [
    azurerm_role_assignment.acr_backend_pull,
    azurerm_role_assignment.keyvault_backend_secrets,
    module.appconfig,
  ]
}

output "CLINIC_RECALL_DURABLE_SMS_JOB_NAME" {
  description = "Manual durable SMS Job name, or null while resource creation is disabled."
  value       = try(azurerm_container_app_job.clinic_recall_sms[0].name, null)
}

output "CLINIC_RECALL_CADENCE_JOB_NAME" {
  description = "Scheduled cadence planner Job name, or null while resource creation is disabled."
  value       = try(azurerm_container_app_job.clinic_recall_planner[0].name, null)
}

output "CLINIC_RECALL_DURABLE_CALL_JOB_NAME" {
  description = "Manual durable CALL Job name, or null while resource creation is disabled."
  value       = try(azurerm_container_app_job.clinic_recall_call[0].name, null)
}

output "CLINIC_RECALL_DURABLE_RECORDING_JOB_NAME" {
  description = "Manual durable recording Job name, or null while resource creation is disabled."
  value       = try(azurerm_container_app_job.clinic_recall_recording[0].name, null)
}

output "CLINIC_RECALL_HANDOFF_AGEING_JOB_NAME" {
  description = "Scheduled handoff ageing Job name, or null while resource creation is disabled."
  value       = try(azurerm_container_app_job.clinic_recall_handoff_ageing[0].name, null)
}

output "CLINIC_RECALL_OPERATIONAL_SNAPSHOT_JOB_NAME" {
  description = "Scheduled PR-14 operational snapshot Job name, or null while resource creation is disabled."
  value       = try(azurerm_container_app_job.clinic_recall_operational_snapshot[0].name, null)
}

output "CLINIC_RECALL_PILOT_ENVIRONMENT" {
  description = "Exact environment bound to the Clinic Recall pilot controls."
  value       = var.clinic_recall_pilot_environment
}

output "CLINIC_RECALL_PILOT_RELEASE_IDENTITY" {
  description = "Reviewed release identity bound to the Clinic Recall pilot controls."
  value       = var.clinic_recall_pilot_release_identity
}
