"""
Azure App Configuration Provider
================================

Provides seamless integration with Azure App Configuration for centralized
configuration management. Falls back to environment variables when App Config
is not available (backwards compatible).

Uses the official azure-appconfiguration-provider package for simplified
configuration loading.

Usage:
    from config.appconfig_provider import get_config_value, get_feature_flag

    # Get a configuration value (falls back to env var)
    endpoint = get_config_value("azure/openai/endpoint", "AZURE_OPENAI_ENDPOINT")

    # Get a feature flag
    if get_feature_flag("warm-pool"):
        enable_warm_pool()

Architecture:
    1. On startup, uses azure-appconfiguration-provider's load() to fetch all config
    2. Syncs fetched values to environment variables for compatibility
    3. Falls back to environment variables if App Config unavailable
"""

import logging
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Startup logging to stderr (before logging is configured)
def _log(msg):
    print(msg, file=sys.stderr, flush=True)


# ==============================================================================
# CONFIGURATION
# ==============================================================================

APPCONFIG_ENDPOINT = os.getenv("AZURE_APPCONFIG_ENDPOINT", "")
APPCONFIG_LABEL = os.getenv("AZURE_APPCONFIG_LABEL", os.getenv("ENVIRONMENT", "dev"))
APPCONFIG_ENABLED = bool(APPCONFIG_ENDPOINT)

# Global configuration dictionary (loaded from App Config)
_config: dict[str, Any] | None = None
_config_lock = threading.Lock()

_dotenv_local_keys_cache: set[str] | None = None
_appconfig_managed_database_values: dict[str, str] = {}

DATABASE_APPCONFIG_ENV_VARS = frozenset(
    {
        "POSTGRES_HOST",
        "POSTGRES_DATABASE_NAME",
        "POSTGRES_ADMIN_LOGIN",
        "CLINIC_RECALL_DATABASE_URL",
        "CLINIC_RECALL_PRIVACY_DATABASE_URL",
        "CLINIC_RECALL_AUDIT_DATABASE_URL",
    }
)
DATABASE_EXPLICIT_ENV_VARS = DATABASE_APPCONFIG_ENV_VARS | {"POSTGRES_PASSWORD"}


def _find_project_root(start: Path) -> Path | None:
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def _get_dotenv_local_keys() -> set[str]:
    """Return env var names declared in local env files (.env.local or .env).

    These keys are treated as user-intentional overrides and should not be
    overwritten by App Configuration when running locally.
    """

    global _dotenv_local_keys_cache
    if _dotenv_local_keys_cache is not None:
        return _dotenv_local_keys_cache

    keys: set[str] = set()
    try:
        from dotenv import dotenv_values
    except Exception:
        _dotenv_local_keys_cache = set()
        return _dotenv_local_keys_cache

    backend_dir = Path(__file__).resolve().parents[1]  # .../backend
    project_root = _find_project_root(backend_dir)

    candidates: list[Path] = [
        backend_dir / ".env.local",
        backend_dir / ".env",
    ]
    if project_root is not None:
        candidates.extend([project_root / ".env.local", project_root / ".env"])

    for path in candidates:
        if not path.exists():
            continue
        try:
            values = dotenv_values(path)
            keys.update({k for k in values.keys() if k})
        except Exception:
            # If parsing fails, fall back to empty (do not accidentally protect keys).
            pass

    _dotenv_local_keys_cache = keys
    return _dotenv_local_keys_cache


def _env_override_allowed_when_appconfig_loaded(env_var_name: str) -> bool:
    """Only allow env-var overrides when explicitly set in .env.local."""

    return env_var_name in _get_dotenv_local_keys() and env_var_name in os.environ


def _external_database_overrides() -> set[str]:
    """Return database values not installed by this provider process."""
    return {
        name
        for name in DATABASE_EXPLICIT_ENV_VARS
        if (value := os.getenv(name, "").strip())
        and _appconfig_managed_database_values.get(name) != value
    }


def _clear_managed_database_values_for_external_override(
    external_names: set[str],
) -> None:
    """Remove stale provider values before preserving an external target."""
    if not external_names:
        return
    for name, managed_value in list(_appconfig_managed_database_values.items()):
        if name not in external_names and os.getenv(name) == managed_value:
            os.environ.pop(name, None)
        _appconfig_managed_database_values.pop(name, None)


CLINIKO_APPCONFIG_ENV_VARS = frozenset(
    {
        "CLINIC_RECALL_CLINIKO_API_KEY",
        "CLINIC_RECALL_CLINIKO_BOOKING_RECONCILIATION_ENABLED",
        "CLINIC_RECALL_CLINIKO_MAX_ITEMS",
        "CLINIC_RECALL_CLINIKO_MAX_PAGES",
        "CLINIC_RECALL_CLINIKO_PER_PAGE",
        "CLINIC_RECALL_CLINIKO_SHARD",
        "CLINIC_RECALL_CLINIKO_SYNC_ENABLED",
        "CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS",
        "CLINIC_RECALL_CLINIKO_USER_AGENT",
        "CLINIC_RECALL_DURABLE_BOOKING_CONFIRMATION_ENABLED",
        "CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED",
    }
)
CLINIKO_ENABLE_APPCONFIG_ENV_VARS = frozenset(
    {
        "CLINIC_RECALL_CLINIKO_SYNC_ENABLED",
        "CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED",
        "CLINIC_RECALL_CLINIKO_BOOKING_RECONCILIATION_ENABLED",
        "CLINIC_RECALL_DURABLE_BOOKING_CONFIRMATION_ENABLED",
    }
)
CLINIKO_REQUIRED_APPCONFIG_ENV_VARS = frozenset(
    {
        "CLINIC_RECALL_CLINIKO_API_KEY",
        "CLINIC_RECALL_CLINIKO_SHARD",
        "CLINIC_RECALL_CLINIKO_USER_AGENT",
    }
)
HANDOFF_ENABLE_APPCONFIG_ENV_VARS = frozenset(
    {
        "CLINIC_RECALL_HANDOFF_AGEING_ENABLED",
        "CLINIC_RECALL_HANDOFF_DELIVERY_CALLBACK_ENABLED",
        "CLINIC_RECALL_HANDOFF_NOTIFICATION_ENABLED",
    }
)


def _clear_stale_clinic_recall_environment() -> None:
    """Remove stale safety-sensitive values while preserving local overrides."""
    for environment_name in CLINIKO_APPCONFIG_ENV_VARS:
        if not _env_override_allowed_when_appconfig_loaded(environment_name):
            os.environ.pop(environment_name, None)
    for enabled_name in CLINIKO_ENABLE_APPCONFIG_ENV_VARS:
        if not _env_override_allowed_when_appconfig_loaded(enabled_name):
            os.environ[enabled_name] = "false"
    for enabled_name in HANDOFF_ENABLE_APPCONFIG_ENV_VARS:
        if not _env_override_allowed_when_appconfig_loaded(enabled_name):
            os.environ[enabled_name] = "false"


# ==============================================================================
# KEY MAPPING: App Config Keys -> Environment Variable Names
# ==============================================================================

# Maps Azure App Configuration keys to their equivalent environment variables
# This enables seamless fallback when App Config is unavailable
APPCONFIG_KEY_MAP: dict[str, str] = {
    # Azure OpenAI
    "azure/openai/endpoint": "AZURE_OPENAI_ENDPOINT",
    "azure/openai/deployment-id": "AZURE_OPENAI_CHAT_DEPLOYMENT_ID",
    "azure/openai/api-version": "AZURE_OPENAI_API_VERSION",
    "azure/openai/default-temperature": "DEFAULT_TEMPERATURE",
    "azure/openai/default-max-tokens": "DEFAULT_MAX_TOKENS",
    "azure/openai/request-timeout": "AOAI_REQUEST_TIMEOUT",
    # Azure Speech
    "azure/speech/endpoint": "AZURE_SPEECH_ENDPOINT",
    "azure/speech/region": "AZURE_SPEECH_REGION",
    "azure/speech/resource-id": "AZURE_SPEECH_RESOURCE_ID",
    # Azure Communication Services
    "azure/acs/endpoint": "ACS_ENDPOINT",
    "azure/acs/auth-mode": "ACS_AUTH_MODE",
    "azure/acs/immutable-id": "ACS_IMMUTABLE_ID",
    "azure/acs/source-phone-number": "ACS_SOURCE_PHONE_NUMBER",
    "azure/acs/connection-string": "ACS_CONNECTION_STRING",
    "azure/acs/email-sender-address": "AZURE_EMAIL_SENDER_ADDRESS",
    # SMS provider fallback (Phase 0: Twilio only when ACS numbers are blocked)
    "app/sms/provider": "SMS_PROVIDER",
    "app/sms/twilio/account-sid": "TWILIO_ACCOUNT_SID",
    "app/sms/twilio/auth-token": "TWILIO_AUTH_TOKEN",
    "app/sms/twilio/from-phone-number": "TWILIO_FROM_PHONE_NUMBER",
    "app/sms/twilio/webhook-base-url": "TWILIO_WEBHOOK_BASE_URL",
    "app/sms/twilio/status-callback-url": "TWILIO_SMS_STATUS_CALLBACK_URL",
    "app/clinic-recall/callback-application-enabled": "CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED",
    "app/clinic-recall/handoff-delivery-callback-enabled": "CLINIC_RECALL_HANDOFF_DELIVERY_CALLBACK_ENABLED",
    "app/clinic-recall/handoff-notification-enabled": "CLINIC_RECALL_HANDOFF_NOTIFICATION_ENABLED",
    "app/clinic-recall/handoff-ageing-enabled": "CLINIC_RECALL_HANDOFF_AGEING_ENABLED",
    "app/clinic-recall/durable-call-enabled": "CLINIC_RECALL_DURABLE_CALL_ENABLED",
    "app/clinic-recall/durable-call-provider": "CLINIC_RECALL_DURABLE_CALL_PROVIDER",
    "app/clinic-recall/durable-recording-enabled": "CLINIC_RECALL_DURABLE_RECORDING_ENABLED",
    "app/clinic-recall/durable-recording-provider": "CLINIC_RECALL_DURABLE_RECORDING_PROVIDER",
    "app/clinic-recall/rights/twilio-enabled": "CLINIC_RECALL_DURABLE_RIGHTS_TWILIO_ENABLED",
    "app/clinic-recall/rights/blob-enabled": "CLINIC_RECALL_DURABLE_RIGHTS_BLOB_ENABLED",
    "app/clinic-recall/rights/hmac-key-version": "CLINIC_RECALL_RIGHTS_HMAC_KEY_VERSION",
    "app/clinic-recall/rights/hmac-key": "CLINIC_RECALL_RIGHTS_HMAC_KEY",
    "app/clinic-recall/rights/hmac-previous-keys-json": "CLINIC_RECALL_RIGHTS_HMAC_PREVIOUS_KEYS_JSON",
    "app/clinic-recall/rights/policy-version": "CLINIC_RECALL_RIGHTS_POLICY_VERSION",
    "app/clinic-recall/rights/approval-evidence-sha256": "CLINIC_RECALL_RIGHTS_APPROVAL_EVIDENCE_SHA256",
    "app/clinic-recall/rights/request-due-seconds": "CLINIC_RECALL_RIGHTS_REQUEST_DUE_SECONDS",
    "app/clinic-recall/rights/residual-approvals-json": "CLINIC_RECALL_RIGHTS_RESIDUAL_APPROVALS_JSON",
    "app/clinic-recall/retention/policy-version": "CLINIC_RECALL_RETENTION_POLICY_VERSION",
    "app/clinic-recall/retention/approval-evidence-sha256": "CLINIC_RECALL_RETENTION_APPROVAL_EVIDENCE_SHA256",
    "app/clinic-recall/retention/policy-approved-at": "CLINIC_RECALL_RETENTION_POLICY_APPROVED_AT",
    "app/clinic-recall/retention/policy-effective-at": "CLINIC_RECALL_RETENTION_POLICY_EFFECTIVE_AT",
    "app/clinic-recall/retention/policy-expires-at": "CLINIC_RECALL_RETENTION_POLICY_EXPIRES_AT",
    "app/clinic-recall/retention/retain-for-seconds": "CLINIC_RECALL_RETENTION_RETAIN_FOR_SECONDS",
    "app/clinic-recall/retention/request-due-seconds": "CLINIC_RECALL_RETENTION_REQUEST_DUE_SECONDS",
    "app/clinic-recall/cliniko/api-key": "CLINIC_RECALL_CLINIKO_API_KEY",
    "app/clinic-recall/cliniko/shard": "CLINIC_RECALL_CLINIKO_SHARD",
    "app/clinic-recall/cliniko/user-agent": "CLINIC_RECALL_CLINIKO_USER_AGENT",
    "app/clinic-recall/cliniko/timeout-seconds": "CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS",
    "app/clinic-recall/cliniko/per-page": "CLINIC_RECALL_CLINIKO_PER_PAGE",
    "app/clinic-recall/cliniko/max-pages": "CLINIC_RECALL_CLINIKO_MAX_PAGES",
    "app/clinic-recall/cliniko/max-items": "CLINIC_RECALL_CLINIKO_MAX_ITEMS",
    "app/clinic-recall/cliniko/enabled": "CLINIC_RECALL_CLINIKO_SYNC_ENABLED",
    "app/clinic-recall/cliniko/write-enabled": "CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED",
    "app/clinic-recall/cliniko/reconciliation-enabled": "CLINIC_RECALL_CLINIKO_BOOKING_RECONCILIATION_ENABLED",
    "app/clinic-recall/booking-confirmation/enabled": "CLINIC_RECALL_DURABLE_BOOKING_CONFIRMATION_ENABLED",
    "app/clinic-recall/pilot/outreach-enabled": "CLINIC_RECALL_PILOT_OUTREACH_ENABLED",
    "app/clinic-recall/pilot/voice-enabled": "CLINIC_RECALL_PILOT_VOICE_ENABLED",
    "app/clinic-recall/pilot/recording-enabled": "CLINIC_RECALL_PILOT_RECORDING_ENABLED",
    "app/clinic-recall/pilot/config-max-age-seconds": "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS",
    "app/clinic-recall/pilot/environment": "CLINIC_RECALL_PILOT_ENVIRONMENT",
    "app/clinic-recall/pilot/release-identity": "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
    "app/clinic-recall/recording/disclosure-approved": "CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED",
    "app/clinic-recall/recording/disclosure-text": "CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT",
    "app/clinic-recall/recording/disclosure-version": "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION",
    "app/clinic-recall/recording/blob-account-url": "RECORDINGS_BLOB_ACCOUNT_URL",
    "app/clinic-recall/recording/blob-container": "RECORDINGS_BLOB_CONTAINER",
    "app/clinic-recall/cadence-planning-enabled": "CLINIC_RECALL_CADENCE_PLANNING_ENABLED",
    "app/clinic-recall/cadence-config-refreshed-at": "CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT",
    "app/clinic-recall/cadence-config-max-age-seconds": "CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS",
    # Voice provider fallback (Twilio only when ACS/ART voice proof is blocked)
    "app/voice/provider": "VOICE_PROVIDER",
    "app/voice/twilio/from-phone-number": "TWILIO_VOICE_FROM_NUMBER",
    "app/voice/twilio/twiml-url": "TWILIO_VOICE_TWIML_URL",
    "app/voice/twilio/media-stream-url": "TWILIO_MEDIA_STREAM_URL",
    "app/voice/twilio/inline-twiml": "TWILIO_VOICE_INLINE_TWIML",
    "app/voice/twilio/status-callback-url": "TWILIO_VOICE_STATUS_CALLBACK_URL",
    "app/voice/twilio/recording-status-callback-url": "TWILIO_RECORDING_STATUS_CALLBACK_URL",
    # Clinic Recall Postgres data plane
    "app/postgres/host": "POSTGRES_HOST",
    "app/postgres/database-name": "POSTGRES_DATABASE_NAME",
    "app/postgres/admin-login": "POSTGRES_ADMIN_LOGIN",
    "app/postgres/connection-string": "CLINIC_RECALL_DATABASE_URL",
    "app/postgres/privacy-connection-string": "CLINIC_RECALL_PRIVACY_DATABASE_URL",
    "app/postgres/audit-connection-string": "CLINIC_RECALL_AUDIT_DATABASE_URL",
    # Temporary Phase 4 staff context until identity-derived clinic mapping lands
    "app/clinic-recall/staff/clinic-id": "CLINIC_RECALL_STAFF_CLINIC_ID",
    "app/clinic-recall/staff/actor": "CLINIC_RECALL_STAFF_ACTOR",
    "app/clinic-recall/staff/roles": "CLINIC_RECALL_STAFF_ROLES",
    "app/clinic-recall/self-serve-signup-enabled": "ENABLE_SELF_SERVE_SIGNUP",
    "app/clinic-recall/inbound-text-agent-enabled": "CLINIC_RECALL_INBOUND_TEXT_AGENT_ENABLED",
    "app/clinic-recall/inbound-text-agent-deployment": "CLINIC_RECALL_INBOUND_TEXT_AGENT_DEPLOYMENT",
    # Redis
    "azure/redis/hostname": "REDIS_HOST",
    "azure/redis/port": "REDIS_PORT",
    # Cosmos DB
    "azure/cosmos/database-name": "AZURE_COSMOS_DATABASE_NAME",
    "azure/cosmos/collection-name": "AZURE_COSMOS_COLLECTION_NAME",
    "azure/cosmos/connection-string": "AZURE_COSMOS_CONNECTION_STRING",
    # Storage
    "azure/storage/account-name": "AZURE_STORAGE_ACCOUNT_NAME",
    "azure/storage/container-url": "AZURE_STORAGE_CONTAINER_URL",
    # Voice Live (note: VoiceLiveSettings expects AZURE_VOICELIVE_* format)
    "azure/voicelive/endpoint": "AZURE_VOICELIVE_ENDPOINT",
    "azure/voicelive/model": "AZURE_VOICELIVE_MODEL",
    "azure/voicelive/resource-id": "AZURE_VOICELIVE_RESOURCE_ID",
    "app/voice/voicelive/token-warmup-enabled": "VOICELIVE_TOKEN_WARMUP_ENABLED",
    "app/voice/voicelive/token-warmup-timeout-seconds": "VOICELIVE_TOKEN_WARMUP_TIMEOUT_SECONDS",

    # Application Insights
    "azure/appinsights/connection-string": "APPLICATIONINSIGHTS_CONNECTION_STRING",
    # AI Foundry
    "azure/ai-foundry/project-endpoint": "AZURE_AI_FOUNDRY_PROJECT_ENDPOINT",
    # Pool Settings
    "app/pools/tts-size": "POOL_SIZE_TTS",
    "app/pools/stt-size": "POOL_SIZE_STT",
    "app/pools/aoai-size": "AOAI_POOL_SIZE",
    "app/pools/low-water-mark": "POOL_LOW_WATER_MARK",
    "app/pools/high-water-mark": "POOL_HIGH_WATER_MARK",
    "app/pools/acquire-timeout": "POOL_ACQUIRE_TIMEOUT",
    "app/pools/warm-tts-size": "WARM_POOL_TTS_SIZE",
    "app/pools/warm-stt-size": "WARM_POOL_STT_SIZE",
    "app/pools/warm-refresh-interval": "WARM_POOL_REFRESH_INTERVAL",
    "app/pools/warm-session-max-age": "WARM_POOL_SESSION_MAX_AGE",
    "app/pools/warm-restart-on-failure": "WARM_POOL_RESTART_ON_FAILURE",
    # Connection Settings
    "app/connections/max-websocket": "MAX_WEBSOCKET_CONNECTIONS",
    "app/connections/queue-size": "CONNECTION_QUEUE_SIZE",
    "app/connections/warning-threshold": "CONNECTION_WARNING_THRESHOLD",
    "app/connections/critical-threshold": "CONNECTION_CRITICAL_THRESHOLD",
    "app/connections/timeout-seconds": "CONNECTION_TIMEOUT_SECONDS",
    "app/connections/heartbeat-interval": "HEARTBEAT_INTERVAL_SECONDS",
    # Session Settings
    "app/session/ttl-seconds": "SESSION_TTL_SECONDS",
    "app/session/cleanup-interval": "SESSION_CLEANUP_INTERVAL",
    "app/session/state-ttl": "SESSION_STATE_TTL",
    "app/session/max-concurrent": "MAX_CONCURRENT_SESSIONS",
    # Voice & TTS Settings
    "app/voice/tts-sample-rate-ui": "TTS_SAMPLE_RATE_UI",
    "app/voice/tts-sample-rate-acs": "TTS_SAMPLE_RATE_ACS",
    "app/voice/tts-chunk-size": "TTS_CHUNK_SIZE",
    "app/voice/tts-processing-timeout": "TTS_PROCESSING_TIMEOUT",
    "app/voice/stt-processing-timeout": "STT_PROCESSING_TIMEOUT",
    "app/voice/silence-duration-ms": "SILENCE_DURATION_MS",
    "app/voice/recognized-languages": "RECOGNIZED_LANGUAGE",
    "app/voice/default-tts-voice": "DEFAULT_TTS_VOICE",
    # Scaling (informational)
    "app/scaling/min-replicas": "CONTAINER_MIN_REPLICAS",
    "app/scaling/max-replicas": "CONTAINER_MAX_REPLICAS",
    # Monitoring
    "app/monitoring/metrics-interval": "METRICS_COLLECTION_INTERVAL",
    "app/monitoring/pool-metrics-interval": "POOL_METRICS_INTERVAL",
    "app/monitoring/genai-input-cost-per-million-tokens-usd": "GENAI_INPUT_COST_PER_MILLION_TOKENS_USD",
    "app/monitoring/genai-output-cost-per-million-tokens-usd": "GENAI_OUTPUT_COST_PER_MILLION_TOKENS_USD",
    # MCP Server Configuration
    "app/mcp/servers/cardapi/url": "MCP_SERVER_CARDAPI_URL",
    "app/mcp/servers/cardapi/timeout": "MCP_SERVER_CARDAPI_TIMEOUT",
    "app/mcp/servers/cardapi/transport": "MCP_SERVER_CARDAPI_TRANSPORT",
    "app/mcp/servers/cardapi/auth-enabled": "MCP_SERVER_CARDAPI_AUTH_ENABLED",
    "app/mcp/servers/cardapi/app-id": "MCP_SERVER_CARDAPI_APP_ID",
    "app/mcp/enabled-servers": "MCP_ENABLED_SERVERS",
    # Public 60-second demo experience (landing widget). The Turnstile SITE key
    # is a public value by design; the Turnstile secret key and demo token
    # secret are stored as Key Vault references, never raw values.
    "app/demo/experience": "DEMO_EXPERIENCE",
    "app/demo/browser-enabled": "DEMO_BROWSER_ENABLED",
    "app/demo/phone-enabled": "DEMO_PHONE_ENABLED",
    "app/demo/max-seconds": "DEMO_MAX_SECONDS",
    "app/demo/turnstile-site-key": "TURNSTILE_SITE_KEY",
    "app/demo/turnstile-secret-key": "TURNSTILE_SECRET_KEY",
    "app/demo/token-secret": "DEMO_TOKEN_SECRET",
    "app/demo/rate-per-ip-per-hour": "DEMO_RATE_PER_IP_PER_HOUR",
    "app/demo/rate-per-phone-per-day": "DEMO_RATE_PER_PHONE_PER_DAY",
    "app/demo/rate-global-per-day": "DEMO_RATE_GLOBAL_PER_DAY",
    # Environment
    "app/environment": "ENVIRONMENT",
    # Application URLs (set by postprovision)
    "app/backend/base-url": "BASE_URL",
    "app/frontend/backend-url": "VITE_BACKEND_BASE_URL",
    "app/frontend/ws-url": "VITE_WS_BASE_URL",
}

PILOT_APPCONFIG_ENV_VARS = frozenset(
    {
        "CLINIC_RECALL_PILOT_OUTREACH_ENABLED",
        "CLINIC_RECALL_PILOT_VOICE_ENABLED",
        "CLINIC_RECALL_PILOT_RECORDING_ENABLED",
        "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS",
        "CLINIC_RECALL_PILOT_ENVIRONMENT",
        "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
    }
)

RECORDING_DISCLOSURE_APPCONFIG_ENV_VARS = frozenset(
    {
        "CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED",
        "CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT",
        "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION",
    }
)

# Feature flag mapping: App Config feature name -> Environment variable name
FEATURE_FLAG_MAP: dict[str, str] = {
    "dtmf-validation": "DTMF_VALIDATION_ENABLED",
    "auth-validation": "ENABLE_AUTH_VALIDATION",
    "call-recording": "ENABLE_ACS_CALL_RECORDING",
    "warm-pool": "WARM_POOL_ENABLED",
    "session-persistence": "ENABLE_SESSION_PERSISTENCE",
    "performance-logging": "ENABLE_PERFORMANCE_LOGGING",
    "tracing": "ENABLE_TRACING",
    "connection-limits": "ENABLE_CONNECTION_LIMITS",
}


# ==============================================================================
# PROVIDER-BASED CONFIGURATION LOADING
# ==============================================================================


def _load_config_from_appconfig() -> dict[str, Any] | None:
    """
    Load all configuration from Azure App Configuration using the provider package.

    Returns:
        Dictionary of all configuration values, or None if loading fails
    """
    global _config

    if not APPCONFIG_ENABLED:
        return None

    # Validate endpoint format
    if not APPCONFIG_ENDPOINT.endswith(".azconfig.io"):
        _log(f"⚠️  Invalid App Config endpoint: {APPCONFIG_ENDPOINT}")
        return None

    try:
        from azure.appconfiguration.provider import SettingSelector, load
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

        # Choose credential based on AZURE_CLIENT_ID
        azure_client_id = os.getenv("AZURE_CLIENT_ID")
        if azure_client_id:
            credential = ManagedIdentityCredential(client_id=azure_client_id)
        else:
            credential = DefaultAzureCredential()

        # Load with retry (exponential backoff)
        import time
        last_error = None

        for attempt in range(1, 4):
            try:
                config = load(
                    endpoint=APPCONFIG_ENDPOINT,
                    credential=credential,
                    selects=[SettingSelector(key_filter="*", label_filter=APPCONFIG_LABEL)],
                    keyvault_credential=credential,
                    replica_discovery_enabled=False,  # Avoid DNS SRV lookup issues
                )

                config_dict = dict(config)

                with _config_lock:
                    _config = config_dict
                return config_dict

            except Exception as e:
                last_error = e
                if attempt < 3:
                    time.sleep(2 ** attempt)  # 2, 4 seconds

        raise last_error

    except ImportError:
        _log("❌ azure-appconfiguration-provider not installed")
        return None
    except Exception as e:
        _log(f"❌ App Config load failed: {e}")
        return None


def sync_appconfig_to_env(config_dict: dict[str, Any] | None = None) -> dict[str, str]:
    """
    Sync App Configuration values to environment variables.

    Args:
        config_dict: Configuration dictionary (uses global if not provided)

    Returns:
        Dict of synced key-value pairs (env_var_name -> value)
    """
    if config_dict is None:
        with _config_lock:
            config_dict = _config

    _clear_stale_clinic_recall_environment()
    if not config_dict:
        return {}

    synced: dict[str, str] = {}
    deferred_cliniko_values: dict[str, str] = {}
    skipped_local = 0
    external_database_names = _external_database_overrides()
    _clear_managed_database_values_for_external_override(external_database_names)

    for appconfig_key, env_var_name in APPCONFIG_KEY_MAP.items():
        # Try exact match, then colon format
        value = config_dict.get(appconfig_key) or config_dict.get(appconfig_key.replace("/", ":"))

        if value is not None:
            if external_database_names and env_var_name in DATABASE_APPCONFIG_ENV_VARS:
                skipped_local += 1
                continue
            # Skip if explicitly set in .env.local
            if _env_override_allowed_when_appconfig_loaded(env_var_name):
                skipped_local += 1
                continue
            # Strip whitespace/newlines that may have been introduced during storage
            clean_value = str(value).strip()
            if env_var_name in CLINIKO_ENABLE_APPCONFIG_ENV_VARS:
                deferred_cliniko_values[env_var_name] = clean_value
                continue
            os.environ[env_var_name] = clean_value
            synced[env_var_name] = clean_value
            if env_var_name in DATABASE_APPCONFIG_ENV_VARS:
                _appconfig_managed_database_values[env_var_name] = clean_value

    complete = all(
        environment_name in synced
        or _env_override_allowed_when_appconfig_loaded(environment_name)
        for environment_name in CLINIKO_REQUIRED_APPCONFIG_ENV_VARS
    )
    base_enabled_name = "CLINIC_RECALL_CLINIKO_SYNC_ENABLED"
    base_requested = (
        os.environ.get(base_enabled_name, "false")
        if _env_override_allowed_when_appconfig_loaded(base_enabled_name)
        else deferred_cliniko_values.get(base_enabled_name, "false")
    ).lower()
    base_enabled = complete and base_requested in {"1", "true", "yes", "on"}
    for enabled_name in CLINIKO_ENABLE_APPCONFIG_ENV_VARS:
        requested = (
            os.environ.get(enabled_name, "false")
            if _env_override_allowed_when_appconfig_loaded(enabled_name)
            else deferred_cliniko_values.get(enabled_name, "false")
        ).lower()
        enabled = (
            base_enabled
            if enabled_name == base_enabled_name
            else base_enabled and requested in {"1", "true", "yes", "on"}
        )
        value = "true" if enabled else "false"
        os.environ[enabled_name] = value
        synced[enabled_name] = value

    # Single summary line
    endpoint_name = APPCONFIG_ENDPOINT.split("//")[-1].split(".")[0] if APPCONFIG_ENDPOINT else "unknown"
    local_note = f", {skipped_local} local overrides" if skipped_local else ""
    _log(f"   App Config ({endpoint_name}): {len(synced)} keys synced{local_note}")

    return synced


def bootstrap_appconfig() -> bool:
    """
    Bootstrap App Configuration at application startup.

    Call this BEFORE any other imports that depend on environment variables.

    Returns:
        True if App Config loaded successfully, False otherwise
    """
    if not APPCONFIG_ENABLED:
        _update_pilot_config_freshness({})
        _update_recording_disclosure_freshness({})
        _log("   App Config: Not configured (using env vars)")
        return False

    config_dict = _load_config_from_appconfig()
    if not config_dict:
        _clear_stale_clinic_recall_environment()
        _update_pilot_config_freshness({})
        _update_recording_disclosure_freshness({})
        _log("⚠️  App Config: Failed to load (using env vars)")
        return False

    synced = sync_appconfig_to_env(config_dict)
    _update_pilot_config_freshness(synced)
    _update_recording_disclosure_freshness(synced)
    return True


# ==============================================================================
# PUBLIC API - Configuration Access
# ==============================================================================


def get_config_value(
    appconfig_key: str,
    env_var_name: str | None = None,
    default: str | None = None,
) -> str | None:
    """
    Get a configuration value with fallback:
    1. Loaded App Configuration (in memory)
    2. Environment variable
    3. Default value

    Args:
        appconfig_key: Key in App Configuration (e.g., "azure/openai/endpoint")
        env_var_name: Environment variable name for fallback (auto-mapped if None)
        default: Default value if not found anywhere

    Returns:
        Configuration value or default
    """
    # Determine env var name
    if env_var_name is None:
        env_var_name = APPCONFIG_KEY_MAP.get(appconfig_key)

    # Check loaded config first
    with _config_lock:
        config_loaded = _config is not None
        if _config and appconfig_key in _config:
            return str(_config[appconfig_key]).strip()

    # Fall back to environment variable
    if env_var_name:
        # When AppConfig is loaded, ignore ambient env vars unless explicitly
        # provided via .env.local (to avoid surprising/incorrect behavior).
        if APPCONFIG_ENABLED and config_loaded and not _env_override_allowed_when_appconfig_loaded(
            env_var_name
        ):
            return default
        value = os.getenv(env_var_name)
        if value is not None:
            return value.strip()

    return default


def get_feature_flag(
    name: str,
    env_var_name: str | None = None,
    default: bool = False,
) -> bool:
    """
    Get a feature flag with fallback:
    1. Loaded App Configuration feature flags
    2. Environment variable (parsed as bool)
    3. Default value

    Args:
        name: Feature flag name (e.g., "warm-pool")
        env_var_name: Environment variable for fallback (auto-mapped if None)
        default: Default value if not found

    Returns:
        Feature flag state (True/False)
    """
    # Determine env var name
    if env_var_name is None:
        env_var_name = FEATURE_FLAG_MAP.get(name)

    # Feature flags in App Config use a special key prefix
    feature_key = f".appconfig.featureflag/{name}"

    # Check loaded config
    with _config_lock:
        config_loaded = _config is not None
        if _config and feature_key in _config:
            flag_data = _config[feature_key]
            if isinstance(flag_data, dict):
                return flag_data.get("enabled", default)
            return bool(flag_data)

    # Fall back to environment variable
    if env_var_name:
        if APPCONFIG_ENABLED and config_loaded and not _env_override_allowed_when_appconfig_loaded(
            env_var_name
        ):
            return default
        env_value = os.getenv(env_var_name, "").lower()
        if env_value in ("true", "1", "yes", "on"):
            return True
        elif env_value in ("false", "0", "no", "off"):
            return False

    return default


def get_config_int(
    appconfig_key: str,
    env_var_name: str | None = None,
    default: int = 0,
) -> int:
    """Get a configuration value as integer."""
    value = get_config_value(appconfig_key, env_var_name)
    if value is not None:
        try:
            return int(value)
        except ValueError:
            logger.warning(f"Invalid int value for {appconfig_key}: {value}")
    return default


def get_config_float(
    appconfig_key: str,
    env_var_name: str | None = None,
    default: float = 0.0,
) -> float:
    """Get a configuration value as float."""
    value = get_config_value(appconfig_key, env_var_name)
    if value is not None:
        try:
            return float(value)
        except ValueError:
            logger.warning(f"Invalid float value for {appconfig_key}: {value}")
    return default


def get_provider_status() -> dict[str, Any]:
    """
    Get the status of the App Configuration provider.

    Returns:
        Dict with status information
    """
    with _config_lock:
        config_loaded = _config is not None
        config_count = len(_config) if _config else 0

    return {
        "enabled": APPCONFIG_ENABLED,
        "endpoint": APPCONFIG_ENDPOINT if APPCONFIG_ENABLED else None,
        "label": APPCONFIG_LABEL,
        "loaded": config_loaded,
        "key_count": config_count,
    }


def refresh_cache() -> None:
    """Clear the configuration, force reload, and sync values to env."""
    global _config
    with _config_lock:
        _config = None
    config_dict = _load_config_from_appconfig()
    if not config_dict:
        _clear_stale_clinic_recall_environment()
        _update_pilot_config_freshness({})
        _update_recording_disclosure_freshness({})
        logger.warning("App Configuration refresh failed")
        return
    synced = sync_appconfig_to_env(config_dict)
    _update_pilot_config_freshness(synced)
    _update_recording_disclosure_freshness(synced)
    logger.info("App Configuration refreshed")


def _update_pilot_config_freshness(synced: dict[str, str]) -> None:
    if PILOT_APPCONFIG_ENV_VARS <= synced.keys():
        _mark_pilot_config_refreshed()
        return
    os.environ.pop("CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT", None)


def _mark_pilot_config_refreshed() -> None:
    os.environ["CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT"] = datetime.now(UTC).isoformat()


def _update_recording_disclosure_freshness(synced: dict[str, str]) -> None:
    marker = "CLINIC_RECALL_RECORDING_DISCLOSURE_REFRESHED_AT"
    if RECORDING_DISCLOSURE_APPCONFIG_ENV_VARS <= synced.keys():
        os.environ[marker] = datetime.now(UTC).isoformat()
        return
    os.environ.pop(marker, None)


# ==============================================================================
# CONVENIENCE ALIASES
# ==============================================================================

refresh_appconfig_cache = refresh_cache
get_appconfig_status = get_provider_status
initialize_appconfig = bootstrap_appconfig
