"""CSV sync adapter (the first, always-available ingestion path).

Parses a single denormalised appointments CSV (patient details inline) into
validated rows and exposes them as a :class:`SyncSource`. Defensive by design
(``SECURITY.md``): UTF-8 only, byte/row caps to bound resource use, a required
header allowlist, and per-row validation via :class:`CsvAppointmentRow` (which
itself guards against formula injection and malformed contact details).

Two entry points share this module's row vocabulary:

* :meth:`CsvSyncSource.from_text` / :meth:`CsvSyncSource.from_path` — the
  legacy trusted-file surface (first error raises, unknown columns ignored).
* :meth:`CsvSyncSource.materialize` — the PR-08 import boundary. Byte-exact
  hashing, a closed header vocabulary, per-field bounds, presence-aware
  consent authority, cross-row consistency, and a bounded closed vocabulary
  of safe errors that never contain raw cell values.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from pydantic import ValidationError

from ..enums import CsvValidationReason
from ..schemas import REQUIRED_COLUMNS, CsvAppointmentRow
from .base import NormalizedAppointment, NormalizedPatient

# Bound resource usage on untrusted input.
MAX_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_ROWS = 200_000

# The import-boundary schema this materializer implements.
CSV_SCHEMA_VERSION = "wulo-csv-v1"

# Closed header vocabulary: required + known optional columns. Anything else
# is rejected rather than silently ignored at the import boundary.
OPTIONAL_COLUMNS = (
    "patient_phone",
    "patient_email",
    "value",
    "consent_sms",
    "consent_email",
    "consent_call",
    "opt_out_sms",
    "opt_out_email",
    "opt_out_call",
)
ALLOWED_COLUMNS = (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS)

_CONSENT_COLUMNS = {"consent_sms": "sms", "consent_email": "email", "consent_call": "call"}
_OPT_OUT_COLUMNS = {"opt_out_sms": "sms", "opt_out_email": "email", "opt_out_call": "call"}

# Per-field bounds aligned with the persisted schema.
MAX_SOURCE_REF_CHARS = 255
MAX_NAME_CHARS = 200
MAX_PHONE_CHARS = 32
MAX_EMAIL_CHARS = 254
MAX_CELL_CHARS = 4_096
MAX_ERRORS = 100

# Source refs are idempotency keys: a strict charset (no formula triggers, no
# separators, no control characters) is rejected rather than sanitized.
_SOURCE_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")

# Spreadsheet formula triggers (OWASP CSV Injection), including full-width
# variants and the whitespace that Excel strips before evaluating.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")
_LEADING_WHITESPACE = " \t\u00a0\u3000"

# Control characters (C0 + DEL) and the interior BOM / zero-width no-break space.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\ufeff]")

_BOM = b"\xef\xbb\xbf"

# ``csv.field_size_limit`` is process-global; serialize set/restore.
_FIELD_LIMIT_LOCK = threading.Lock()
_PARSE_FIELD_LIMIT = 64 * 1024


class CsvSyncError(ValueError):
    """Raised when a CSV is malformed, too large, or fails row validation."""


@dataclass(frozen=True)
class CsvSafeError:
    """One bounded, PII-free validation finding.

    ``record`` is the 1-based data-record ordinal (``None`` for file-level
    findings); ``line`` is the physical line when known; ``field`` is an
    allowlisted column name or ``file``/``header``/``row``. Raw cell values,
    header text, and exception messages never appear here.
    """

    reason: CsvValidationReason
    field: str = "file"
    record: int | None = None
    line: int | None = None


@dataclass(frozen=True)
class CsvMaterialization:
    """Immutable outcome of materializing one uploaded CSV byte payload.

    Carries exact hashes, bounded aggregate counts, safe errors, and — only
    when the complete file is valid — normalized records. Raw bytes, raw
    text, filenames, and row values are deliberately absent.
    """

    file_sha256: str
    schema_version: str
    total_rows: int
    valid_row_count: int
    invalid_row_count: int
    patient_count: int
    appointment_count: int
    valid: bool
    errors: tuple[CsvSafeError, ...]
    error_reason_counts: Mapping[str, int]
    validation_summary_sha256: str
    patients: tuple[NormalizedPatient, ...] = field(default=(), repr=False)
    appointments: tuple[NormalizedAppointment, ...] = field(default=(), repr=False)

    def source(self) -> MaterializedCsvSource:
        """Return the importable sync source; only valid files have one."""
        if not self.valid:
            raise CsvSyncError("materialization is not importable")
        return MaterializedCsvSource(patients=self.patients, appointments=self.appointments)


@dataclass(frozen=True)
class MaterializedCsvSource:
    """A :class:`SyncSource` over already-materialized normalized records."""

    patients: tuple[NormalizedPatient, ...]
    appointments: tuple[NormalizedAppointment, ...]
    name: str = "csv"

    def fetch_patients(self) -> Sequence[NormalizedPatient]:
        return self.patients

    def fetch_appointments(self) -> Sequence[NormalizedAppointment]:
        return self.appointments


class _ErrorCollector:
    """Bounded error accumulator with aggregate reason counts."""

    def __init__(self) -> None:
        self.errors: list[CsvSafeError] = []
        self.reason_counts: dict[str, int] = {}
        self.truncated = False

    def add(
        self,
        reason: CsvValidationReason,
        *,
        field: str = "file",
        record: int | None = None,
        line: int | None = None,
    ) -> None:
        if self.truncated:
            return
        if len(self.errors) >= MAX_ERRORS - 1:
            self.truncated = True
            self.errors.append(CsvSafeError(reason=CsvValidationReason.TOO_MANY_ERRORS))
            self.reason_counts[CsvValidationReason.TOO_MANY_ERRORS.value] = 1
            return
        self.errors.append(CsvSafeError(reason=reason, field=field, record=record, line=line))
        self.reason_counts[reason.value] = self.reason_counts.get(reason.value, 0) + 1


def _has_formula_prefix(value: str) -> bool:
    stripped = value.lstrip(_LEADING_WHITESPACE)
    return stripped.startswith(_FORMULA_TRIGGERS)


def _summary_sha256(
    *,
    total_rows: int,
    valid_row_count: int,
    invalid_row_count: int,
    patient_count: int,
    appointment_count: int,
    valid: bool,
    reason_counts: Mapping[str, int],
) -> str:
    payload = json.dumps(
        {
            "schema_version": CSV_SCHEMA_VERSION,
            "total_rows": total_rows,
            "valid_row_count": valid_row_count,
            "invalid_row_count": invalid_row_count,
            "patient_count": patient_count,
            "appointment_count": appointment_count,
            "valid": valid,
            "error_reason_counts": dict(sorted(reason_counts.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class CsvSyncSource:
    """A :class:`SyncSource` backed by an in-memory list of validated rows."""

    name = "csv"

    def __init__(self, rows: Sequence[CsvAppointmentRow]) -> None:
        self._rows = list(rows)

    @classmethod
    def from_text(cls, text: str) -> CsvSyncSource:
        """Build from raw CSV text, validating header and every row."""
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise CsvSyncError("CSV exceeds the maximum allowed size")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise CsvSyncError("CSV has no header row")
        headers = {h.strip() for h in reader.fieldnames if h}
        missing = [column for column in REQUIRED_COLUMNS if column not in headers]
        if missing:
            raise CsvSyncError(f"CSV missing required columns: {sorted(missing)}")

        rows: list[CsvAppointmentRow] = []
        for line_number, raw in enumerate(reader, start=2):  # line 1 is the header
            if line_number - 1 > MAX_ROWS:
                raise CsvSyncError(f"CSV exceeds the maximum of {MAX_ROWS} rows")
            # Drop any unnamed overflow column (csv restkey is None).
            clean = {key: value for key, value in raw.items() if key is not None}
            try:
                rows.append(CsvAppointmentRow.model_validate(clean))
            except ValidationError as exc:
                raise CsvSyncError(f"CSV row {line_number}: {exc}") from exc
        return cls(rows)

    @classmethod
    def from_path(cls, path: str | Path) -> CsvSyncSource:
        """Build from a UTF-8 CSV file on disk (size-capped)."""
        data = Path(path).read_bytes()
        if len(data) > MAX_BYTES:
            raise CsvSyncError("CSV exceeds the maximum allowed size")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CsvSyncError("CSV must be UTF-8 encoded") from exc
        return cls.from_text(text)

    @classmethod
    def materialize(cls, data: bytes) -> CsvMaterialization:
        """Materialize uploaded bytes under the strict PR-08 import contract.

        Never raises for content problems: every finding becomes a bounded
        :class:`CsvSafeError` and the result is marked not importable. The
        SHA-256 is always computed over the exact input bytes.
        """
        file_sha256 = hashlib.sha256(data).hexdigest()
        collector = _ErrorCollector()
        rows, total_rows = _parse_strict(data, collector)

        patients, appointments = _normalize_rows(rows, collector)
        valid = not collector.errors and total_rows > 0
        if total_rows == 0 and not collector.errors:
            collector.add(CsvValidationReason.EMPTY_FILE)
            valid = False

        patient_count = len({p.source_ref for p in patients}) if patients else 0
        appointment_count = len(appointments)
        valid_row_count = appointment_count
        invalid_row_count = max(0, total_rows - valid_row_count)
        summary = _summary_sha256(
            total_rows=total_rows,
            valid_row_count=valid_row_count,
            invalid_row_count=invalid_row_count,
            patient_count=patient_count,
            appointment_count=appointment_count,
            valid=valid,
            reason_counts=collector.reason_counts,
        )
        return CsvMaterialization(
            file_sha256=file_sha256,
            schema_version=CSV_SCHEMA_VERSION,
            total_rows=total_rows,
            valid_row_count=valid_row_count,
            invalid_row_count=invalid_row_count,
            patient_count=patient_count,
            appointment_count=appointment_count,
            valid=valid,
            errors=tuple(collector.errors),
            error_reason_counts=dict(collector.reason_counts),
            validation_summary_sha256=summary,
            patients=tuple(patients) if valid else (),
            appointments=tuple(appointments) if valid else (),
        )

    def fetch_patients(self) -> list[NormalizedPatient]:
        """Distinct patients (first occurrence wins) keyed by source_ref."""
        seen: dict[str, NormalizedPatient] = {}
        for row in self._rows:
            if row.patient_source_ref not in seen:
                seen[row.patient_source_ref] = NormalizedPatient(
                    source_ref=row.patient_source_ref,
                    name=row.patient_name,
                    phone=row.patient_phone,
                    email=row.patient_email,
                    consent_flags=row.consent_flags,
                    opt_out_flags=row.opt_out_flags,
                )
        return list(seen.values())

    def fetch_appointments(self) -> list[NormalizedAppointment]:
        """All appointments from the CSV in order."""
        return [
            NormalizedAppointment(
                source_ref=row.appointment_source_ref,
                patient_source_ref=row.patient_source_ref,
                status=row.status,
                start_at=row.start_at,
                value=row.value,
            )
            for row in self._rows
        ]


@dataclass(frozen=True)
class _StrictRow:
    """One header-mapped, bounds-checked raw row awaiting normalization."""

    record: int
    line: int
    cells: Mapping[str, str]
    present: frozenset[str]


def _decode_upload(data: bytes, collector: _ErrorCollector) -> str | None:
    if not data:
        collector.add(CsvValidationReason.EMPTY_FILE)
        return None
    if len(data) > MAX_BYTES:
        collector.add(CsvValidationReason.FILE_TOO_LARGE)
        return None
    if b"\x00" in data:
        collector.add(CsvValidationReason.CONTROL_CHARACTER)
        return None
    if data.startswith(_BOM):
        data = data[len(_BOM):]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        collector.add(CsvValidationReason.INVALID_ENCODING)
        return None


def _read_header(
    reader: Iterator[list[str]], collector: _ErrorCollector
) -> list[str] | None:
    try:
        raw_header = next(reader)
    except StopIteration:
        collector.add(CsvValidationReason.EMPTY_FILE)
        return None
    except csv.Error:
        collector.add(CsvValidationReason.MALFORMED_CSV, field="header")
        return None
    header = [column.strip() for column in raw_header]
    ok = True
    seen: set[str] = set()
    for column in header:
        if not column:
            collector.add(CsvValidationReason.UNKNOWN_COLUMN, field="header")
            ok = False
            continue
        if column in seen:
            collector.add(CsvValidationReason.DUPLICATE_COLUMN, field="header")
            ok = False
        seen.add(column)
        if column not in ALLOWED_COLUMNS:
            collector.add(CsvValidationReason.UNKNOWN_COLUMN, field="header")
            ok = False
    for column in REQUIRED_COLUMNS:
        if column not in seen:
            collector.add(
                CsvValidationReason.MISSING_REQUIRED_COLUMN, field=column
            )
            ok = False
    return header if ok else None


def _parse_strict(
    data: bytes, collector: _ErrorCollector
) -> tuple[list[_StrictRow], int]:
    """Read bytes into bounds-checked rows under the closed header vocabulary."""
    text = _decode_upload(data, collector)
    if text is None:
        return [], 0

    rows: list[_StrictRow] = []
    total_rows = 0
    with _FIELD_LIMIT_LOCK:
        previous_limit = csv.field_size_limit(_PARSE_FIELD_LIMIT)
        try:
            reader = csv.reader(io.StringIO(text, newline=""), strict=True)
            header = _read_header(reader, collector)
            if header is None:
                # Count the remaining data rows for display metadata only.
                try:
                    total_rows = sum(1 for _ in reader)
                except csv.Error:
                    pass
                return [], total_rows
            width = len(header)
            record = 0
            while True:
                try:
                    raw = next(reader)
                except StopIteration:
                    break
                except csv.Error:
                    collector.add(
                        CsvValidationReason.MALFORMED_CSV,
                        field="row",
                        record=record + 1,
                    )
                    break
                record += 1
                total_rows = record
                if record > MAX_ROWS:
                    collector.add(CsvValidationReason.TOO_MANY_ROWS)
                    break
                if collector.truncated:
                    continue
                line = reader.line_num
                if len(raw) > width:
                    collector.add(
                        CsvValidationReason.ROW_FIELD_OVERFLOW,
                        field="row",
                        record=record,
                        line=line,
                    )
                    continue
                if len(raw) < width:
                    collector.add(
                        CsvValidationReason.ROW_MISSING_FIELD,
                        field="row",
                        record=record,
                        line=line,
                    )
                    continue
                cells = dict(zip(header, raw, strict=True))
                present = frozenset(
                    column for column, value in cells.items() if value.strip() != ""
                )
                if _check_cells(cells, collector, record=record, line=line):
                    rows.append(
                        _StrictRow(record=record, line=line, cells=cells, present=present)
                    )
        finally:
            csv.field_size_limit(previous_limit)
    return rows, total_rows


def _check_cells(
    cells: Mapping[str, str],
    collector: _ErrorCollector,
    *,
    record: int,
    line: int,
) -> bool:
    """Bounds, charset, control-character, and contact checks on raw cells."""
    ok = True

    def fail(reason: CsvValidationReason, column: str) -> None:
        nonlocal ok
        ok = False
        collector.add(reason, field=column, record=record, line=line)

    for column, value in cells.items():
        if len(value) > MAX_CELL_CHARS:
            fail(CsvValidationReason.FIELD_TOO_LONG, column)
            continue
        if _CONTROL_RE.search(value):
            fail(CsvValidationReason.CONTROL_CHARACTER, column)

    for column in ("appointment_source_ref", "patient_source_ref"):
        value = cells.get(column, "").strip()
        if not value:
            fail(CsvValidationReason.MISSING_VALUE, column)
        elif _has_formula_prefix(value):
            fail(CsvValidationReason.FORMULA_PREFIX, column)
        elif len(value) > MAX_SOURCE_REF_CHARS or not _SOURCE_REF_RE.match(value):
            fail(CsvValidationReason.INVALID_SOURCE_REF, column)

    name = cells.get("patient_name", "").strip()
    if not name:
        fail(CsvValidationReason.MISSING_VALUE, "patient_name")
    elif len(name) > MAX_NAME_CHARS:
        fail(CsvValidationReason.FIELD_TOO_LONG, "patient_name")

    phone = cells.get("patient_phone", "").strip()
    if phone:
        from ..validation import is_valid_e164

        if len(phone) > MAX_PHONE_CHARS or not is_valid_e164(phone):
            fail(CsvValidationReason.INVALID_PHONE, "patient_phone")

    email = cells.get("patient_email", "").strip()
    if email:
        from ..validation import is_valid_email

        if _has_formula_prefix(email):
            fail(CsvValidationReason.FORMULA_PREFIX, "patient_email")
        elif len(email) > MAX_EMAIL_CHARS or not is_valid_email(email):
            fail(CsvValidationReason.INVALID_EMAIL, "patient_email")

    return ok


_PYDANTIC_REASONS: tuple[tuple[str, str, CsvValidationReason], ...] = (
    ("start_at", "timezone", CsvValidationReason.NAIVE_TIMESTAMP),
    ("start_at", "", CsvValidationReason.INVALID_TIMESTAMP),
    ("status", "", CsvValidationReason.INVALID_VALUE),
    ("value", "", CsvValidationReason.INVALID_DECIMAL),
    ("consent_sms", "", CsvValidationReason.INVALID_BOOLEAN),
    ("consent_email", "", CsvValidationReason.INVALID_BOOLEAN),
    ("consent_call", "", CsvValidationReason.INVALID_BOOLEAN),
    ("opt_out_sms", "", CsvValidationReason.INVALID_BOOLEAN),
    ("opt_out_email", "", CsvValidationReason.INVALID_BOOLEAN),
    ("opt_out_call", "", CsvValidationReason.INVALID_BOOLEAN),
)


def _map_validation_error(
    exc: ValidationError, collector: _ErrorCollector, *, record: int, line: int
) -> None:
    """Map Pydantic findings onto the closed reason vocabulary; no raw values."""
    for error in exc.errors(include_input=False, include_url=False):
        loc = str(error.get("loc", ("row",))[0]) if error.get("loc") else "row"
        message = str(error.get("msg", ""))
        for column, needle, reason in _PYDANTIC_REASONS:
            if loc == column and (not needle or needle in message):
                collector.add(reason, field=column, record=record, line=line)
                break
        else:
            field_name = loc if loc in ALLOWED_COLUMNS else "row"
            collector.add(
                CsvValidationReason.INVALID_VALUE,
                field=field_name,
                record=record,
                line=line,
            )


@dataclass(frozen=True)
class _PatientFacts:
    """Authoritative patient facts used for cross-row consistency."""

    name: str
    phone: str | None
    email: str | None
    consent: tuple[tuple[str, bool], ...]
    opt_out: tuple[tuple[str, bool], ...]
    authoritative_fields: tuple[str, ...]
    record: int


def _authority_flags(
    row: CsvAppointmentRow, present: frozenset[str], columns: Mapping[str, str]
) -> dict[str, bool] | None:
    """Per-channel flags limited to columns present with a non-empty cell."""
    flags = {
        channel: bool(getattr(row, column))
        for column, channel in columns.items()
        if column in present
    }
    return flags or None


def _normalize_rows(
    rows: Sequence[_StrictRow], collector: _ErrorCollector
) -> tuple[list[NormalizedPatient], list[NormalizedAppointment]]:
    """Validate rows, enforce cross-row consistency, and normalize records."""
    patients: dict[str, NormalizedPatient] = {}
    facts: dict[str, _PatientFacts] = {}
    appointments: list[NormalizedAppointment] = []
    seen_appointments: set[str] = set()

    for strict_row in rows:
        if collector.truncated:
            break
        try:
            row = CsvAppointmentRow.model_validate(dict(strict_row.cells))
        except ValidationError as exc:
            _map_validation_error(
                exc, collector, record=strict_row.record, line=strict_row.line
            )
            continue

        if row.appointment_source_ref in seen_appointments:
            collector.add(
                CsvValidationReason.DUPLICATE_APPOINTMENT_REF,
                field="appointment_source_ref",
                record=strict_row.record,
                line=strict_row.line,
            )
            continue
        seen_appointments.add(row.appointment_source_ref)

        consent = _authority_flags(row, strict_row.present, _CONSENT_COLUMNS)
        opt_out = _authority_flags(row, strict_row.present, _OPT_OUT_COLUMNS)
        authority = {"name"}
        if "patient_phone" in strict_row.present:
            authority.add("phone")
        if "patient_email" in strict_row.present:
            authority.add("email")
        authoritative_fields = frozenset(authority)
        row_facts = _PatientFacts(
            name=row.patient_name,
            phone=row.patient_phone,
            email=row.patient_email,
            consent=tuple(sorted((consent or {}).items())),
            opt_out=tuple(sorted((opt_out or {}).items())),
            authoritative_fields=tuple(sorted(authoritative_fields)),
            record=strict_row.record,
        )
        known = facts.get(row.patient_source_ref)
        if known is None:
            facts[row.patient_source_ref] = row_facts
            patients[row.patient_source_ref] = NormalizedPatient(
                source_ref=row.patient_source_ref,
                name=row.patient_name,
                phone=row.patient_phone,
                email=row.patient_email,
                consent_flags=consent,
                opt_out_flags=opt_out,
                authoritative_fields=authoritative_fields,
            )
        elif replace(known, record=row_facts.record) != row_facts:
            collector.add(
                CsvValidationReason.CONFLICTING_PATIENT_FACT,
                field="patient_source_ref",
                record=strict_row.record,
                line=strict_row.line,
            )
            continue

        appointments.append(
            NormalizedAppointment(
                source_ref=row.appointment_source_ref,
                patient_source_ref=row.patient_source_ref,
                status=row.status,
                start_at=row.start_at,
                value=row.value,
            )
        )

    return list(patients.values()), appointments
