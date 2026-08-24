variable "clinic_recall_audit_db_role_enabled" {
  description = "Provision the ordinary SELECT-only PostgreSQL role used for release inventory."
  type        = bool
  default     = true
}

variable "clinic_recall_audit_db_password_rotation_epoch" {
  description = "Explicit rotation marker for the release-inventory PostgreSQL credential."
  type        = string
  default     = "v1"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$", var.clinic_recall_audit_db_password_rotation_epoch))
    error_message = "clinic_recall_audit_db_password_rotation_epoch is invalid."
  }
}

resource "random_password" "clinic_recall_audit_db" {
  count = var.clinic_recall_audit_db_role_enabled ? 1 : 0

  length           = 32
  special          = true
  override_special = "!#$^*()-_=+"
  keepers = {
    rotation_epoch = var.clinic_recall_audit_db_password_rotation_epoch
  }
}

resource "azurerm_key_vault_secret" "clinic_recall_audit_db_password" {
  count = var.clinic_recall_audit_db_role_enabled ? 1 : 0

  name         = "clinic-recall-audit-db-password"
  value        = random_password.clinic_recall_audit_db[0].result
  key_vault_id = azurerm_key_vault.main.id
  content_type = "text/plain"

  depends_on = [azurerm_role_assignment.keyvault_admin]
}

resource "azurerm_key_vault_secret" "clinic_recall_audit_db_connection" {
  count = var.clinic_recall_audit_db_role_enabled ? 1 : 0

  name = "clinic-recall-audit-db-connection-string"
  value = format(
    "host=%s port=5432 dbname=%s user=clinic_recall_audit password=%s sslmode=require",
    azurerm_postgresql_flexible_server.clinic_recall.fqdn,
    azurerm_postgresql_flexible_server_database.clinic_recall.name,
    random_password.clinic_recall_audit_db[0].result,
  )
  key_vault_id = azurerm_key_vault.main.id
  content_type = "text/plain"

  depends_on = [
    azurerm_key_vault_secret.clinic_recall_audit_db_password,
    azurerm_postgresql_flexible_server_database.clinic_recall,
  ]
}

output "CLINIC_RECALL_AUDIT_DB_ROLE_ENABLED" {
  description = "Whether the SELECT-only release inventory role is provisioned."
  value       = var.clinic_recall_audit_db_role_enabled
}

output "CLINIC_RECALL_AUDIT_DB_PASSWORD_ROTATION_EPOCH" {
  description = "Rotation marker for the SELECT-only release inventory credential."
  value       = var.clinic_recall_audit_db_password_rotation_epoch
}