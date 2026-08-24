# ============================================================================
# CONSENTED CALL RECORDINGS — private blob container + retention + RBAC
# ============================================================================
# Audio for consented calls is copied here by the recording-status webhook and
# deleted from the telephony provider immediately after (UK data residency).
# The container is private; access is via the backend's managed identity only.

resource "azurerm_storage_container" "call_recordings" {
  name                  = "call-recordings"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

# Default retention for recording audio. Override per environment with
# var.call_recording_retention_days; clinics with stricter policies are
# enforced at the application layer.
variable "call_recording_retention_days" {
  description = "Days to retain consented call recordings in blob storage before automatic deletion."
  type        = number
  default     = 30
}

resource "azurerm_storage_management_policy" "call_recordings_retention" {
  storage_account_id = azurerm_storage_account.main.id

  rule {
    name    = "call-recordings-retention"
    enabled = true
    filters {
      prefix_match = ["${azurerm_storage_container.call_recordings.name}/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = var.call_recording_retention_days
      }
    }
  }
}

# Backend write/read access comes from the existing account-scoped
# "Storage Blob Data Contributor" assignment in data.tf
# (azurerm_role_assignment.storage_backend_contributor) — no extra RBAC needed.
