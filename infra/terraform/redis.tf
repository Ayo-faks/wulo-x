# ============================================================================
# AZURE CACHE FOR REDIS (ENTERPRISE)
# ============================================================================
resource "azapi_resource" "redisEnterprise" {
  type                    = "Microsoft.Cache/redisEnterprise@2024-09-01-preview"
  parent_id               = azurerm_resource_group.main.id
  name                    = replace(local.resource_names.redis, "-", "")
  location                = azurerm_resource_group.main.location
  ignore_missing_property = true
  body = {
    properties = {
      highAvailability  = var.enable_redis_ha ? "Enabled" : "Disabled"
      minimumTlsVersion = "1.2"
    }
    sku = {
      name = var.redis_sku
    }
  }
  tags = local.tags

  lifecycle {
    ignore_changes = [tags]
  }
}

# Redis Enterprise Database with RBAC authentication
resource "azapi_resource" "redisDatabase" {
  type      = "Microsoft.Cache/redisEnterprise/databases@2024-09-01-preview"
  parent_id = azapi_resource.redisEnterprise.id
  name      = "default"
  body = {
    properties = {
      clientProtocol           = "Encrypted"
      clusteringPolicy         = "OSSCluster"
      evictionPolicy           = "VolatileLRU"
      port                     = var.redis_port
      accessKeysAuthentication = "Disabled"
    }
  }
  depends_on = [azapi_resource.redisEnterprise]
}

resource "azapi_resource" "backendRedisUser" {
  type      = "Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments@2024-09-01-preview"
  name      = "backendaccess"
  parent_id = azapi_resource.redisDatabase.id
  body = {
    properties = {
      accessPolicyName = "default"
      user = {
        objectId = azurerm_user_assigned_identity.backend.principal_id
      }
    }
  }
}

resource "azapi_resource" "principalRedisUser" {
  type      = "Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments@2024-09-01-preview"
  name      = "principalaccess"
  parent_id = azapi_resource.redisDatabase.id
  body = {
    properties = {
      accessPolicyName = "default"
      user = {
        objectId = data.azuread_client_config.current.object_id
      }
    }
  }
}

data "azapi_resource" "redis_enterprise_fetched" {
  type      = "Microsoft.Cache/redisEnterprise@2024-09-01-preview"
  name      = azapi_resource.redisEnterprise.name
  parent_id = azurerm_resource_group.main.id
}
