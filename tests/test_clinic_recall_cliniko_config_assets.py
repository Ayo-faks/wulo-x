from pathlib import Path

APPCONFIG_SYNC = Path("devops/scripts/azd/helpers/sync-appconfig.sh")


def test_cliniko_appconfig_sync_is_false_first_reference_then_true_last() -> None:
    source = APPCONFIG_SYNC.read_text(encoding="utf-8")
    block = source.split("# Clinic Recall Cliniko sync (PR-05)", 1)[1].split(
        "# End Clinic Recall Cliniko sync",
        1,
    )[0]

    false_write = 'set_kv "app/clinic-recall/cliniko/enabled" "false"'
    reference_write = (
        'set_kv_ref "app/clinic-recall/cliniko/api-key" '
        '"clinic-recall-cliniko-api-key"'
    )
    true_write = 'set_kv "app/clinic-recall/cliniko/enabled" "true"'
    assert block.index(false_write) < block.index(reference_write) < block.index(true_write)
    assert 'delete_kv "app/clinic-recall/cliniko/api-key"' in block
    assert "az keyvault secret set" not in block
    assert "CLINIC_RECALL_CLINIKO_API_KEY" not in block
    assert "cliniko_configuration_failed=true" in block


def test_cliniko_appconfig_sync_uses_exact_non_secret_keys() -> None:
    source = APPCONFIG_SYNC.read_text(encoding="utf-8")
    block = source.split("# Clinic Recall Cliniko sync (PR-05)", 1)[1].split(
        "# End Clinic Recall Cliniko sync",
        1,
    )[0]
    for key in (
        "shard",
        "user-agent",
        "timeout-seconds",
        "per-page",
        "max-pages",
        "max-items",
    ):
        assert f'app/clinic-recall/cliniko/{key}' in block


def test_pr07_switches_are_false_first_validated_and_true_last() -> None:
    source = APPCONFIG_SYNC.read_text(encoding="utf-8")
    block = source.split("# Clinic Recall Cliniko sync (PR-05)", 1)[1].split(
        "# End Clinic Recall Cliniko sync",
        1,
    )[0]
    base_true = 'set_kv "app/clinic-recall/cliniko/enabled" "true"'
    keys = (
        "app/clinic-recall/cliniko/write-enabled",
        "app/clinic-recall/cliniko/reconciliation-enabled",
        "app/clinic-recall/booking-confirmation/enabled",
    )
    for key in keys:
        false_write = f'set_kv "{key}" "false"'
        true_write = f'set_kv "{key}" "true"'
        assert block.index(false_write) < block.index(base_true) < block.index(
            true_write
        )
        assert f"{key} must be true or false" in block