from pathlib import Path


def test_phase0_postgres_seed_has_ten_synthetic_rows() -> None:
    seed_sql = Path("infra/postgres/phase0_missed_appointments.sql").read_text(
        encoding="utf-8"
    )

    assert seed_sql.count("appt-phase0-") == 10
    assert "Synthetic Patient 01" in seed_sql
    assert "phase0_missed_appointments" in seed_sql