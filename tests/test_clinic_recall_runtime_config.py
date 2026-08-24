import json
from datetime import UTC, datetime, timedelta

import pytest
from apps.artagent.backend.config import appconfig_provider
from apps.artagent.backend.config.appconfig_provider import APPCONFIG_KEY_MAP
from src.clinic_recall.config import (
    get_database_url,
    get_privacy_database_url,
    get_retention_policy,
    get_rights_residual_approvals,
)
from src.clinic_recall.durable.config import (
    cliniko_booking_reconciliation_enabled,
    durable_booking_confirmation_enabled,
    durable_cliniko_write_enabled,
    durable_rights_blob_enabled,
    durable_rights_enabled,
    durable_rights_twilio_enabled,
    retention_scheduling_enabled,
)
from src.clinic_recall.enums import RightsResidualCategory


def test_clinic_recall_database_url_accepts_libpq_conninfo(monkeypatch):
    monkeypatch.delenv("CLINIC_RECALL_PRIVACY_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        "host=db.example.test port=5432 dbname=clinic_recall_spike "
        "user=clinicrecalladmin password='p@ss word' sslmode=require",
    )
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    assert get_database_url() == (
        "postgresql+psycopg://clinicrecalladmin:p%40ss+word"
        "@db.example.test:5432/clinic_recall_spike?sslmode=require"
    )


def test_privacy_job_database_url_is_separate_from_general_connection(monkeypatch):
    monkeypatch.setenv(
        "CLINIC_RECALL_PRIVACY_DATABASE_URL",
        "postgresql+psycopg://privacy-role:private@db.example.test/clinic",
    )
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        "postgresql+psycopg://admin:admin@db.example.test/clinic",
    )

    assert get_privacy_database_url() == (
        "postgresql+psycopg://privacy-role:private@db.example.test/clinic"
    )
    assert get_database_url() == (
        "postgresql+psycopg://admin:admin@db.example.test/clinic"
    )


def test_appconfig_maps_clinic_recall_postgres_and_staff_context():
    assert APPCONFIG_KEY_MAP["app/postgres/connection-string"] == "CLINIC_RECALL_DATABASE_URL"
    assert (
        APPCONFIG_KEY_MAP["app/postgres/privacy-connection-string"]
        == "CLINIC_RECALL_PRIVACY_DATABASE_URL"
    )
    assert APPCONFIG_KEY_MAP["app/postgres/host"] == "POSTGRES_HOST"
    assert APPCONFIG_KEY_MAP["app/postgres/database-name"] == "POSTGRES_DATABASE_NAME"
    assert APPCONFIG_KEY_MAP["app/postgres/admin-login"] == "POSTGRES_ADMIN_LOGIN"
    assert APPCONFIG_KEY_MAP["app/clinic-recall/staff/clinic-id"] == "CLINIC_RECALL_STAFF_CLINIC_ID"
    assert APPCONFIG_KEY_MAP["app/clinic-recall/staff/actor"] == "CLINIC_RECALL_STAFF_ACTOR"
    assert APPCONFIG_KEY_MAP["app/clinic-recall/staff/roles"] == "CLINIC_RECALL_STAFF_ROLES"
    assert (
        APPCONFIG_KEY_MAP["app/clinic-recall/callback-application-enabled"]
        == "CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED"
    )
    assert APPCONFIG_KEY_MAP["app/clinic-recall/self-serve-signup-enabled"] == "ENABLE_SELF_SERVE_SIGNUP"


def test_appconfig_cannot_override_an_explicit_database_target(monkeypatch) -> None:
    monkeypatch.setattr(appconfig_provider, "_appconfig_managed_database_values", {})
    monkeypatch.setattr(
        appconfig_provider,
        "_env_override_allowed_when_appconfig_loaded",
        lambda _name: False,
    )
    for name in appconfig_provider.DATABASE_EXPLICIT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    explicit_url = "postgresql+psycopg://local:test@127.0.0.1/local"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", explicit_url)

    synced = appconfig_provider.sync_appconfig_to_env(
        {
            "app/postgres/host": "live.example.test",
            "app/postgres/database-name": "live",
            "app/postgres/admin-login": "live-admin",
            "app/postgres/connection-string": (
                "postgresql+psycopg://live:secret@live.example.test/live"
            ),
            "app/postgres/audit-connection-string": (
                "postgresql+psycopg://clinic_recall_audit:secret@live.example.test/live"
            ),
        }
    )

    assert appconfig_provider.os.environ["CLINIC_RECALL_DATABASE_URL"] == explicit_url
    assert "POSTGRES_HOST" not in appconfig_provider.os.environ
    assert "CLINIC_RECALL_AUDIT_DATABASE_URL" not in appconfig_provider.os.environ
    assert not (appconfig_provider.DATABASE_APPCONFIG_ENV_VARS & synced.keys())


def test_appconfig_managed_database_target_can_refresh(monkeypatch) -> None:
    monkeypatch.setattr(appconfig_provider, "_appconfig_managed_database_values", {})
    monkeypatch.setattr(
        appconfig_provider,
        "_env_override_allowed_when_appconfig_loaded",
        lambda _name: False,
    )
    for name in appconfig_provider.DATABASE_EXPLICIT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    first = appconfig_provider.sync_appconfig_to_env(
        {
            "app/postgres/host": "first.example.test",
            "app/postgres/database-name": "first",
            "app/postgres/connection-string": (
                "postgresql+psycopg://admin:test@first.example.test/first"
            ),
        }
    )
    second = appconfig_provider.sync_appconfig_to_env(
        {
            "app/postgres/host": "second.example.test",
            "app/postgres/database-name": "second",
            "app/postgres/connection-string": (
                "postgresql+psycopg://admin:test@second.example.test/second"
            ),
        }
    )

    assert first["POSTGRES_HOST"] == "first.example.test"
    assert second["POSTGRES_HOST"] == "second.example.test"
    assert second["POSTGRES_DATABASE_NAME"] == "second"
    assert second["CLINIC_RECALL_DATABASE_URL"].endswith(
        "@second.example.test/second"
    )


def test_rights_and_retention_runtime_switches_default_off(monkeypatch) -> None:
    names = (
        "CLINIC_RECALL_DURABLE_RIGHTS_ENABLED",
        "CLINIC_RECALL_DURABLE_RIGHTS_TWILIO_ENABLED",
        "CLINIC_RECALL_DURABLE_RIGHTS_BLOB_ENABLED",
        "CLINIC_RECALL_RETENTION_SCHEDULER_ENABLED",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    assert durable_rights_enabled() is False
    assert durable_rights_twilio_enabled() is False
    assert durable_rights_blob_enabled() is False
    assert retention_scheduling_enabled() is False

    for name in names:
        monkeypatch.setenv(name, "true")
    assert durable_rights_enabled() is True
    assert durable_rights_twilio_enabled() is True
    assert durable_rights_blob_enabled() is True
    assert retention_scheduling_enabled() is True


def test_cliniko_write_reconcile_and_confirmation_switches_default_off(
    monkeypatch,
) -> None:
    names = (
        "CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED",
        "CLINIC_RECALL_CLINIKO_BOOKING_RECONCILIATION_ENABLED",
        "CLINIC_RECALL_DURABLE_BOOKING_CONFIRMATION_ENABLED",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert durable_cliniko_write_enabled() is False
    assert cliniko_booking_reconciliation_enabled() is False
    assert durable_booking_confirmation_enabled() is False

    for name in names:
        monkeypatch.setenv(name, "definitely")
    assert durable_cliniko_write_enabled() is False
    assert cliniko_booking_reconciliation_enabled() is False
    assert durable_booking_confirmation_enabled() is False

    for name in names:
        monkeypatch.setenv(name, "true")
    assert durable_cliniko_write_enabled() is True
    assert cliniko_booking_reconciliation_enabled() is True
    assert durable_booking_confirmation_enabled() is True


def test_appconfig_maps_cliniko_write_controls_and_fails_closed(monkeypatch) -> None:
    expected = {
        "app/clinic-recall/cliniko/write-enabled": (
            "CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED"
        ),
        "app/clinic-recall/cliniko/reconciliation-enabled": (
            "CLINIC_RECALL_CLINIKO_BOOKING_RECONCILIATION_ENABLED"
        ),
        "app/clinic-recall/booking-confirmation/enabled": (
            "CLINIC_RECALL_DURABLE_BOOKING_CONFIRMATION_ENABLED"
        ),
    }
    for key, environment_name in expected.items():
        assert APPCONFIG_KEY_MAP[key] == environment_name

    monkeypatch.setattr(appconfig_provider, "APPCONFIG_ENABLED", True)
    monkeypatch.setattr(
        appconfig_provider,
        "_env_override_allowed_when_appconfig_loaded",
        lambda _name: False,
    )
    synced = appconfig_provider.sync_appconfig_to_env(
        {key: "true" for key in expected}
    )
    for environment_name in expected.values():
        assert synced[environment_name] == "false"
        assert appconfig_provider.os.environ[environment_name] == "false"

    complete = {
        "app/clinic-recall/cliniko/api-key": "synthetic-appconfig-key-uk2",
        "app/clinic-recall/cliniko/shard": "uk2",
        "app/clinic-recall/cliniko/user-agent": (
            "Clinic Recall Tests (cliniko-tests@example.invalid)"
        ),
        "app/clinic-recall/cliniko/enabled": "true",
        **{key: "true" for key in expected},
    }
    synced = appconfig_provider.sync_appconfig_to_env(complete)
    for environment_name in expected.values():
        assert synced[environment_name] == "true"

    base_disabled = {
        **complete,
        "app/clinic-recall/cliniko/enabled": "false",
    }
    synced = appconfig_provider.sync_appconfig_to_env(base_disabled)
    assert synced["CLINIC_RECALL_CLINIKO_SYNC_ENABLED"] == "false"
    for environment_name in expected.values():
        assert synced[environment_name] == "false"


def test_retention_policy_requires_complete_explicit_configuration(monkeypatch) -> None:
    names = {
        "CLINIC_RECALL_RETENTION_POLICY_VERSION": "tests-retention-v1",
        "CLINIC_RECALL_RETENTION_APPROVAL_EVIDENCE_SHA256": "a" * 64,
        "CLINIC_RECALL_RETENTION_POLICY_APPROVED_AT": "2026-07-20T00:00:00Z",
        "CLINIC_RECALL_RETENTION_POLICY_EFFECTIVE_AT": "2026-07-21T00:00:00Z",
        "CLINIC_RECALL_RETENTION_POLICY_EXPIRES_AT": "2027-07-21T00:00:00Z",
        "CLINIC_RECALL_RETENTION_RETAIN_FOR_SECONDS": "3456000",
        "CLINIC_RECALL_RETENTION_REQUEST_DUE_SECONDS": "604800",
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="incomplete"):
        get_retention_policy()

    for name, value in names.items():
        monkeypatch.setenv(name, value)
    policy = get_retention_policy()
    assert policy.version == "tests-retention-v1"
    assert policy.approved_at == datetime(2026, 7, 20, tzinfo=UTC)
    assert policy.retain_for == timedelta(days=40)
    assert policy.request_due_after == timedelta(days=7)

    monkeypatch.setenv(
        "CLINIC_RECALL_RETENTION_POLICY_EFFECTIVE_AT",
        "2026-07-21T00:00:00",
    )
    with pytest.raises(RuntimeError, match="invalid"):
        get_retention_policy()


def test_residual_approvals_require_strict_versioned_absolute_policy(monkeypatch) -> None:
    name = "CLINIC_RECALL_RIGHTS_RESIDUAL_APPROVALS_JSON"
    monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="must be set"):
        get_rights_residual_approvals()

    payload = {
        RightsResidualCategory.PROCESSOR_PROCEDURE.value: {
            "policy_version": "tests-residual-v1",
            "approval_evidence_sha256": "b" * 64,
            "due_at": "2026-10-21T00:00:00Z",
            "completion_eligible": True,
        },
        RightsResidualCategory.CLINIKO_CONTROLLER_PROCEDURE.value: {
            "policy_version": "tests-controller-v1",
            "approval_evidence_sha256": "c" * 64,
            "due_at": "2026-11-21T00:00:00+00:00",
            "completion_eligible": False,
        },
    }
    monkeypatch.setenv(name, json.dumps(payload))

    approvals = get_rights_residual_approvals()

    assert set(approvals) == {
        RightsResidualCategory.PROCESSOR_PROCEDURE,
        RightsResidualCategory.CLINIKO_CONTROLLER_PROCEDURE,
    }
    assert approvals[RightsResidualCategory.PROCESSOR_PROCEDURE].due_at == datetime(
        2026,
        10,
        21,
        tzinfo=UTC,
    )
    assert approvals[RightsResidualCategory.PROCESSOR_PROCEDURE].completion_eligible is True
    assert (
        approvals[RightsResidualCategory.CLINIKO_CONTROLLER_PROCEDURE].completion_eligible
        is False
    )

    payload[RightsResidualCategory.PROCESSOR_PROCEDURE.value]["free_text"] = "not allowed"
    monkeypatch.setenv(name, json.dumps(payload))
    with pytest.raises(RuntimeError, match="invalid"):
        get_rights_residual_approvals()


def test_appconfig_maps_rights_and_retention_configuration() -> None:
    expected = {
        "app/clinic-recall/rights/twilio-enabled": (
            "CLINIC_RECALL_DURABLE_RIGHTS_TWILIO_ENABLED"
        ),
        "app/clinic-recall/rights/blob-enabled": (
            "CLINIC_RECALL_DURABLE_RIGHTS_BLOB_ENABLED"
        ),
        "app/clinic-recall/rights/hmac-key-version": (
            "CLINIC_RECALL_RIGHTS_HMAC_KEY_VERSION"
        ),
        "app/clinic-recall/rights/hmac-key": "CLINIC_RECALL_RIGHTS_HMAC_KEY",
        "app/clinic-recall/rights/hmac-previous-keys-json": (
            "CLINIC_RECALL_RIGHTS_HMAC_PREVIOUS_KEYS_JSON"
        ),
        "app/clinic-recall/rights/residual-approvals-json": (
            "CLINIC_RECALL_RIGHTS_RESIDUAL_APPROVALS_JSON"
        ),
        "app/clinic-recall/retention/policy-version": (
            "CLINIC_RECALL_RETENTION_POLICY_VERSION"
        ),
        "app/clinic-recall/retention/approval-evidence-sha256": (
            "CLINIC_RECALL_RETENTION_APPROVAL_EVIDENCE_SHA256"
        ),
    }
    for key, environment_name in expected.items():
        assert APPCONFIG_KEY_MAP[key] == environment_name
    assert "app/clinic-recall/rights/enabled" not in APPCONFIG_KEY_MAP
    assert "app/clinic-recall/retention/scheduler-enabled" not in APPCONFIG_KEY_MAP


def test_appconfig_maps_cliniko_configuration_and_clears_stale_snapshot(
    monkeypatch,
) -> None:
    expected = {
        "app/clinic-recall/cliniko/api-key": "CLINIC_RECALL_CLINIKO_API_KEY",
        "app/clinic-recall/cliniko/shard": "CLINIC_RECALL_CLINIKO_SHARD",
        "app/clinic-recall/cliniko/user-agent": "CLINIC_RECALL_CLINIKO_USER_AGENT",
        "app/clinic-recall/cliniko/timeout-seconds": (
            "CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS"
        ),
        "app/clinic-recall/cliniko/per-page": "CLINIC_RECALL_CLINIKO_PER_PAGE",
        "app/clinic-recall/cliniko/max-pages": "CLINIC_RECALL_CLINIKO_MAX_PAGES",
        "app/clinic-recall/cliniko/max-items": "CLINIC_RECALL_CLINIKO_MAX_ITEMS",
        "app/clinic-recall/cliniko/enabled": "CLINIC_RECALL_CLINIKO_SYNC_ENABLED",
    }
    for key, environment_name in expected.items():
        assert APPCONFIG_KEY_MAP[key] == environment_name

    monkeypatch.setattr(appconfig_provider, "APPCONFIG_ENABLED", True)
    monkeypatch.setattr(
        appconfig_provider,
        "_env_override_allowed_when_appconfig_loaded",
        lambda _name: False,
    )
    full_payload = {
        "app/clinic-recall/cliniko/api-key": "synthetic-appconfig-key-uk2",
        "app/clinic-recall/cliniko/shard": "uk2",
        "app/clinic-recall/cliniko/user-agent": (
            "Clinic Recall Tests (cliniko-tests@example.invalid)"
        ),
        "app/clinic-recall/cliniko/enabled": "true",
    }
    synced = appconfig_provider.sync_appconfig_to_env(full_payload)
    assert synced["CLINIC_RECALL_CLINIKO_SYNC_ENABLED"] == "true"
    assert appconfig_provider.os.environ["CLINIC_RECALL_CLINIKO_API_KEY"] == (
        "synthetic-appconfig-key-uk2"
    )

    partial = appconfig_provider.sync_appconfig_to_env(
        {
            "app/clinic-recall/cliniko/shard": "uk2",
            "app/clinic-recall/cliniko/enabled": "true",
        }
    )
    assert partial["CLINIC_RECALL_CLINIKO_SYNC_ENABLED"] == "false"
    assert appconfig_provider.os.environ["CLINIC_RECALL_CLINIKO_SYNC_ENABLED"] == "false"
    assert "CLINIC_RECALL_CLINIKO_API_KEY" not in appconfig_provider.os.environ
    assert "CLINIC_RECALL_CLINIKO_USER_AGENT" not in appconfig_provider.os.environ

    monkeypatch.setenv("CLINIC_RECALL_CLINIKO_SYNC_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_CLINIKO_API_KEY", "fixture-uk2")
    monkeypatch.setattr(appconfig_provider, "_load_config_from_appconfig", lambda: None)
    assert appconfig_provider.bootstrap_appconfig() is False
    assert "CLINIC_RECALL_CLINIKO_API_KEY" not in appconfig_provider.os.environ
    assert appconfig_provider.os.environ["CLINIC_RECALL_CLINIKO_SYNC_ENABLED"] == "false"


def test_appconfig_handoff_switches_fail_closed_on_partial_and_failed_load(
    monkeypatch,
) -> None:
    expected = {
        "app/clinic-recall/handoff-notification-enabled": (
            "CLINIC_RECALL_HANDOFF_NOTIFICATION_ENABLED"
        ),
        "app/clinic-recall/handoff-ageing-enabled": (
            "CLINIC_RECALL_HANDOFF_AGEING_ENABLED"
        ),
        "app/clinic-recall/handoff-delivery-callback-enabled": (
            "CLINIC_RECALL_HANDOFF_DELIVERY_CALLBACK_ENABLED"
        ),
    }
    for key, environment_name in expected.items():
        assert APPCONFIG_KEY_MAP[key] == environment_name

    monkeypatch.setattr(appconfig_provider, "APPCONFIG_ENABLED", True)
    monkeypatch.setattr(
        appconfig_provider,
        "_env_override_allowed_when_appconfig_loaded",
        lambda _name: False,
    )
    synced = appconfig_provider.sync_appconfig_to_env(
        {key: "true" for key in expected}
    )
    for environment_name in expected.values():
        assert synced[environment_name] == "true"

    partial = appconfig_provider.sync_appconfig_to_env(
        {"app/clinic-recall/handoff-notification-enabled": "true"}
    )
    assert partial["CLINIC_RECALL_HANDOFF_NOTIFICATION_ENABLED"] == "true"
    assert appconfig_provider.os.environ["CLINIC_RECALL_HANDOFF_AGEING_ENABLED"] == "false"
    assert (
        appconfig_provider.os.environ[
            "CLINIC_RECALL_HANDOFF_DELIVERY_CALLBACK_ENABLED"
        ]
        == "false"
    )

    monkeypatch.setattr(appconfig_provider, "_load_config_from_appconfig", lambda: None)
    for operation in (appconfig_provider.bootstrap_appconfig, appconfig_provider.refresh_cache):
        for environment_name in expected.values():
            monkeypatch.setenv(environment_name, "true")
        operation()
        for environment_name in expected.values():
            assert appconfig_provider.os.environ[environment_name] == "false"


def test_appconfig_maps_pilot_controls_and_sets_refresh_only_after_success(
    monkeypatch,
) -> None:
    expected = {
        "app/clinic-recall/pilot/outreach-enabled": "CLINIC_RECALL_PILOT_OUTREACH_ENABLED",
        "app/clinic-recall/pilot/voice-enabled": "CLINIC_RECALL_PILOT_VOICE_ENABLED",
        "app/clinic-recall/pilot/recording-enabled": "CLINIC_RECALL_PILOT_RECORDING_ENABLED",
        "app/clinic-recall/pilot/config-max-age-seconds": "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS",
        "app/clinic-recall/pilot/environment": "CLINIC_RECALL_PILOT_ENVIRONMENT",
        "app/clinic-recall/pilot/release-identity": "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
    }
    for key, environment_name in expected.items():
        assert APPCONFIG_KEY_MAP[key] == environment_name
    full_payload = {
        "app/clinic-recall/pilot/outreach-enabled": "true",
        "app/clinic-recall/pilot/voice-enabled": "true",
        "app/clinic-recall/pilot/recording-enabled": "false",
        "app/clinic-recall/pilot/config-max-age-seconds": "60",
        "app/clinic-recall/pilot/environment": "production",
        "app/clinic-recall/pilot/release-identity": "sha256:release-r1",
    }
    monkeypatch.setattr(appconfig_provider, "APPCONFIG_ENABLED", True)
    monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT", "ambient-stale")
    monkeypatch.setattr(appconfig_provider, "_load_config_from_appconfig", lambda: None)
    assert appconfig_provider.bootstrap_appconfig() is False
    assert "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT" not in appconfig_provider.os.environ

    monkeypatch.setattr(
        appconfig_provider,
        "_load_config_from_appconfig",
        lambda: {"app/clinic-recall/pilot/outreach-enabled": "true"},
    )
    assert appconfig_provider.bootstrap_appconfig() is True
    assert "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT" not in appconfig_provider.os.environ
    assert appconfig_provider.os.environ["CLINIC_RECALL_PILOT_OUTREACH_ENABLED"] == "true"

    monkeypatch.setattr(appconfig_provider, "_load_config_from_appconfig", lambda: full_payload)
    assert appconfig_provider.bootstrap_appconfig() is True
    refreshed_at = datetime.fromisoformat(
        appconfig_provider.os.environ["CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT"]
    )
    assert refreshed_at.tzinfo is not None

    monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT", "ambient-stale")
    monkeypatch.setattr(appconfig_provider, "_load_config_from_appconfig", lambda: None)
    appconfig_provider.refresh_cache()
    assert "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT" not in appconfig_provider.os.environ

    monkeypatch.setattr(
        appconfig_provider,
        "_load_config_from_appconfig",
        lambda: {"app/clinic-recall/pilot/voice-enabled": "true"},
    )
    appconfig_provider.refresh_cache()
    assert "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT" not in appconfig_provider.os.environ

    monkeypatch.setattr(appconfig_provider, "_load_config_from_appconfig", lambda: full_payload)
    appconfig_provider.refresh_cache()
    assert "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT" in appconfig_provider.os.environ

    monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT", "ambient-stale")
    monkeypatch.setattr(
        appconfig_provider,
        "_env_override_allowed_when_appconfig_loaded",
        lambda environment_name: environment_name == "CLINIC_RECALL_PILOT_RECORDING_ENABLED",
    )
    appconfig_provider.refresh_cache()
    assert "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT" not in appconfig_provider.os.environ


def test_appconfig_maps_voicelive_token_warmup_settings() -> None:
    assert (
        APPCONFIG_KEY_MAP["app/voice/voicelive/token-warmup-enabled"]
        == "VOICELIVE_TOKEN_WARMUP_ENABLED"
    )
    assert (
        APPCONFIG_KEY_MAP["app/voice/voicelive/token-warmup-timeout-seconds"]
        == "VOICELIVE_TOKEN_WARMUP_TIMEOUT_SECONDS"
    )


def test_appconfig_marks_recording_disclosure_fresh_only_for_complete_current_load(
    monkeypatch,
) -> None:
    full_payload = {
        "app/clinic-recall/recording/disclosure-approved": "false",
        "app/clinic-recall/recording/disclosure-text": (
            "Synthetic recording disclosure used only by configuration tests."
        ),
        "app/clinic-recall/recording/disclosure-version": "synthetic-pr09-v1",
    }
    marker = "CLINIC_RECALL_RECORDING_DISCLOSURE_REFRESHED_AT"
    monkeypatch.setattr(appconfig_provider, "APPCONFIG_ENABLED", True)
    monkeypatch.setenv(marker, "ambient-stale")
    monkeypatch.setattr(appconfig_provider, "_load_config_from_appconfig", lambda: None)
    assert appconfig_provider.bootstrap_appconfig() is False
    assert marker not in appconfig_provider.os.environ

    monkeypatch.setattr(
        appconfig_provider,
        "_load_config_from_appconfig",
        lambda: {"app/clinic-recall/recording/disclosure-approved": "false"},
    )
    assert appconfig_provider.bootstrap_appconfig() is True
    assert marker not in appconfig_provider.os.environ

    monkeypatch.setattr(appconfig_provider, "_load_config_from_appconfig", lambda: full_payload)
    assert appconfig_provider.bootstrap_appconfig() is True
    refreshed_at = datetime.fromisoformat(appconfig_provider.os.environ[marker])
    assert refreshed_at.tzinfo is not None

    monkeypatch.setenv(marker, "ambient-stale")
    monkeypatch.setattr(
        appconfig_provider,
        "_env_override_allowed_when_appconfig_loaded",
        lambda environment_name: (
            environment_name == "CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT"
        ),
    )
    appconfig_provider.refresh_cache()
    assert marker not in appconfig_provider.os.environ
