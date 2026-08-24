# ============================================================================
# NETWORK FOUNDATION
# ============================================================================
# Optional private landing-zone network for regulated staging/prod. Keeping this
# behind enable_private_networking lets the current public dev environment remain
# stable while the private ingress and private endpoint slices are introduced.
# ============================================================================

resource "azurerm_network_ddos_protection_plan" "main" {
  count = var.enable_private_networking && var.enable_ddos_network_protection ? 1 : 0

  name                = local.resource_names.ddos_plan
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_virtual_network" "main" {
  count = var.enable_private_networking ? 1 : 0

  name                = local.resource_names.virtual_network
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = var.virtual_network_address_space
  tags                = local.tags

  dynamic "ddos_protection_plan" {
    for_each = var.enable_ddos_network_protection ? [azurerm_network_ddos_protection_plan.main[0].id] : []

    content {
      id     = ddos_protection_plan.value
      enable = true
    }
  }
}

resource "azurerm_network_security_group" "app_gateway" {
  count = var.enable_private_networking ? 1 : 0

  name                = local.resource_names.nsg_app_gateway
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags

  security_rule {
    name                       = "AllowHttpsFromCloudflareIpv4"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefixes    = var.app_gateway_allowed_source_ipv4_cidrs
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowHttpsFromCloudflareIpv6"
    priority                   = 101
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefixes    = var.app_gateway_allowed_source_ipv6_cidrs
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowGatewayManager"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "65200-65535"
    source_address_prefix      = "GatewayManager"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowAzureLoadBalancer"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "AzureLoadBalancer"
    destination_address_prefix = "*"
  }
}

resource "azurerm_network_security_group" "container_apps" {
  count = var.enable_private_networking ? 1 : 0

  name                = local.resource_names.nsg_container_apps
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags

  security_rule {
    name                       = "AllowAppGatewayInbound"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["80", "443"]
    source_address_prefix      = var.app_gateway_subnet_address_prefix
    destination_address_prefix = "*"
  }
}

resource "azurerm_network_security_group" "private_endpoints" {
  count = var.enable_private_networking ? 1 : 0

  name                = local.resource_names.nsg_private_endpoints
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_subnet" "app_gateway" {
  count = var.enable_private_networking ? 1 : 0

  name                 = local.resource_names.subnet_app_gateway
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main[0].name
  address_prefixes     = [var.app_gateway_subnet_address_prefix]
}

resource "azurerm_subnet" "container_apps" {
  count = var.enable_private_networking ? 1 : 0

  name                 = local.resource_names.subnet_container_apps
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main[0].name
  address_prefixes     = [var.container_apps_subnet_address_prefix]

  delegation {
    name = "container-apps-environment"

    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "private_endpoints" {
  count = var.enable_private_networking ? 1 : 0

  name                              = local.resource_names.subnet_private_endpoints
  resource_group_name               = azurerm_resource_group.main.name
  virtual_network_name              = azurerm_virtual_network.main[0].name
  address_prefixes                  = [var.private_endpoint_subnet_address_prefix]
  private_endpoint_network_policies = "NetworkSecurityGroupEnabled"
}

resource "azurerm_subnet_network_security_group_association" "app_gateway" {
  count = var.enable_private_networking ? 1 : 0

  subnet_id                 = azurerm_subnet.app_gateway[0].id
  network_security_group_id = azurerm_network_security_group.app_gateway[0].id
}

resource "azurerm_subnet_network_security_group_association" "container_apps" {
  count = var.enable_private_networking ? 1 : 0

  subnet_id                 = azurerm_subnet.container_apps[0].id
  network_security_group_id = azurerm_network_security_group.container_apps[0].id
}

resource "azurerm_subnet_network_security_group_association" "private_endpoints" {
  count = var.enable_private_networking ? 1 : 0

  subnet_id                 = azurerm_subnet.private_endpoints[0].id
  network_security_group_id = azurerm_network_security_group.private_endpoints[0].id
}

# ============================================================================
# PRIVATE DNS ZONES AND ENDPOINTS
# ============================================================================
# These endpoints give private-networked staging/prod a working private data
# plane before public access is disabled. Public access lockdown happens after
# CI/VNet access, DNS resolution, and service-specific runtime paths are proven.
# ============================================================================

locals {
  private_dns_zone_names = local.should_create_private_endpoints ? {
    for service, zone in {
      acr          = "privatelink.azurecr.io"
      keyvault     = "privatelink.vaultcore.azure.net"
      storage_blob = "privatelink.blob.core.windows.net"
      appconfig    = "privatelink.azconfig.io"
      postgres     = "privatelink.postgres.database.azure.com"
    } : service => zone if contains(var.private_endpoint_services, service)
  } : {}
}

resource "azurerm_private_dns_zone" "main" {
  for_each = local.private_dns_zone_names

  name                = each.value
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "main" {
  for_each = azurerm_private_dns_zone.main

  name                  = "link-${each.key}-${var.environment_name}-${local.resource_token}"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = each.value.name
  virtual_network_id    = azurerm_virtual_network.main[0].id
  registration_enabled  = false
  tags                  = local.tags
}

resource "azurerm_private_dns_zone" "container_apps" {
  count = var.enable_private_networking ? 1 : 0

  name                = "internal.${azurerm_container_app_environment.main.default_domain}"
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "container_apps" {
  count = var.enable_private_networking ? 1 : 0

  name                  = "link-aca-${var.environment_name}-${local.resource_token}"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.container_apps[0].name
  virtual_network_id    = azurerm_virtual_network.main[0].id
  registration_enabled  = false
  tags                  = local.tags
}

resource "azurerm_private_dns_a_record" "container_apps_wildcard" {
  count = var.enable_private_networking ? 1 : 0

  name                = "*"
  zone_name           = azurerm_private_dns_zone.container_apps[0].name
  resource_group_name = azurerm_resource_group.main.name
  ttl                 = 60
  records             = [azurerm_container_app_environment.main.static_ip_address]
  tags                = local.tags
}

resource "azurerm_private_dns_zone" "container_apps_default" {
  count = var.enable_private_networking ? 1 : 0

  name                = azurerm_container_app_environment.main.default_domain
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "container_apps_default" {
  count = var.enable_private_networking ? 1 : 0

  name                  = "link-aca-default-${var.environment_name}-${local.resource_token}"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.container_apps_default[0].name
  virtual_network_id    = azurerm_virtual_network.main[0].id
  registration_enabled  = false
  tags                  = local.tags
}

resource "azurerm_private_dns_a_record" "container_apps_default_wildcard" {
  count = var.enable_private_networking ? 1 : 0

  name                = "*"
  zone_name           = azurerm_private_dns_zone.container_apps_default[0].name
  resource_group_name = azurerm_resource_group.main.name
  ttl                 = 60
  records             = [azurerm_container_app_environment.main.static_ip_address]
  tags                = local.tags
}

resource "azurerm_private_dns_zone" "container_app_custom_domain" {
  count = local.should_create_container_app_custom_domain_private_dns ? 1 : 0

  name                = var.container_app_custom_domain_private_dns_zone_name
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "container_app_custom_domain" {
  count = local.should_create_container_app_custom_domain_private_dns ? 1 : 0

  name                  = "link-aca-custom-${var.environment_name}-${local.resource_token}"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.container_app_custom_domain[0].name
  virtual_network_id    = azurerm_virtual_network.main[0].id
  registration_enabled  = false
  tags                  = local.tags
}

resource "azurerm_private_dns_a_record" "container_app_custom_domain_backend" {
  count = local.should_create_container_app_custom_domain_private_dns ? 1 : 0

  name                = trimsuffix(var.app_gateway_backend_host_name, ".${var.container_app_custom_domain_private_dns_zone_name}")
  zone_name           = azurerm_private_dns_zone.container_app_custom_domain[0].name
  resource_group_name = azurerm_resource_group.main.name
  ttl                 = 60
  records             = [azurerm_container_app_environment.main.static_ip_address]
  tags                = local.tags
}

resource "azurerm_private_dns_a_record" "container_app_custom_domain_frontend" {
  count = local.should_create_container_app_custom_domain_private_dns ? 1 : 0

  name                = trimsuffix(var.app_gateway_frontend_host_name, ".${var.container_app_custom_domain_private_dns_zone_name}")
  zone_name           = azurerm_private_dns_zone.container_app_custom_domain[0].name
  resource_group_name = azurerm_resource_group.main.name
  ttl                 = 60
  records             = [azurerm_container_app_environment.main.static_ip_address]
  tags                = local.tags
}

resource "azurerm_private_dns_a_record" "container_app_custom_domain_public_frontend" {
  count = local.should_create_container_app_custom_domain_private_dns ? 1 : 0

  name                = trimsuffix(var.app_gateway_hostname, ".${var.container_app_custom_domain_private_dns_zone_name}")
  zone_name           = azurerm_private_dns_zone.container_app_custom_domain[0].name
  resource_group_name = azurerm_resource_group.main.name
  ttl                 = 60
  records             = [azurerm_container_app_environment.main.static_ip_address]
  tags                = local.tags
}

resource "azurerm_private_endpoint" "acr" {
  count = local.should_create_private_endpoints && contains(var.private_endpoint_services, "acr") ? 1 : 0

  name                = local.resource_names.pe_acr
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints[0].id
  tags                = local.tags

  private_service_connection {
    name                           = "psc-acr-${var.environment_name}"
    private_connection_resource_id = azurerm_container_registry.main.id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.main["acr"].id]
  }
}

resource "azurerm_private_endpoint" "keyvault" {
  count = local.should_create_private_endpoints && contains(var.private_endpoint_services, "keyvault") ? 1 : 0

  name                = local.resource_names.pe_keyvault
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints[0].id
  tags                = local.tags

  private_service_connection {
    name                           = "psc-kv-${var.environment_name}"
    private_connection_resource_id = azurerm_key_vault.main.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.main["keyvault"].id]
  }
}

resource "azurerm_private_endpoint" "storage_blob" {
  count = local.should_create_private_endpoints && contains(var.private_endpoint_services, "storage_blob") ? 1 : 0

  name                = local.resource_names.pe_storage_blob
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints[0].id
  tags                = local.tags

  private_service_connection {
    name                           = "psc-stblob-${var.environment_name}"
    private_connection_resource_id = azurerm_storage_account.main.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.main["storage_blob"].id]
  }
}

resource "azurerm_private_endpoint" "appconfig" {
  count = local.should_create_private_endpoints && contains(var.private_endpoint_services, "appconfig") ? 1 : 0

  name                = local.resource_names.pe_appconfig
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints[0].id
  tags                = local.tags

  private_service_connection {
    name                           = "psc-appcfg-${var.environment_name}"
    private_connection_resource_id = module.appconfig.id
    subresource_names              = ["configurationStores"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.main["appconfig"].id]
  }
}

resource "azurerm_private_endpoint" "postgres" {
  count = local.should_create_private_endpoints && contains(var.private_endpoint_services, "postgres") ? 1 : 0

  name                = local.resource_names.pe_postgres
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints[0].id
  tags                = local.tags

  private_service_connection {
    name                           = "psc-psql-${var.environment_name}"
    private_connection_resource_id = azurerm_postgresql_flexible_server.clinic_recall.id
    subresource_names              = ["postgresqlServer"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.main["postgres"].id]
  }
}

# ============================================================================
# CLOUDFLARE ORIGIN: APPLICATION GATEWAY WAF
# ============================================================================
# The external DNS provider owns the public certificate for the configured host. The
# Application Gateway listener is the Azure origin and uses an origin TLS
# certificate stored in Key Vault. Creation is skipped until that secret ID is
# supplied, which prevents an invalid HTTPS listener during early staging setup.
# ============================================================================

resource "azurerm_user_assigned_identity" "app_gateway" {
  count = local.should_create_application_gateway ? 1 : 0

  name                = local.resource_names.app_gateway_identity
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_role_assignment" "keyvault_app_gateway_secrets" {
  count = local.should_create_application_gateway ? 1 : 0

  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app_gateway[0].principal_id
}

resource "azurerm_public_ip" "app_gateway" {
  count = local.should_create_application_gateway ? 1 : 0

  name                = local.resource_names.app_gateway_public_ip
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.tags
}

resource "azurerm_web_application_firewall_policy" "app_gateway" {
  count = local.should_create_application_gateway ? 1 : 0

  name                = local.resource_names.app_gateway_waf_policy
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.tags

  custom_rules {
    name      = "AllowEasyAuthProviderCallbackPost"
    priority  = 10
    rule_type = "MatchRule"
    action    = "Allow"

    match_conditions {
      match_variables {
        variable_name = "RequestUri"
      }

      operator           = "BeginsWith"
      negation_condition = false
      # OAuth callbacks can trip managed OWASP rules (double-encoded state,
      # form_post bodies); allow only the EasyAuth provider callback paths.
      match_values = [
        "/.auth/login/aad/callback",
        "/.auth/login/google/callback",
      ]
    }

    match_conditions {
      match_variables {
        variable_name = "RequestMethod"
      }

      operator           = "Equal"
      negation_condition = false
      # Entra returns via form_post (POST); Google's code flow returns via GET.
      match_values = ["GET", "POST"]
    }
  }

  custom_rules {
    name      = "AllowTwilioVoiceWebhookPost"
    priority  = 20
    rule_type = "MatchRule"
    action    = "Allow"

    match_conditions {
      match_variables {
        variable_name = "RequestUri"
      }

      operator           = "BeginsWith"
      negation_condition = false
      # Twilio voice webhook POSTs carry a CallToken parameter (JSON-wrapped
      # JWT) that trips OWASP CRS SQLi/encoding rules (anomaly score 21,
      # rule 949110 blocked live calls on 2026-07-08). Auth on this path is
      # the app-level X-Twilio-Signature validation + AccountSid check, which
      # this rule does NOT bypass. Voice call-status and recording callbacks
      # can carry the same evolving Twilio-shaped fields and are validated by
      # the same exact-public-URL signature boundary. SMS remains subject to
      # the normal managed-rule inspection path.
      match_values = [
        "/api/v1/voice/twilio/twiml",
        "/api/v1/voice/twilio/call-status",
        "/api/v1/voice/twilio/recording-status",
      ]
    }

    match_conditions {
      match_variables {
        variable_name = "RequestMethod"
      }

      operator           = "Equal"
      negation_condition = false
      # Twilio is configured to deliver the voice webhook via POST only.
      match_values = ["POST"]
    }
  }

  policy_settings {
    enabled                     = true
    mode                        = "Prevention"
    request_body_check          = true
    max_request_body_size_in_kb = 128
    file_upload_limit_in_mb     = 100
  }

  managed_rules {
    managed_rule_set {
      type    = "OWASP"
      version = "3.2"
    }
  }
}

locals {
  app_gateway_frontend_ip_configuration_name = "appGwFrontendIp"
  app_gateway_frontend_port_name             = "https443"
  app_gateway_listener_name                  = "https-${replace(var.app_gateway_hostname, ".", "-")}"
  app_gateway_ssl_certificate_name           = "cloudflare-origin"
  app_gateway_trusted_root_certificate_name  = "origin-ca-root"
  app_gateway_frontend_pool_name             = "frontend-container-app"
  app_gateway_backend_pool_name              = "backend-container-app"
  app_gateway_frontend_http_settings_name    = "frontend-https"
  app_gateway_backend_http_settings_name     = "backend-https"
  app_gateway_frontend_probe_name            = "frontend-health"
  app_gateway_backend_probe_name             = "backend-health"
  app_gateway_url_path_map_name              = "wulo-path-map"
  app_gateway_request_routing_rule_name      = "wulo-routing"
  app_gateway_backend_fqdn                   = azurerm_container_app.backend.ingress[0].fqdn
  app_gateway_frontend_fqdn                  = azurerm_container_app.frontend.ingress[0].fqdn
  app_gateway_backend_pool_fqdn              = local.should_bind_container_app_custom_domains ? var.app_gateway_backend_host_name : local.app_gateway_backend_fqdn
  app_gateway_frontend_pool_fqdn             = local.should_bind_container_app_custom_domains ? var.app_gateway_hostname : local.app_gateway_frontend_fqdn
  app_gateway_backend_host_name              = local.should_bind_container_app_custom_domains ? var.app_gateway_backend_host_name : local.app_gateway_backend_fqdn
  app_gateway_frontend_host_name             = local.should_bind_container_app_custom_domains ? var.app_gateway_hostname : local.app_gateway_frontend_fqdn
}

resource "azurerm_application_gateway" "main" {
  count = local.should_create_application_gateway ? 1 : 0

  name                = local.resource_names.app_gateway
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  firewall_policy_id  = azurerm_web_application_firewall_policy.app_gateway[0].id
  tags                = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app_gateway[0].id]
  }

  sku {
    name     = "WAF_v2"
    tier     = "WAF_v2"
    capacity = var.app_gateway_sku_capacity
  }

  gateway_ip_configuration {
    name      = "appGatewayIpConfig"
    subnet_id = azurerm_subnet.app_gateway[0].id
  }

  frontend_ip_configuration {
    name                 = local.app_gateway_frontend_ip_configuration_name
    public_ip_address_id = azurerm_public_ip.app_gateway[0].id
  }

  frontend_port {
    name = local.app_gateway_frontend_port_name
    port = 443
  }

  ssl_certificate {
    name                = local.app_gateway_ssl_certificate_name
    key_vault_secret_id = var.app_gateway_origin_certificate_secret_id
  }

  ssl_policy {
    policy_type = "Predefined"
    policy_name = "AppGwSslPolicy20220101S"
  }

  trusted_root_certificate {
    name = local.app_gateway_trusted_root_certificate_name
    data = var.app_gateway_trusted_root_certificate_base64
  }

  backend_address_pool {
    name  = local.app_gateway_frontend_pool_name
    fqdns = [local.app_gateway_frontend_pool_fqdn]
  }

  backend_address_pool {
    name  = local.app_gateway_backend_pool_name
    fqdns = [local.app_gateway_backend_pool_fqdn]
  }

  probe {
    name                                      = local.app_gateway_frontend_probe_name
    protocol                                  = "Https"
    path                                      = var.app_gateway_frontend_probe_path
    interval                                  = 30
    timeout                                   = 30
    unhealthy_threshold                       = 3
    pick_host_name_from_backend_http_settings = true

    match {
      status_code = ["200-401"]
    }
  }

  probe {
    name                                      = local.app_gateway_backend_probe_name
    protocol                                  = "Https"
    path                                      = var.app_gateway_backend_probe_path
    interval                                  = 30
    timeout                                   = 30
    unhealthy_threshold                       = 3
    pick_host_name_from_backend_http_settings = true
  }

  backend_http_settings {
    name                           = local.app_gateway_frontend_http_settings_name
    cookie_based_affinity          = "Disabled"
    port                           = 443
    protocol                       = "Https"
    request_timeout                = var.app_gateway_request_timeout_seconds
    host_name                      = local.app_gateway_frontend_host_name
    sni_name                       = local.app_gateway_frontend_host_name
    trusted_root_certificate_names = local.should_bind_container_app_custom_domains ? [local.app_gateway_trusted_root_certificate_name] : []
    probe_name                     = local.app_gateway_frontend_probe_name
  }

  backend_http_settings {
    name                           = local.app_gateway_backend_http_settings_name
    cookie_based_affinity          = "Disabled"
    port                           = 443
    protocol                       = "Https"
    request_timeout                = var.app_gateway_request_timeout_seconds
    host_name                      = local.app_gateway_backend_host_name
    sni_name                       = local.app_gateway_backend_host_name
    trusted_root_certificate_names = local.should_bind_container_app_custom_domains ? [local.app_gateway_trusted_root_certificate_name] : []
    probe_name                     = local.app_gateway_backend_probe_name
  }

  http_listener {
    name                           = local.app_gateway_listener_name
    frontend_ip_configuration_name = local.app_gateway_frontend_ip_configuration_name
    frontend_port_name             = local.app_gateway_frontend_port_name
    protocol                       = "Https"
    host_name                      = var.app_gateway_hostname
    require_sni                    = true
    ssl_certificate_name           = local.app_gateway_ssl_certificate_name
  }

  url_path_map {
    name                               = local.app_gateway_url_path_map_name
    default_backend_address_pool_name  = local.app_gateway_frontend_pool_name
    default_backend_http_settings_name = local.app_gateway_frontend_http_settings_name

    path_rule {
      name                       = "api-backend"
      paths                      = ["/api/*"]
      backend_address_pool_name  = local.app_gateway_backend_pool_name
      backend_http_settings_name = local.app_gateway_backend_http_settings_name
    }
  }

  request_routing_rule {
    name               = local.app_gateway_request_routing_rule_name
    priority           = 100
    rule_type          = "PathBasedRouting"
    http_listener_name = local.app_gateway_listener_name
    url_path_map_name  = local.app_gateway_url_path_map_name
  }

  depends_on = [
    azurerm_role_assignment.keyvault_app_gateway_secrets,
    azurerm_subnet_network_security_group_association.app_gateway,
    azurerm_private_dns_a_record.container_app_custom_domain_backend,
    azurerm_private_dns_a_record.container_app_custom_domain_frontend,
    azurerm_private_dns_a_record.container_app_custom_domain_public_frontend,
    azapi_update_resource.frontend_sticky_sessions,
    azapi_update_resource.backend_sticky_sessions,
  ]
}