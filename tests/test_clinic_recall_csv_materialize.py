"""PR-08 strict CSV materializer tests: bounds, encoding, injection, safety.

Fixture-driven: every case file lives in ``tests/fixtures/csv/pr08/`` and is
sealed by ``manifest.json``. Limit boundaries are exercised at runtime with
patched module bounds so exact at-limit/over-limit semantics stay fast.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest
from src.clinic_recall.enums import CsvValidationReason
from src.clinic_recall.sync import CSV_SCHEMA_VERSION, CsvSyncError, CsvSyncSource, csv_adapter

FIXTURES = Path(__file__).parent / "fixtures" / "csv" / "pr08"

VALID_BYTES = (FIXTURES / "valid_multi.csv").read_bytes()

_HEADER = "appointment_source_ref,patient_source_ref,patient_name,status,start_at"


def _row(ref: str, patient: str = "PAT-X-1", name: str = "Test Patient Range") -> str:
    return f"{ref},{patient},{name},missed,2026-06-20T09:00:00+00:00"


def _reasons(materialization) -> set[str]:
    return {error.reason.value for error in materialization.errors}


# --------------------------------------------------------------------------- #
# Manifest integrity and synthetic-data scan
# --------------------------------------------------------------------------- #
def test_fixture_manifest_matches_files_and_contains_no_real_pii():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert manifest["authority"] == "synthetic_contract_verified"
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest
    # Contact values stay inside reserved synthetic ranges.
    for name in manifest["files"]:
        payload = (FIXTURES / name).read_bytes().decode("utf-8", errors="ignore")
        for token in payload.replace(",", " ").split():
            if token.startswith("+44") and not token.startswith("+4477009"):
                pytest.fail(f"{name} contains a non-reserved phone value")
            if "@" in token and "." in token.split("@")[-1]:
                assert token.split("@")[-1].endswith("invalid"), f"{name}: {token}"


# --------------------------------------------------------------------------- #
# Golden path
# --------------------------------------------------------------------------- #
def test_materialize_valid_multi():
    result = CsvSyncSource.materialize(VALID_BYTES)
    assert result.valid is True
    assert result.errors == ()
    assert result.file_sha256 == hashlib.sha256(VALID_BYTES).hexdigest()
    assert result.schema_version == CSV_SCHEMA_VERSION
    assert result.total_rows == 4
    assert result.valid_row_count == 4
    assert result.invalid_row_count == 0
    assert result.patient_count == 3
    assert result.appointment_count == 4
    # Presence-aware consent authority.
    by_ref = {p.source_ref: p for p in result.patients}
    assert by_ref["PAT-PR08-001"].consent_flags == {
        "sms": True,
        "email": False,
        "call": False,
    }
    gamma = by_ref["PAT-PR08-003"]
    assert gamma.consent_flags is None  # all consent cells empty: no authority
    assert gamma.opt_out_flags == {"sms": True}  # only the non-empty cell
    source = result.source()
    assert len(source.fetch_patients()) == 3
    assert len(source.fetch_appointments()) == 4


def test_materialize_is_deterministic():
    first = CsvSyncSource.materialize(VALID_BYTES)
    second = CsvSyncSource.materialize(VALID_BYTES)
    assert first.file_sha256 == second.file_sha256
    assert first.validation_summary_sha256 == second.validation_summary_sha256


def test_bom_is_accepted_once_at_start():
    result = CsvSyncSource.materialize((FIXTURES / "bom_valid.csv").read_bytes())
    assert result.valid is True


# --------------------------------------------------------------------------- #
# Fixture matrix: every invalid file maps to closed reasons
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("fixture", "expected_reasons"),
    [
        ("empty.csv", {CsvValidationReason.EMPTY_FILE}),
        ("header_only.csv", {CsvValidationReason.EMPTY_FILE}),
        ("nul_byte.csv", {CsvValidationReason.CONTROL_CHARACTER}),
        ("invalid_utf8.bin", {CsvValidationReason.INVALID_ENCODING}),
        ("interior_bom.csv", {CsvValidationReason.CONTROL_CHARACTER}),
        ("missing_required_column.csv", {CsvValidationReason.MISSING_REQUIRED_COLUMN}),
        ("duplicate_header.csv", {CsvValidationReason.DUPLICATE_COLUMN}),
        ("unknown_column.csv", {CsvValidationReason.UNKNOWN_COLUMN}),
        ("malformed_quoting.csv", {CsvValidationReason.MALFORMED_CSV}),
        ("control_characters.csv", {CsvValidationReason.CONTROL_CHARACTER}),
        ("naive_timestamp.csv", {CsvValidationReason.NAIVE_TIMESTAMP}),
        ("row_overflow.csv", {CsvValidationReason.ROW_FIELD_OVERFLOW}),
        ("row_missing_fields.csv", {CsvValidationReason.ROW_MISSING_FIELD}),
        (
            "duplicate_appointment_ref.csv",
            {CsvValidationReason.DUPLICATE_APPOINTMENT_REF},
        ),
        (
            "conflicting_patient_facts.csv",
            {CsvValidationReason.CONFLICTING_PATIENT_FACT},
        ),
    ],
)
def test_invalid_fixture_reasons(fixture, expected_reasons):
    result = CsvSyncSource.materialize((FIXTURES / fixture).read_bytes())
    assert result.valid is False
    assert result.valid_row_count + result.invalid_row_count == result.total_rows
    assert {error.reason for error in result.errors} >= expected_reasons
    assert result.patients == ()
    assert result.appointments == ()
    with pytest.raises(CsvSyncError):
        result.source()


def test_formula_injection_rejected_per_variant():
    result = CsvSyncSource.materialize((FIXTURES / "formula_injection.csv").read_bytes())
    assert result.valid is False
    formula_errors = [
        error for error in result.errors if error.reason == CsvValidationReason.FORMULA_PREFIX
    ]
    # =, +, -, @, leading-space-then-=, and full-width = across six rows.
    assert len(formula_errors) == 6
    assert {error.field for error in formula_errors} <= {
        "appointment_source_ref",
        "patient_source_ref",
    }


def test_blank_header_name_is_rejected():
    data = (_HEADER + ",\n" + _row("APPT-BLANK-HEADER") + ",\n").encode()
    result = CsvSyncSource.materialize(data)
    assert result.valid is False
    assert CsvValidationReason.UNKNOWN_COLUMN.value in _reasons(result)


def test_tab_and_cr_led_refs_rejected():
    result = CsvSyncSource.materialize((FIXTURES / "formula_tab_cr.csv").read_bytes())
    assert result.valid is False
    reasons = _reasons(result)
    assert reasons & {
        CsvValidationReason.FORMULA_PREFIX.value,
        CsvValidationReason.CONTROL_CHARACTER.value,
    }


def test_invalid_values_map_to_specific_reasons():
    result = CsvSyncSource.materialize((FIXTURES / "invalid_values.csv").read_bytes())
    assert result.valid is False
    assert _reasons(result) >= {
        CsvValidationReason.INVALID_PHONE.value,
        CsvValidationReason.INVALID_EMAIL.value,
        CsvValidationReason.INVALID_VALUE.value,
        CsvValidationReason.INVALID_DECIMAL.value,
        CsvValidationReason.INVALID_BOOLEAN.value,
        CsvValidationReason.INVALID_TIMESTAMP.value,
    }


# --------------------------------------------------------------------------- #
# Errors are safe: bounded fields, no raw values
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fixture",
    [
        "conflicting_patient_facts.csv",
        "invalid_values.csv",
        "formula_injection.csv",
        "malformed_quoting.csv",
        "duplicate_appointment_ref.csv",
    ],
)
def test_errors_never_contain_raw_values(fixture):
    data = (FIXTURES / fixture).read_bytes()
    result = CsvSyncSource.materialize(data)
    allowed_fields = set(csv_adapter.ALLOWED_COLUMNS) | {"file", "header", "row"}
    rendered = repr(result.errors) + repr(result.error_reason_counts)
    for error in result.errors:
        assert error.field in allowed_fields
    for marker in ("PAT-", "APPT-", "Test Patient", "+4477", "clinic-test.invalid"):
        assert marker not in rendered
    # Aggregate counts match the emitted errors exactly.
    assert sum(result.error_reason_counts.values()) == len(result.errors)


def test_error_collection_is_capped():
    rows = "\n".join(_row("=BAD", f"PAT-CAP-{i}") for i in range(150))
    data = (_HEADER + "\n" + rows + "\n").encode()
    result = CsvSyncSource.materialize(data)
    assert result.valid is False
    assert len(result.errors) <= csv_adapter.MAX_ERRORS
    assert result.errors[-1].reason == CsvValidationReason.TOO_MANY_ERRORS


# --------------------------------------------------------------------------- #
# Exact at-limit / over-limit boundaries
# --------------------------------------------------------------------------- #
def test_byte_cap_boundary(monkeypatch):
    at_limit = (_HEADER + "\n" + _row("APPT-LIM-1") + "\n").encode()
    monkeypatch.setattr(csv_adapter, "MAX_BYTES", len(at_limit))
    assert CsvSyncSource.materialize(at_limit).valid is True
    over = at_limit + b"x"
    result = CsvSyncSource.materialize(over)
    assert result.valid is False
    assert CsvValidationReason.FILE_TOO_LARGE.value in _reasons(result)


def test_row_cap_boundary(monkeypatch):
    monkeypatch.setattr(csv_adapter, "MAX_ROWS", 3)
    rows = [_row(f"APPT-ROW-{i}", f"PAT-ROW-{i}") for i in range(3)]
    data = (_HEADER + "\n" + "\n".join(rows) + "\n").encode()
    assert CsvSyncSource.materialize(data).valid is True
    rows.append(_row("APPT-ROW-3", "PAT-ROW-3"))
    over = (_HEADER + "\n" + "\n".join(rows) + "\n").encode()
    result = CsvSyncSource.materialize(over)
    assert result.valid is False
    assert CsvValidationReason.TOO_MANY_ROWS.value in _reasons(result)


def test_real_module_bounds_are_unchanged():
    assert csv_adapter.MAX_BYTES == 50 * 1024 * 1024
    assert csv_adapter.MAX_ROWS == 200_000
    assert csv_adapter.MAX_ERRORS == 100


@pytest.mark.parametrize(
    ("column", "at_limit", "over_reason"),
    [
        ("appointment_source_ref", 255, CsvValidationReason.INVALID_SOURCE_REF),
        ("patient_name", 200, CsvValidationReason.FIELD_TOO_LONG),
    ],
)
def test_field_length_boundaries(column, at_limit, over_reason):
    def build(length: int) -> bytes:
        cells = {
            "appointment_source_ref": "APPT-LEN-1",
            "patient_source_ref": "PAT-LEN-1",
            "patient_name": "Test Patient Len",
            "status": "missed",
            "start_at": "2026-06-20T09:00:00+00:00",
        }
        cells[column] = ("N" if column == "patient_name" else "R") * length
        line = ",".join(cells[c] for c in _HEADER.split(","))
        return (_HEADER + "\n" + line + "\n").encode()

    assert CsvSyncSource.materialize(build(at_limit)).valid is True
    result = CsvSyncSource.materialize(build(at_limit + 1))
    assert result.valid is False
    assert over_reason.value in _reasons(result)


def test_generic_cell_bound():
    long_cell = "x" * (csv_adapter.MAX_CELL_CHARS + 1)
    data = (
        _HEADER + ",patient_email\n" + _row("APPT-CELL-1") + f",{long_cell}@clinic-test.invalid\n"
    ).encode()
    result = CsvSyncSource.materialize(data)
    assert result.valid is False
    assert CsvValidationReason.FIELD_TOO_LONG.value in _reasons(result)


def test_field_size_limit_restored_after_parse():
    before = csv.field_size_limit()
    CsvSyncSource.materialize(VALID_BYTES)
    CsvSyncSource.materialize((FIXTURES / "malformed_quoting.csv").read_bytes())
    assert csv.field_size_limit() == before


# --------------------------------------------------------------------------- #
# Present-but-malformed contacts are errors, not silent None
# --------------------------------------------------------------------------- #
def test_present_malformed_phone_is_an_error_not_coercion():
    data = (
        _HEADER
        + ",patient_phone\n"
        + _row("APPT-PH-1")
        + ",07700900123\n"  # not E.164: import must not silently drop it
    ).encode()
    result = CsvSyncSource.materialize(data)
    assert result.valid is False
    assert CsvValidationReason.INVALID_PHONE.value in _reasons(result)
    # The legacy trusted-file surface keeps its coercion contract.
    legacy = CsvSyncSource.from_text(data.decode())
    assert legacy.fetch_patients()[0].phone is None
