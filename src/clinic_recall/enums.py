"""Enumerations for the Clinic Recall data model (PRD section 9).

These mirror the indicative data model in the PRD. They are defined as
``str``-based enums so they serialise cleanly to JSON, map to native
PostgreSQL enums, and compare equal to their wire values.

All values are deterministic, closed sets. The model never invents a status,
reason, or intent outside these enumerations.
"""

from __future__ import annotations

from enum import StrEnum


class AppointmentStatus(StrEnum):
    """Lifecycle status of an appointment as normalised into our schema."""

    SCHEDULED = "scheduled"
    MISSED = "missed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class ReasonCode(StrEnum):
    """Outreach reason codes set by deterministic detection (FR-05)."""

    MISSED = "missed"
    CANCELLED = "cancelled"
    OVERDUE_FOLLOWUP = "overdue_followup"
    DUE_RECURRING = "due_recurring"
    UPCOMING_REMINDER = "upcoming_reminder"


class Channel(StrEnum):
    """Outreach channels."""

    SMS = "sms"
    EMAIL = "email"
    CALL = "call"


class IdentityTier(StrEnum):
    """Server-owned disclosure and action tier for one bound session."""

    T0 = "t0"
    T1 = "t1"
    T2 = "t2"


class IdentityEvidenceState(StrEnum):
    """Lifecycle of one identity-evidence session."""

    ACTIVE = "active"
    REVOKED = "revoked"


class IdentityFactorResult(StrEnum):
    """Minimized result of one transient factor comparison."""

    MATCH = "match"
    NO_MATCH = "no_match"
    UNCERTAIN = "uncertain"


class IdentityEvidenceReason(StrEnum):
    """Sanitized identity-evidence outcomes safe for persistence."""

    ROUTE_ONLY = "route_only"
    ROUTE_UNVERIFIED = "route_unverified"
    MISSING_POLICY = "missing_policy"
    MATCHED = "matched"
    MISMATCH = "mismatch"
    UNCERTAIN = "uncertain"
    REPLAYED = "replayed"
    EXPIRED = "expired"
    REVOKED = "revoked"
    STALE_POLICY = "stale_policy"
    BINDING_MISMATCH = "binding_mismatch"
    RIGHTS_FROZEN = "rights_frozen"
    INVALID_FACTOR = "invalid_factor"
    RETRY_EXHAUSTED = "retry_exhausted"
    INSUFFICIENT_TIER = "insufficient_tier"
    AUTHORIZED = "authorized"


class ExternalEffectType(StrEnum):
    """Closed set of externally dispatched effect kinds."""

    SMS = "sms"
    CALL = "call"
    RECORDING = "recording"
    RIGHTS = "rights"
    CLINIKO_BOOKING = "cliniko_booking"
    HANDOFF_NOTIFICATION = "handoff_notification"


class ExternalEffectState(StrEnum):
    """Durable lifecycle for an external provider effect."""

    PENDING = "pending"
    LEASED = "leased"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    RECONCILE_REQUIRED = "reconcile_required"
    DEAD_LETTER = "dead_letter"
    CANCELED = "canceled"


class HandoffSeverity(StrEnum):
    """Deterministic urgency assigned to human-owned work."""

    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"


class HandoffDeliveryState(StrEnum):
    """Provider evidence kept independent from human acknowledgement."""

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    DEFINITIVE_FAILURE = "definitive_failure"
    RECONCILE_REQUIRED = "reconcile_required"


class HandoffAlternateState(StrEnum):
    """Durable alternate notification request state."""

    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"


class HandoffDestinationRole(StrEnum):
    """Server-owned operational destination roles."""

    CLINIC_OPERATIONS = "clinic_operations"
    CLINIC_ON_CALL = "clinic_on_call"


class HandoffRouteKind(StrEnum):
    """Closed notification routes without destination addresses."""

    OPERATIONAL_EMAIL = "operational_email"
    MONITOR_ACTION_GROUP = "monitor_action_group"


class ProviderCallbackKind(StrEnum):
    """Provider callback contracts normalized at the HTTP boundary."""

    SMS = "sms"
    VOICE = "voice"
    RECORDING = "recording"
    AMD = "amd"
    EMAIL = "email"


class ProviderCallbackState(StrEnum):
    """Finite processing lifecycle for a callback receipt."""

    PENDING = "pending"
    PROCESSING = "processing"
    APPLIED = "applied"
    RECONCILE_REQUIRED = "reconcile_required"


class ProviderCallbackReason(StrEnum):
    """Allowlisted callback application outcomes."""

    APPLIED = "applied"
    STALE_NOOP = "stale_noop"
    EFFECT_BUSY = "effect_busy"
    CONFLICTING_TERMINAL = "conflicting_terminal"
    PROVIDER_IDENTITY_CONFLICT = "provider_identity_conflict"
    EFFECT_STATE_CONFLICT = "effect_state_conflict"
    UNSUPPORTED_EFFECT = "unsupported_effect"
    MISSING_EVIDENCE = "missing_evidence"


class CampaignType(StrEnum):
    """Type of outreach campaign."""

    RECOVERY = "recovery"
    FEEDBACK = "feedback"
    REMINDER = "reminder"


class CampaignStatus(StrEnum):
    """Campaign lifecycle status."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class PilotProgrammeState(StrEnum):
    """Closed lifecycle for one production-pilot release programme."""

    DRAFT = "draft"
    DARK = "dark"
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class PromptProposalStatus(StrEnum):
    """Review status for governed Recall Agent prompt proposals."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    ACCEPTED = "accepted"


class ClinicPhoneProvider(StrEnum):
    """Telephony provider that owns a clinic phone number."""

    ACS = "acs"
    TWILIO = "twilio"


class ClinicPhonePurpose(StrEnum):
    """Allowed direction/purpose for a clinic phone number."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"
    BOTH = "both"


class ClinicPhoneStatus(StrEnum):
    """Operational status for a clinic phone number route."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class InboundCallStatus(StrEnum):
    """Lifecycle status for an inbound clinic call."""

    STARTED = "started"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"


class InboundMessageStatus(StrEnum):
    """Lifecycle status for a minimized inbound clinic message."""

    RECEIVED = "received"
    ROUTED = "routed"
    FAILED = "failed"


class CallRecordingStatus(StrEnum):
    """Provider recording lifecycle for one all-call ledger row."""

    NONE = "none"
    # Retained for historical Phase-A rows; PR-09 uses explicit start/stop states.
    PENDING = "pending"
    START_PENDING = "start_pending"
    STARTING = "starting"
    IN_PROGRESS = "in_progress"
    STOP_PENDING = "stop_pending"
    STOPPING = "stopping"
    COMPLETED = "completed"
    STORED = "stored"
    ABSENT = "absent"
    FAILED = "failed"
    RECONCILE_REQUIRED = "reconcile_required"


class RecordingConsentState(StrEnum):
    """Deterministic per-call recording-consent lifecycle."""

    NOT_ASKED = "not_asked"
    ASKED = "asked"
    GRANTED = "granted"
    DECLINED = "declined"
    AMBIGUOUS = "ambiguous"
    WITHDRAWN = "withdrawn"


class RecordingConsentSource(StrEnum):
    """Closed evidence sources for a per-call consent decision."""

    SPEECH = "speech"
    DTMF = "dtmf"
    TIMEOUT = "timeout"
    POLICY = "policy"


class RecordingDeletionState(StrEnum):
    """Local marker reserved for the durable PR-10 deletion workflow."""

    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class RightsRequestKind(StrEnum):
    """Durable privacy workflow kinds."""

    ERASURE = "erasure"
    RETENTION = "retention"


class RightsRequestState(StrEnum):
    """Monotonic lifecycle of one rights aggregate."""

    REQUESTED = "requested"
    FROZEN = "frozen"
    DELETING = "deleting"
    VERIFYING = "verifying"
    COMPLETED = "completed"


class RightsTargetSystem(StrEnum):
    """Closed ownership boundaries for rights targets."""

    LOCAL = "local"
    TWILIO = "twilio"
    AZURE_BLOB = "azure_blob"
    PROCESSOR = "processor"
    CONTROLLER = "controller"


class RightsTargetResource(StrEnum):
    """Resources that can participate in a rights workflow."""

    MESSAGE = "message"
    CALL = "call"
    RECORDING = "recording"
    INTERACTION_CONTENT = "interaction_content"
    TRANSCRIPTION_COLLECTION = "transcription_collection"
    BLOB_COLLECTION = "blob_collection"
    PATIENT_GRAPH = "patient_graph"
    CLINIKO = "cliniko"
    POSTGRES_BACKUP = "postgres_backup"
    APPLICATION_LOG = "application_log"
    MONITOR_LOG = "monitor_log"
    SUPPORT_PATH = "support_path"
    VOICE_LIVE = "voice_live"
    REDIS_SESSION = "redis_session"
    INCIDENT_RECORD = "incident_record"


class RightsTargetAction(StrEnum):
    """Allowed operations for a rights target."""

    STOP = "stop"
    DELETE = "delete"
    PURGE = "purge"
    MINIMIZE = "minimize"
    PROCEDURE = "procedure"
    VERIFY = "verify"


class RightsTargetOwnerType(StrEnum):
    """Rows that retain a target locator until verification."""

    EXTERNAL_EFFECT = "external_effect"
    CALL_RECORD = "call_record"
    INBOUND_CALL = "inbound_call"
    INBOUND_MESSAGE = "inbound_message"
    INTERACTION = "interaction"
    RIGHTS_REQUEST = "rights_request"
    INCIDENT_REPORT = "incident_report"


class RightsTargetState(StrEnum):
    """Durable lifecycle of one deletion or procedure target."""

    REQUESTED = "requested"
    DISPATCHING = "dispatching"
    RECONCILE_REQUIRED = "reconcile_required"
    VERIFIED = "verified"
    RESIDUAL = "residual"


class RightsResidualCategory(StrEnum):
    """Documented residual classes requiring explicit policy approval."""

    PROVIDER_BACKUP_WINDOW = "provider_backup_window"
    PROVIDER_METADATA_WINDOW = "provider_metadata_window"
    BLOB_SOFT_DELETE_WINDOW = "blob_soft_delete_window"
    LEGAL_OR_IMMUTABILITY_HOLD = "legal_or_immutability_hold"
    PROCESSOR_PROCEDURE = "processor_procedure"
    LEGACY_ARCHIVE_PROCEDURE = "legacy_archive_procedure"
    CLINIKO_CONTROLLER_PROCEDURE = "cliniko_controller_procedure"
    POSTGRES_BACKUP_WINDOW = "postgres_backup_window"
    APPLICATION_LOG_WINDOW = "application_log_window"
    MONITOR_LOG_WINDOW = "monitor_log_window"
    SUPPORT_PROCEDURE = "support_procedure"
    VOICE_LIVE_PROCESSOR_PROCEDURE = "voice_live_processor_procedure"
    REDIS_SESSION_PROCEDURE = "redis_session_procedure"
    CLINICAL_GOVERNANCE_RECORD = "clinical_governance_record"


class InboundStaffTaskKind(StrEnum):
    """Human task types created by the inbound assistant."""

    CALLBACK = "callback"
    ESCALATION = "escalation"
    BOOKING_REQUEST = "booking_request"
    IDENTITY_UNCLEAR = "identity_unclear"


class InboundStaffTaskStatus(StrEnum):
    """Lifecycle status for inbound human tasks."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class OutreachState(StrEnum):
    """State of a single outreach job (one patient + one appointment attempt)."""

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    REPLIED = "replied"
    NO_REPLY = "no_reply"
    ESCALATED = "escalated"
    COMPLETED = "completed"


class InteractionDirection(StrEnum):
    """Direction of a logged interaction."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class InteractionIntent(StrEnum):
    """Classified intent of an inbound patient response."""

    FEEDBACK = "feedback"
    REBOOK = "rebook"
    DECLINE = "decline"
    OPT_OUT = "opt_out"
    QUESTION = "question"
    UNCLEAR = "unclear"
    CLINICAL = "clinical"
    URGENT = "urgent"


class InteractionOutcome(StrEnum):
    """Outcome of handling an interaction."""

    IGNORED = "ignored"
    ROUTED_TO_STAFF = "routed_to_staff"
    AUTO_HANDLED = "auto_handled"


class BookingActionType(StrEnum):
    """Type of booking action."""

    BOOK = "book"
    RESCHEDULE = "reschedule"


class BookingActionStatus(StrEnum):
    """Status of a booking action / approval-queue entry."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"


class BookingWriteBackState(StrEnum):
    """Provider write lifecycle kept independent from local workflow status."""

    NOT_ATTEMPTED = "not_attempted"
    PENDING = "pending"
    DISPATCHING = "dispatching"
    VERIFIED = "verified"
    REJECTED = "rejected"
    RECONCILE_REQUIRED = "reconcile_required"
    CONFLICT = "conflict"


class EscalationReason(StrEnum):
    """Why a candidate / interaction was escalated to a human."""

    CLINICAL = "clinical"
    URGENT = "urgent"
    AMBIGUOUS = "ambiguous"
    FAILED_CONTACT = "failed_contact"
    COMPLAINT = "complaint"


class EscalationPriority(StrEnum):
    """Priority of a human-review escalation."""

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class EscalationStatus(StrEnum):
    """Status of a human-review escalation."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


ACTIVE_ESCALATION_STATUSES = frozenset(
    {EscalationStatus.OPEN, EscalationStatus.ACKNOWLEDGED}
)
ACTIVE_INBOUND_STAFF_TASK_STATUSES = frozenset(
    {InboundStaffTaskStatus.OPEN, InboundStaffTaskStatus.ACKNOWLEDGED}
)


class IncidentSource(StrEnum):
    """Who filed an anonymous incident report (never identified further)."""

    STAFF = "staff"
    PATIENT = "patient"


class IncidentCategory(StrEnum):
    """Clinical-governance incident categories (LFPSE/Datix-style)."""

    PATIENT_SAFETY = "patient_safety"
    NEAR_MISS = "near_miss"
    COMMUNICATION_FAILURE = "communication_failure"
    WRONG_PATIENT_CONTACTED = "wrong_patient_contacted"
    DATA_PRIVACY_CONCERN = "data_privacy_concern"
    AGENT_BEHAVIOUR = "agent_behaviour"
    OTHER = "other"


class IncidentSeverity(StrEnum):
    """NPSA-style harm grading for incident reports."""

    NO_HARM = "no_harm"
    LOW = "low"
    MODERATE = "moderate"
    SEVERE = "severe"


class IncidentStatus(StrEnum):
    """Governance review workflow for incident reports."""

    NEW = "new"
    UNDER_REVIEW = "under_review"
    ACTIONED = "actioned"
    CLOSED = "closed"


class AuditAction(StrEnum):
    """Actions recorded in the immutable audit log (SR-05).

    Phase 1 only emits the deterministic data-plane actions
    (``SYNC_UPSERT``, ``DETECT_CANDIDATE``, ``ENQUEUE_OUTREACH``,
    ``SKIP_CANDIDATE``). The send / book / opt-out actions are declared here so
    later phases write to the same closed vocabulary.
    """

    # Phase 1 (deterministic data plane)
    SYNC_UPSERT = "sync_upsert"
    DETECT_CANDIDATE = "detect_candidate"
    ENQUEUE_OUTREACH = "enqueue_outreach"
    SKIP_CANDIDATE = "skip_candidate"
    # Later phases (declared for a stable vocabulary)
    SEND_SMS = "send_sms"
    SEND_EMAIL = "send_email"
    PLACE_CALL = "place_call"
    BOOK_APPOINTMENT = "book_appointment"
    RECORD_FEEDBACK = "record_feedback"
    RECORDING_CONSENT = "recording_consent"
    RETENTION_PURGE = "retention_purge"
    ERASE_PATIENT = "erase_patient"
    OPT_OUT_PATIENT = "opt_out_patient"
    ESCALATE = "escalate"
    ACKNOWLEDGE = "acknowledge"
    RESOLVE = "resolve"
    APPROVE = "approve"
    REJECT = "reject"
    INCIDENT_REPORT = "incident_report"
    INCIDENT_STATUS_CHANGE = "incident_status_change"
    # PR-08 controlled CSV import (metadata-only payload hashes)
    CSV_IMPORT_PREVIEW = "csv_import_preview"
    CSV_IMPORT_APPROVE = "csv_import_approve"
    CSV_IMPORT_MATCH = "csv_import_match"


class SkipReason(StrEnum):
    """Deterministic reason a candidate was held back by eligibility (FR-06)."""

    OPTED_OUT = "opted_out"
    NO_CONSENT = "no_consent"
    NOT_CONTACTABLE = "not_contactable"
    FREQUENCY_CAP = "frequency_cap"
    DAILY_CAP = "daily_cap"
    OUTSIDE_CONTACT_HOURS = "outside_contact_hours"
    QUIET_HOURS = "quiet_hours"


class SourceSystem(StrEnum):
    """Closed vocabulary of sync/import source systems (PR-08)."""

    CSV = "csv"
    CLINIKO = "cliniko"


class ImportBatchState(StrEnum):
    """Lifecycle of one controlled CSV import batch (PR-08).

    A retryable approval failure leaves the batch in ``preview_valid`` so the
    same bytes can be re-approved before expiry; ``expired`` and ``superseded``
    are bounded terminal preview states; only ``completed`` has imported rows.
    """

    PREVIEW_VALID = "preview_valid"
    PREVIEW_INVALID = "preview_invalid"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    COMPLETED = "completed"


class SourceLinkState(StrEnum):
    """Lifecycle of a provider-qualified patient source link (PR-08)."""

    ACTIVE = "active"
    FROZEN = "frozen"


class ImportMatchReviewState(StrEnum):
    """Deterministic provider source-match review workflow (PR-08)."""

    NOT_RUN = "not_run"
    PENDING = "pending"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    LINKED = "linked"
    DISMISSED = "dismissed"
    FAILED = "failed"


class MatchStrategy(StrEnum):
    """Reviewed deterministic matching strategies (PR-08). No fuzzy matching."""

    EXACT_SOURCE_REF = "exact_source_ref"
    OPERATOR_RESOLVED = "operator_resolved"


class CsvValidationReason(StrEnum):
    """Closed, safe CSV validation reason codes (PR-08).

    These are the only reason tokens allowed in preview error responses,
    aggregate reason counts, and validation-summary hashes. Raw cell values
    never accompany them.
    """

    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    TOO_MANY_ROWS = "too_many_rows"
    INVALID_ENCODING = "invalid_encoding"
    CONTROL_CHARACTER = "control_character"
    NO_HEADER = "no_header"
    MISSING_REQUIRED_COLUMN = "missing_required_column"
    DUPLICATE_COLUMN = "duplicate_column"
    UNKNOWN_COLUMN = "unknown_column"
    MALFORMED_CSV = "malformed_csv"
    ROW_FIELD_OVERFLOW = "row_field_overflow"
    ROW_MISSING_FIELD = "row_missing_field"
    FIELD_TOO_LONG = "field_too_long"
    FORMULA_PREFIX = "formula_prefix"
    INVALID_SOURCE_REF = "invalid_source_ref"
    MISSING_VALUE = "missing_value"
    INVALID_VALUE = "invalid_value"
    INVALID_TIMESTAMP = "invalid_timestamp"
    NAIVE_TIMESTAMP = "naive_timestamp"
    INVALID_BOOLEAN = "invalid_boolean"
    INVALID_DECIMAL = "invalid_decimal"
    INVALID_PHONE = "invalid_phone"
    INVALID_EMAIL = "invalid_email"
    DUPLICATE_APPOINTMENT_REF = "duplicate_appointment_ref"
    CONFLICTING_PATIENT_FACT = "conflicting_patient_fact"
    TOO_MANY_ERRORS = "too_many_errors"
