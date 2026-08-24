"""Validation helpers for Clinic Recall authored evaluation cases."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_AGENTS = {
    "inbound-assistant",
    "recall-agent",
    "sms-text-adapter",
    "voice-orchestrator",
}
ALLOWED_CHANNELS = {"inbound_voice", "mixed", "outbound_voice", "sms"}
ALLOWED_EXPECTED_ROUTES = {
    "availability_offer",
    "booking_confirmed",
    "booking_intake",
    "callback_task",
    "channel_handoff",
    "clarification",
    "clinical_escalation",
    "complaint_escalation",
    "consent_update",
    "echo_suppressed",
    "governed_playout_sequence",
    "identity_handoff",
    "no_availability_handoff",
    "opt_out",
    "prompt_injection_rejected",
    "safeguarding_escalation",
    "safe_handoff",
    "tool_failure_handoff",
    "urgent_escalation",
}
ALLOWED_REVIEW_STATUSES = {"pending_human_review", "reviewed", "rejected"}
ALLOWED_SOURCES = {
    "ACI-BENCH-inspired",
    "AgentClinic-inspired",
    "MTS-Dialog-inspired",
    "PriMock57-inspired",
    "live-trace-synthetic",
    "synthetic",
}

REQUIRED_CASE_FIELDS = {
    "agent",
    "case_id",
    "channel",
    "expected_patient_reply_style",
    "expected_route",
    "forbidden_actions",
    "persona",
    "required_actions",
    "review_status",
    "source",
    "tool_state",
    "trusted_context",
    "turns",
}
SMOKE_REQUIRED_FIELDS = {"expected", "ground_truth", "input", "query"}

PHONE_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+?\d[\d\s().-]{7,}\d)(?![A-Za-z0-9])"
)
DOB_RE = re.compile(
    r"\b(?:dob|date of birth|born)\b\s*(?:is|:|-)?\s*"
    r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
PHI_MARKER_RE = re.compile(
    r"\b(?:nhs\s*(?:number|no\.?|#)|medical\s*record\s*(?:number|no\.?|#)|mrn)\b\s*[:#-]?\s*\w+",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation issue with enough location context for a CLI report."""

    location: str
    message: str

    def format(self) -> str:
        return f"{self.location}: {self.message}"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: JSONL row must be an object")
        rows.append(row)
    return rows


def validate_case_rows(rows: Iterable[Mapping[str, Any]], *, source_name: str = "<rows>") -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen_case_ids: set[str] = set()

    for index, row in enumerate(rows, start=1):
        location = f"{source_name}:{index}"
        missing = sorted(REQUIRED_CASE_FIELDS - set(row))
        if missing:
            issues.append(ValidationIssue(location, f"missing required fields: {', '.join(missing)}"))

        case_id = str(row.get("case_id", "")).strip()
        if not case_id:
            issues.append(ValidationIssue(location, "case_id must not be blank"))
        elif case_id in seen_case_ids:
            issues.append(ValidationIssue(location, f"duplicate case_id: {case_id}"))
        else:
            seen_case_ids.add(case_id)

        _check_allowed(row, "agent", ALLOWED_AGENTS, location, issues)
        _check_allowed(row, "channel", ALLOWED_CHANNELS, location, issues)
        _check_allowed(row, "expected_route", ALLOWED_EXPECTED_ROUTES, location, issues)
        _check_allowed(row, "review_status", ALLOWED_REVIEW_STATUSES, location, issues)
        _check_allowed(row, "source", ALLOWED_SOURCES, location, issues)

        turns = row.get("turns")
        if not isinstance(turns, list) or not turns:
            issues.append(ValidationIssue(location, "turns must be a non-empty list"))

        for value_path, value in _walk_strings(row):
            _check_phi_like_text(value, f"{location}.{value_path}", issues)

    return issues


def validate_case_file(path: Path) -> list[ValidationIssue]:
    return validate_case_rows(read_jsonl(path), source_name=str(path))


def validate_smoke_rows(rows: Iterable[Mapping[str, Any]], *, source_name: str = "<rows>") -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, row in enumerate(rows, start=1):
        location = f"{source_name}:{index}"
        for field in sorted(SMOKE_REQUIRED_FIELDS):
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                issues.append(ValidationIssue(location, f"smoke row requires non-empty {field}"))
    return issues


def validate_smoke_file(path: Path) -> list[ValidationIssue]:
    return validate_smoke_rows(read_jsonl(path), source_name=str(path))


def _check_allowed(
    row: Mapping[str, Any],
    field: str,
    allowed_values: set[str],
    location: str,
    issues: list[ValidationIssue],
) -> None:
    value = row.get(field)
    if value not in allowed_values:
        issues.append(
            ValidationIssue(
                location,
                f"{field} must be one of {', '.join(sorted(allowed_values))}; got {value!r}",
            )
        )


def _walk_strings(value: Any, path: str = "$.") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path.rstrip("."), value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_strings(child, f"{path}{key}.")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{path}[{index}].")


def _check_phi_like_text(text: str, location: str, issues: list[ValidationIssue]) -> None:
    checks = [
        (PHONE_NUMBER_RE, "raw phone-number-looking string"),
        (DOB_RE, "DOB-looking string"),
        (PHI_MARKER_RE, "raw clinical identifier marker"),
        (EMAIL_RE, "raw email-looking string"),
    ]
    for pattern, message in checks:
        if pattern.search(text):
            issues.append(ValidationIssue(location, f"contains {message}"))


def _main() -> int:
    parser = argparse.ArgumentParser(description="Validate Clinic Recall evaluation JSONL assets.")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Validate Foundry smoke rows instead of canonical authored cases.",
    )
    args = parser.parse_args()

    issues: list[ValidationIssue] = []
    for path in args.paths:
        issues.extend(validate_smoke_file(path) if args.smoke else validate_case_file(path))

    if issues:
        for issue in issues:
            print(issue.format())
        return 1
    print(f"validated {len(args.paths)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())