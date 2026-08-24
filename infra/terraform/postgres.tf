# ============================================================================
# POSTGRESQL FLEXIBLE SERVER (PHASE 0 SPIKE)
# ============================================================================

resource "random_password" "postgres_admin" {
  length           = 24
  special          = true
  override_special = "!#$^*()-_=+"
}

resource "azurerm_postgresql_flexible_server" "clinic_recall" {
  name                          = local.resource_names.postgres
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  version                       = "16"
  administrator_login           = var.postgres_admin_login
  administrator_password        = random_password.postgres_admin.result
  sku_name                      = var.postgres_sku_name
  storage_mb                    = var.postgres_storage_mb
  public_network_access_enabled = var.postgres_public_network_access_enabled
  backup_retention_days         = var.postgres_backup_retention_days
  tags                          = local.tags

  lifecycle {
    ignore_changes = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "clinic_recall" {
  name      = var.postgres_database_name
  server_id = azurerm_postgresql_flexible_server.clinic_recall.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_azure_services" {
  name             = "allow-azure-services"
  server_id        = azurerm_postgresql_flexible_server.clinic_recall.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

resource "azurerm_key_vault_secret" "postgres_admin_password" {
  name            = "postgres-admin-password"
  value           = random_password.postgres_admin.result
  key_vault_id    = azurerm_key_vault.main.id
  content_type    = "text/plain"
  expiration_date = timeadd(timestamp(), "720h") # 30 days

  lifecycle {
    ignore_changes = [expiration_date]
  }

  depends_on = [azurerm_role_assignment.keyvault_admin]
}

resource "azurerm_key_vault_secret" "postgres_connection_string" {
  name = "postgres-connection-string"
  value = format(
    "host=%s port=5432 dbname=%s user=%s password=%s sslmode=require",
    azurerm_postgresql_flexible_server.clinic_recall.fqdn,
    azurerm_postgresql_flexible_server_database.clinic_recall.name,
    var.postgres_admin_login,
    random_password.postgres_admin.result,
  )
  key_vault_id    = azurerm_key_vault.main.id
  content_type    = "text/plain"
  expiration_date = timeadd(timestamp(), "720h") # 30 days

  lifecycle {
    ignore_changes = [expiration_date]
  }

  depends_on = [
    azurerm_postgresql_flexible_server_database.clinic_recall,
    azurerm_role_assignment.keyvault_admin,
  ]
}