"""Clinic Recall Phase 4 staff surfaces API."""

from __future__ import annotations

import base64
import difflib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from src.clinic_recall.candidate_queue import generate_candidate_queue
from src.clinic_recall.config import (
    csv_import_enabled,
    csv_matching_enabled,
    get_csv_import_policy,
    get_rights_policy,
    get_rights_subject_keyring,
)
from src.clinic_recall.db import (
    clinic_scope,
    get_privacy_sessionmaker,
    get_sessionmaker,
    tenant_select,
)
from src.clinic_recall.durable.config import (
    durable_cliniko_write_enabled,
    handoff_delivery_callback_enabled,
)
from src.clinic_recall.durable.handoff_delivery import (
    HandoffDeliveryCorrelationError,
    HandoffDeliveryValidationError,
    receive_acs_email_events,
)
from src.clinic_recall.enums import (
    AuditAction,
    BookingActionStatus,
    CampaignStatus,
    Channel,
    ClinicPhoneStatus,
    EscalationStatus,
    ImportBatchState,
    InboundCallStatus,
    InboundStaffTaskKind,
    InboundStaffTaskStatus,
    OutreachState,
    PromptProposalStatus,
)
from src.clinic_recall.handoffs import (
    acknowledge_handoff_owner,
    mark_handoff_resolved,
)
from src.clinic_recall.identity_runtime import runtime_identity_service
from src.clinic_recall.incidents import (
    create_incident,
    list_incidents,
    update_incident_status,
)
from src.clinic_recall.messaging.audit import audit_action
from src.clinic_recall.models import (
    BookingAction,
    CallRecord,
    Campaign,
    Clinic,
    ClinicIdentityMapping,
    ClinicPhoneNumber,
    Escalation,
    HandoffReceipt,
    ImportBatch,
    InboundCall,
    InboundMessage,
    InboundStaffTask,
    Interaction,
    OutreachJob,
    Patient,
    PilotParticipant,
    PilotProgramme,
    PromptProposal,
)
from src.clinic_recall.outbox import list_interaction_timeline, list_outbox_items
from src.clinic_recall.pilot_controls import (
    JobPilotGate,
    PilotControlError,
    close_programme,
    create_programme,
    enroll_participant,
    job_gate_for_snapshot,
    mark_programme_dark,
    operational_switch_snapshot_from_environment,
    patient_gate_for_snapshot,
    pause_programme,
    release_cumulative_limit,
)
from src.clinic_recall.rights import (
    get_rights_operations_status,
    get_rights_request_status,
    request_patient_erasure,
)
from src.clinic_recall.roi import get_roi_metrics, roi_metrics_csv
from src.clinic_recall.staff_queue import (
    QueueDecision,
    acknowledge_queue_item,
    list_staff_queue,
    resolve_queue_item,
)
from src.clinic_recall.voice_worker import run_voice_cadence

router = APIRouter(tags=["Clinic Recall"])
logger = logging.getLogger(__name__)

_STAFF_ROLES = {"staff", "clinic_staff", "operator", "dpo", "privacy"}
_OPERATOR_ROLES = {"operator"}
_RIGHTS_ROLES = {"operator", "dpo", "privacy"}
_EVENT_GRID_HANDOFF_ROLE = "AzureEventGridSecureWebhookSubscriber"
_EVENT_GRID_SUBSCRIPTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_EVENT_GRID_TOPIC = re.compile(
    r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[A-Za-z0-9._()/-]+/"
    r"providers/Microsoft\.Communication/communicationServices/[A-Za-z0-9._-]+\Z",
    re.IGNORECASE,
)
_ONBOARDING_STEPS = (
    "connect_data",
    "confirm_number",
    "choose_script",
    "set_rules",
    "first_campaign",
)


async def _authenticate_handoff_delivery_request(request: Request) -> None:
    """Validate the Event Grid Microsoft Entra bearer token before body access."""
    from apps.artagent.backend.src.utils.auth import validate_entraid_token

    claims = await validate_entraid_token(request)
    roles = claims.get("roles")
    if not isinstance(roles, list) or _EVENT_GRID_HANDOFF_ROLE not in roles:
        raise HTTPException(status_code=403, detail="Event Grid role is required")


def _handoff_event_grid_authority(request: Request) -> tuple[str, str]:
    subscription_name = os.getenv(
        "CLINIC_RECALL_HANDOFF_EVENTGRID_SUBSCRIPTION_NAME",
        "",
    ).strip()
    topic_id = os.getenv("CLINIC_RECALL_HANDOFF_EVENTGRID_TOPIC_ID", "").strip()
    if (
        _EVENT_GRID_SUBSCRIPTION.fullmatch(subscription_name) is None
        or _EVENT_GRID_TOPIC.fullmatch(topic_id) is None
    ):
        raise HTTPException(status_code=503, detail="Event Grid authority unavailable")
    if request.headers.get("aeg-subscription-name", "") != subscription_name:
        raise HTTPException(status_code=403, detail="Event Grid subscription mismatch")
    return subscription_name, topic_id


@dataclass(frozen=True)
class StaffContext:
    """Trusted staff context derived server-side, never from request body clinic ids."""

    clinic_id: str
    actor: str
    roles: frozenset[str]


class QueueResponse(BaseModel):
    items: list[Any]


class ResolveQueueRequest(BaseModel):
    decision: QueueDecision
    reason: str | None = Field(default=None, max_length=250)
    clinic_id: str | None = None


class CampaignSettingsRequest(BaseModel):
    clinic_id: str | None = None
    daily_caps: int | None = Field(default=None, ge=1, le=10_000)
    branding: dict[str, Any] | None = None
    contact_hours: dict[str, Any] | None = None


class CampaignLaunchRequest(BaseModel):
    clinic_id: str | None = None
    now: datetime | None = None
    channel: Channel = Channel.SMS


class VoiceFallbackRequest(BaseModel):
    now: datetime | None = None


class PilotProgrammeCreateRequest(BaseModel):
    programme_id: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=32)
    release_identity: str = Field(min_length=1, max_length=200)


class PilotParticipantEnrollRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=128)


class PilotReleaseRequest(BaseModel):
    cumulative_limit: int
    evidence_hash: str = Field(min_length=64, max_length=64)


class PilotPauseRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=64)


class PilotDarkRequest(BaseModel):
    evidence_hash: str = Field(min_length=64, max_length=64)


class ClinicSignupRequest(BaseModel):
    clinic_name: str = Field(min_length=2, max_length=120)
    contact_email: str = Field(min_length=3, max_length=254)
    # Paid-social attribution (utm_source/medium/campaign/content/term/id),
    # captured by /go/ landing variants. Keys and values are length-capped
    # non-identifying campaign labels.
    attribution: dict[str, str] | None = None


class ClinicSignupResponse(BaseModel):
    clinic_id: str
    status: str
    onboarding_next: str


class OnboardingRequest(BaseModel):
    completed_step: str | None = None
    onboarding_step: str | None = None
    onboarding_steps: dict[str, bool] | None = None
    outreach_enabled: bool | None = None


class OnboardingResponse(BaseModel):
    status: str
    onboarding_required: bool
    onboarding_step: str
    onboarding_steps: dict[str, bool]
    outreach_enabled: bool


class MonitorResponse(BaseModel):
    open_queue_count: int
    queued_outbox_count: int
    active_campaigns: int
    recent_interactions_count: int
    latest_escalation_at: datetime | None = None
    voice_fallback_summary: dict[str, Any]


class PhoneNumberRecord(BaseModel):
    id: str
    provider: str
    phone_number: str
    purpose: str
    status: str
    webhook_url: str | None = None
    test_status: str | None = None


class PhoneNumbersResponse(BaseModel):
    items: list[PhoneNumberRecord]


class InboundCallRecord(BaseModel):
    id: str
    provider: str
    provider_call_id: str
    called_number: str
    caller_number_redacted: str
    status: str
    outcome: str | None = None
    created_at: datetime


class InboundCallsResponse(BaseModel):
    items: list[InboundCallRecord]


class CallLedgerStatusRecord(BaseModel):
    id: str
    provider: str
    direction: str
    scenario: str | None = None
    patient_linked: bool
    provider_call_bound: bool
    consent_state: str
    consent_decision_source: str | None = None
    consent_version: str | None = None
    consent_asked_at: datetime | None = None
    consent_decided_at: datetime | None = None
    recording_status: str
    recording_identity_bound: bool
    recording_requested_at: datetime | None = None
    recording_started_at: datetime | None = None
    recording_stop_requested_at: datetime | None = None
    recording_stopped_at: datetime | None = None
    deletion_state: str
    started_at: datetime | None = None
    ended_at: datetime | None = None


class CallLedgerStatusResponse(BaseModel):
    items: list[CallLedgerStatusRecord]


class InboundTaskRecord(BaseModel):
    id: str
    inbound_call_id: str | None = None
    inbound_message_id: str | None = None
    source: str
    kind: str
    status: str
    priority: str
    reason: str | None = None
    summary: str | None = None
    created_at: datetime
    severity: str | None = None
    delivery_state: str | None = None
    queued_at: datetime | None = None
    due_at: datetime | None = None
    overdue: bool = False
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    alternate_requested: bool = False
    owner_resolved: bool = False


class InboundTasksResponse(BaseModel):
    items: list[InboundTaskRecord]


class InboundMessageRecord(BaseModel):
    id: str
    provider: str
    from_number_redacted: str
    intent: str | None = None
    status: str
    summary: str | None = None
    created_at: datetime


class InboundMessagesResponse(BaseModel):
    items: list[InboundMessageRecord]


class InboundTaskResolveRequest(BaseModel):
    status: Literal["resolved"] = "resolved"
    reason: str | None = Field(default=None, max_length=250)


class InboundConfigRequest(BaseModel):
    greeting: str | None = Field(default=None, max_length=500)
    callback_sla_hours: int | None = Field(default=None, ge=1, le=168)
    escalation_destination: str | None = Field(default=None, max_length=250)
    recording_enabled: bool | None = None


class InboundConfigResponse(BaseModel):
    greeting: str
    callback_sla_hours: int
    escalation_destination: str | None = None


class InboundMetricsResponse(BaseModel):
    calls_total: int
    calls_completed: int
    texts_total: int = 0
    texts_routed: int = 0
    open_tasks: int
    callbacks_open: int
    escalations_open: int
    booking_requests_open: int
    text_callbacks_open: int = 0
    text_escalations_open: int = 0
    text_booking_requests_open: int = 0
    text_identity_unclear_open: int = 0


class PromptProposalRequest(BaseModel):
    proposed_prompt: str = Field(min_length=20, max_length=20_000)


class PromptProposalResponse(BaseModel):
    prompt_path: str
    diff: str
    gate_required: bool = True


class PromptProposalRecord(BaseModel):
    id: str
    actor: str
    status: str
    proposed_prompt: str
    diff: str
    gate_required: bool = True
    created_at: datetime
    updated_at: datetime


class PromptProposalListResponse(BaseModel):
    proposals: list[PromptProposalRecord]


class ScriptTemplatesRequest(BaseModel):
    templates: dict[str, str] = Field(default_factory=dict)


class ScriptTemplatesResponse(BaseModel):
    templates: dict[str, str]


class VoicePersonaRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    tone: str | None = Field(default=None, max_length=120)
    voice_name: str | None = Field(default=None, max_length=120)


class VoicePersonaResponse(BaseModel):
    display_name: str
    tone: str
    voice_name: str | None = None


class ErasePatientRequest(BaseModel):
    clinic_id: str | None = None
    confirm_token: str = Field(min_length=1, max_length=250)


class RightsRequestResponse(BaseModel):
    request_id: str
    state: str
    created: bool
    target_count: int
    due_at: datetime


class RightsStatusResponse(BaseModel):
    request_id: str
    state: str
    target_count: int
    pending_count: int
    verified_count: int
    residual_count: int
    unapproved_residual_count: int
    overdue_count: int
    requested_at: datetime
    due_at: datetime
    completed_at: datetime | None


class RightsOperationsResponse(BaseModel):
    request_count: int
    incomplete_request_count: int
    target_count: int
    pending_count: int
    reconcile_required_count: int
    handoff_count: int
    unapproved_residual_count: int
    overdue_count: int
    zero_overdue: bool
    ready: bool


class CampaignSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    timezone: str
    daily_caps: int
    branding: dict[str, Any] | None
    contact_hours: dict[str, Any] | None


def _parse_roles(raw: Any) -> frozenset[str]:
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, list | tuple | set):
        values = raw
    else:
        values = []
    return frozenset(str(role).strip().lower() for role in values if str(role).strip())


def _has_staff_access(roles: frozenset[str]) -> bool:
    return bool(roles & _STAFF_ROLES)


def _normalize_identity(value: Any) -> str:
    return str(value or "").strip().lower()


def _require_operator(context: StaffContext) -> None:
    if not (context.roles & _OPERATOR_ROLES):
        raise HTTPException(status_code=403, detail="Clinic Recall operator access required")


def _require_rights_access(context: StaffContext) -> str:
    role = next((role for role in ("dpo", "privacy", "operator") if role in context.roles), None)
    if role is None:
        raise HTTPException(status_code=403, detail="Clinic Recall rights access required")
    return role


def _staff_context_from_env() -> StaffContext | None:
    """Transitional local/staging fallback until all users are persisted mappings."""
    clinic_id = (os.getenv("CLINIC_RECALL_STAFF_CLINIC_ID") or "").strip()
    actor = (os.getenv("CLINIC_RECALL_STAFF_ACTOR") or "").strip()
    roles = _parse_roles(os.getenv("CLINIC_RECALL_STAFF_ROLES") or "")
    if not clinic_id or not actor or not _has_staff_access(roles):
        return None
    return StaffContext(clinic_id=clinic_id, actor=actor, roles=roles)


def _easy_auth_principal(request: Request) -> dict[str, Any] | None:
    encoded = request.headers.get("x-ms-client-principal")
    if not encoded:
        return None
    try:
        from apps.artagent.backend.src.utils.auth import get_easyauth_identity

        return get_easyauth_identity(request)
    except ImportError:
        pass
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
        return json.loads(decoded)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid EasyAuth header encoding") from exc


_PROVIDER_ALIASES = {
    "azureactivedirectory": "aad",
    "azuread": "aad",
    "microsoft": "aad",
}


def _principal_provider(principal: dict[str, Any]) -> str:
    """Normalized identity provider from the EasyAuth principal (fail to 'aad' never silently cross-links)."""
    raw = str(
        principal.get("auth_typ")
        or principal.get("identityProvider")
        or principal.get("provider")
        or "aad"
    ).strip().lower()
    return _PROVIDER_ALIASES.get(raw, raw)


def _principal_identifiers(principal: dict[str, Any]) -> list[str]:
    identifiers: list[str] = []
    for key in ("userDetails", "userId", "name"):
        value = str(principal.get(key) or "").strip()
        if value:
            identifiers.append(value)
    for claim in principal.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        claim_type = str(claim.get("typ") or claim.get("type") or "").lower()
        value = str(claim.get("val") or claim.get("value") or "").strip()
        if not value:
            continue
        if any(token in claim_type for token in ("objectidentifier", "oid", "upn", "email", "name")):
            identifiers.append(value)
    deduped: list[str] = []
    seen: set[str] = set()
    for identifier in identifiers:
        key = identifier.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(identifier)
    return deduped


def _identity_map() -> dict[str, Any]:
    raw = (os.getenv("CLINIC_RECALL_IDENTITY_MAP") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Invalid Clinic Recall identity map") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=500, detail="Invalid Clinic Recall identity map")
    return {str(key).lower(): value for key, value in parsed.items()}


def _staff_context_from_db(principal: dict[str, Any]) -> StaffContext | None:
    identifiers = {
        _normalize_identity(identifier)
        for identifier in _principal_identifiers(principal)
        if _normalize_identity(identifier)
    }
    if not identifiers:
        return None
    provider = _principal_provider(principal)
    with get_sessionmaker()() as session:
        # Provider-scoped subject match only. Email is intentionally NOT an
        # auto-grant path: a Google login with an email matching an Entra
        # mapping must fail closed (account-linking risk for clinical data).
        mappings = list(
            session.execute(
                sa.select(ClinicIdentityMapping).where(
                    ClinicIdentityMapping.provider == provider,
                    ClinicIdentityMapping.subject.in_(identifiers),
                )
            ).scalars()
        )
    if not mappings:
        return None
    if len(mappings) > 1:
        raise HTTPException(status_code=403, detail="Clinic Recall identity mapping is ambiguous")
    mapping = mappings[0]
    if mapping.status.lower() != "active":
        raise HTTPException(status_code=403, detail="Clinic Recall identity is not active")
    roles = _parse_roles(mapping.roles)
    if not _has_staff_access(roles):
        raise HTTPException(status_code=403, detail="Clinic Recall identity is not authorized")
    actor = mapping.email or mapping.subject or "easyauth:mapped"
    return StaffContext(clinic_id=mapping.clinic_id, actor=actor, roles=roles)


def _staff_context_from_easyauth(request: Request) -> StaffContext | None:
    principal = _easy_auth_principal(request)
    if principal is None:
        return None
    context = _staff_context_from_db(principal)
    if context is not None:
        return context
    identity_map = _identity_map()
    for identifier in _principal_identifiers(principal):
        mapped = identity_map.get(_normalize_identity(identifier))
        if not isinstance(mapped, dict):
            continue
        clinic_id = str(mapped.get("clinic_id") or "").strip()
        roles = _parse_roles(mapped.get("roles") or mapped.get("role") or "")
        if not clinic_id or not _has_staff_access(roles):
            continue
        actor = str(mapped.get("actor") or f"easyauth:{identifier}").strip()
        return StaffContext(clinic_id=clinic_id, actor=actor, roles=roles)
    raise HTTPException(status_code=403, detail="Clinic Recall identity is not mapped")


def staff_context(request: Request) -> StaffContext:
    """Resolve clinic/staff from trusted auth context or server fallback."""
    context = _staff_context_from_easyauth(request)
    if context is not None:
        return context
    context = _staff_context_from_env()
    if context is not None:
        return context
    raise HTTPException(status_code=403, detail="Clinic Recall staff access required")


def _env_enabled(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _clinic_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:48] or "clinic"


def _next_onboarding_step(steps: dict[str, bool]) -> str:
    for step in _ONBOARDING_STEPS:
        if not steps.get(step, False):
            return step
    return "complete"


def _initial_onboarding_policy(contact_email: str) -> dict[str, Any]:
    return {
        "signup_status": "pending",
        "contact_email": contact_email.strip(),
        "outreach_enabled": False,
        "onboarding_required": True,
        "onboarding_step": _ONBOARDING_STEPS[0],
        "onboarding_steps": {step: False for step in _ONBOARDING_STEPS},
    }


def _onboarding_state(clinic: Clinic) -> dict[str, Any]:
    policy = dict(clinic.consent_policy or {})
    has_policy = bool(clinic.consent_policy)
    if not has_policy:
        return {
            "status": "active",
            "onboarding_required": False,
            "onboarding_step": "complete",
            "onboarding_steps": {step: True for step in _ONBOARDING_STEPS},
            "outreach_enabled": True,
        }
    required = bool(policy.get("onboarding_required", not has_policy))
    default_complete = not required
    raw_steps = policy.get("onboarding_steps")
    raw_steps = raw_steps if isinstance(raw_steps, dict) else {}
    steps = {step: bool(raw_steps.get(step, default_complete)) for step in _ONBOARDING_STEPS}
    required = not all(steps.values()) if has_policy else False
    outreach_enabled = bool(policy.get("outreach_enabled", not required))
    status = str(policy.get("signup_status") or ("pending" if required else "active"))
    if status == "pending" and not required and outreach_enabled:
        status = "active"
    onboarding_step = str(policy.get("onboarding_step") or _next_onboarding_step(steps))
    if onboarding_step not in {*_ONBOARDING_STEPS, "complete"}:
        onboarding_step = _next_onboarding_step(steps)
    return {
        "status": status,
        "onboarding_required": required,
        "onboarding_step": onboarding_step,
        "onboarding_steps": steps,
        "outreach_enabled": outreach_enabled,
    }


def _write_onboarding_state(clinic: Clinic, state: dict[str, Any]) -> None:
    policy = dict(clinic.consent_policy or {})
    policy.update(
        {
            "signup_status": state["status"],
            "outreach_enabled": state["outreach_enabled"],
            "onboarding_required": state["onboarding_required"],
            "onboarding_step": state["onboarding_step"],
            "onboarding_steps": state["onboarding_steps"],
        }
    )
    clinic.consent_policy = policy


def _principal_mapping_values(principal: dict[str, Any]) -> tuple[str | None, str | None]:
    identifiers = [_normalize_identity(identifier) for identifier in _principal_identifiers(principal)]
    identifiers = [identifier for identifier in identifiers if identifier]
    subject = identifiers[0] if identifiers else None
    email = next((identifier for identifier in identifiers if "@" in identifier), None)
    return subject, email


def _ensure_signup_identity_available(
    principal: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    if principal is None:
        return None, None
    subject, email = _principal_mapping_values(principal)
    return subject, email


def _outreach_is_enabled(clinic: Clinic) -> bool:
    return dict(clinic.consent_policy or {}).get("outreach_enabled") is not False


def _require_outreach_enabled(clinic: Clinic) -> None:
    if not _outreach_is_enabled(clinic):
        raise HTTPException(
            status_code=409,
            detail="Outreach disabled until onboarding and operator approval are complete",
        )


def _recall_prompt_path() -> Path:
    configured = (os.getenv("RECALL_AGENT_PROMPT_PATH") or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[6] / ".agentops" / "prompts" / "recall-agent.prompt.md"


def _prompt_proposal_diff(payload: PromptProposalRequest) -> tuple[Path, str, str]:
    prompt_path = _recall_prompt_path()
    try:
        current = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Recall Agent prompt file is unavailable") from exc
    proposed = payload.proposed_prompt.rstrip() + "\n"
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=str(prompt_path),
            tofile=f"{prompt_path} (proposed)",
        )
    )
    return prompt_path, proposed, diff


def _prompt_proposal_record(proposal: PromptProposal) -> PromptProposalRecord:
    return PromptProposalRecord(
        id=proposal.id,
        actor=proposal.actor,
        status=proposal.status.value,
        proposed_prompt=proposal.proposed_prompt,
        diff=proposal.diff,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
    )


def _branding_dict(clinic: Clinic) -> dict[str, Any]:
    return dict(clinic.branding or {})


def _default_script_templates() -> dict[str, str]:
    return {
        "missed": "We noticed you missed your appointment. Would you like to find another time?",
        "overdue": "You may be due for a follow-up. Would you like us to check available times?",
        "feedback": "Thanks for visiting. Would you like to share any feedback with the clinic team?",
    }


def _validate_script_templates(templates: dict[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    if len(templates) > 12:
        raise HTTPException(status_code=422, detail="At most 12 script templates are allowed")
    for raw_key, raw_value in templates.items():
        key = str(raw_key).strip().lower()
        value = str(raw_value).strip()
        if not re.fullmatch(r"[a-z0-9_-]{2,40}", key):
            raise HTTPException(status_code=422, detail=f"Invalid script template key: {raw_key}")
        if not 5 <= len(value) <= 1000:
            raise HTTPException(status_code=422, detail=f"Invalid script template body for {key}")
        clean[key] = value
    return clean


def _voice_persona_from_branding(branding: dict[str, Any], clinic_name: str) -> VoicePersonaResponse:
    raw = branding.get("voice_persona")
    persona = raw if isinstance(raw, dict) else {}
    return VoicePersonaResponse(
        display_name=str(persona.get("display_name") or clinic_name or "Clinic Recall"),
        tone=str(persona.get("tone") or "warm, concise, and professional"),
        voice_name=str(persona.get("voice_name") or "") or None,
    )


def _inbound_config_from_clinic(clinic: Clinic) -> InboundConfigResponse:
    policy = dict(clinic.consent_policy or {})
    raw = policy.get("inbound_config")
    config = raw if isinstance(raw, dict) else {}
    return InboundConfigResponse(
        greeting=str(config.get("greeting") or "Hello, thanks for calling. How can I help today?"),
        callback_sla_hours=int(config.get("callback_sla_hours") or 4),
        escalation_destination=str(config.get("escalation_destination") or "") or None,
    )


def _redacted_caller_hash(value: str | None) -> str:
    if not value:
        return "unknown"
    return f"hash:{value[-8:]}"


def _phone_webhook_url(record: ClinicPhoneNumber) -> str | None:
    if record.provider.value == "twilio" and record.purpose.value in {"inbound", "both"}:
        return "/api/v1/voice/twilio/twiml"
    if record.provider.value == "acs" and record.purpose.value in {"inbound", "both"}:
        return "/api/v1/calls/event"
    return None


def _phone_number_record(record: ClinicPhoneNumber) -> PhoneNumberRecord:
    return PhoneNumberRecord(
        id=record.id,
        provider=record.provider.value,
        phone_number=record.phone_number,
        purpose=record.purpose.value,
        status=record.status.value,
        webhook_url=_phone_webhook_url(record),
        test_status=str((record.config or {}).get("test_status") or "not_tested"),
    )


def _task_record(
    task: InboundStaffTask,
    receipt: HandoffReceipt | None = None,
    *,
    now: datetime | None = None,
) -> InboundTaskRecord:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    due_at = _aware(receipt.due_at) if receipt is not None else None
    return InboundTaskRecord(
        id=task.id,
        inbound_call_id=task.inbound_call_id,
        inbound_message_id=task.inbound_message_id,
        source="sms" if task.inbound_message_id else "call",
        kind=task.kind.value,
        status=task.status.value,
        priority=task.priority,
        reason=task.reason,
        summary=task.summary,
        created_at=task.created_at,
        severity=receipt.severity.value if receipt is not None else None,
        delivery_state=(
            receipt.delivery_state.value if receipt is not None else None
        ),
        queued_at=(
            _aware(receipt.queued_at) if receipt is not None else None
        ),
        due_at=due_at,
        overdue=(
            receipt is not None
            and receipt.acknowledged_at is None
            and receipt.resolved_at is None
            and due_at is not None
            and due_at <= observed_at
        ),
        acknowledged_at=(
            _aware(receipt.acknowledged_at)
            if receipt is not None and receipt.acknowledged_at is not None
            else None
        ),
        acknowledged_by=receipt.acknowledged_by if receipt is not None else None,
        alternate_requested=(
            receipt is not None and receipt.alternate_state.value == "requested"
        ),
        owner_resolved=receipt is not None and receipt.resolved_at is not None,
    )


def _message_record(message: InboundMessage) -> InboundMessageRecord:
    return InboundMessageRecord(
        id=message.id,
        provider=message.provider.value,
        from_number_redacted=_redacted_caller_hash(message.from_number_hash),
        intent=message.intent,
        status=message.status.value,
        summary=message.summary,
        created_at=message.created_at,
    )


STAFF_CONTEXT = Depends(staff_context)


@router.post("/signup", response_model=ClinicSignupResponse)
def signup_clinic(payload: ClinicSignupRequest, request: Request) -> ClinicSignupResponse:
    if not _env_enabled("ENABLE_SELF_SERVE_SIGNUP"):
        raise HTTPException(status_code=403, detail="Self-serve signup is not enabled")
    principal = _easy_auth_principal(request)
    subject, email = _ensure_signup_identity_available(principal)
    provider = _principal_provider(principal) if principal is not None else "aad"
    clinic_id_base = f"clinic-{_clinic_slug(payload.clinic_name)}"
    clinic_id = clinic_id_base
    with get_sessionmaker()() as session:
        identity_conditions = []
        if subject:
            identity_conditions.append(ClinicIdentityMapping.subject == subject)
        if email:
            identity_conditions.append(ClinicIdentityMapping.email == email)
        if identity_conditions:
            existing_mapping = session.execute(
                sa.select(ClinicIdentityMapping).where(
                    ClinicIdentityMapping.provider == provider,
                    sa.or_(*identity_conditions),
                )
            ).scalar_one_or_none()
            if existing_mapping is not None:
                raise HTTPException(status_code=409, detail="Clinic Recall identity is already mapped")
        if session.get(Clinic, clinic_id) is not None:
            clinic_id = f"{clinic_id_base}-{uuid.uuid4().hex[:8]}"
        branding: dict[str, Any] = {"sms_sender": payload.clinic_name.strip()[:64]}
        if payload.attribution:
            branding["attribution"] = {
                str(key)[:32]: str(value)[:128]
                for key, value in payload.attribution.items()
                if str(key).startswith("utm_")
            }
        clinic = Clinic(
            id=clinic_id,
            name=payload.clinic_name.strip(),
            timezone="Europe/London",
            daily_caps=25,
            contact_hours={"start_hour": 9, "end_hour": 17},
            branding=branding,
            consent_policy=_initial_onboarding_policy(payload.contact_email),
        )
        session.add(clinic)
        if subject or email:
            session.add(
                ClinicIdentityMapping(
                    id=f"identity-{uuid.uuid4().hex}",
                    clinic_id=clinic_id,
                    provider=provider,
                    subject=subject,
                    email=email,
                    roles=["staff", "operator"],
                    status="active",
                )
            )
        session.commit()
    return ClinicSignupResponse(
        clinic_id=clinic_id,
        status="pending",
        onboarding_next="connect_data",
    )


@router.get("/onboarding", response_model=OnboardingResponse)
def get_onboarding(context: StaffContext = STAFF_CONTEXT) -> OnboardingResponse:
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            return OnboardingResponse(
                **_overlay_connect_data_evidence(session, clinic, _onboarding_state(clinic))
            )


def _has_completed_import(session: Any, clinic_id: str) -> bool:
    """Durable server-side data-connection evidence: one completed import."""
    return (
        session.execute(
            sa.select(ImportBatch.id)
            .where(
                ImportBatch.clinic_id == clinic_id,
                ImportBatch.state == ImportBatchState.COMPLETED,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _overlay_connect_data_evidence(
    session: Any, clinic: Clinic, state: dict[str, Any]
) -> dict[str, Any]:
    """Overlay ``connect_data`` from durable evidence (PR-08).

    Client input can no longer mark the data-connection step complete; a
    stored legacy ``True`` stays honored, and a completed import batch
    completes the step server-side.
    """
    steps = dict(state["onboarding_steps"])
    derived = bool(steps.get("connect_data")) or _has_completed_import(session, clinic.id)
    if derived == steps.get("connect_data"):
        return state
    steps["connect_data"] = derived
    required = not all(steps.values())
    onboarding_step = state["onboarding_step"]
    if onboarding_step == "connect_data" and derived:
        onboarding_step = _next_onboarding_step(steps)
    outreach_enabled = state["outreach_enabled"]
    status = state["status"]
    if not required and outreach_enabled:
        status = "active"
    elif not required and status == "pending":
        status = "setup_complete"
    return {
        "status": status,
        "onboarding_required": required,
        "onboarding_step": onboarding_step,
        "onboarding_steps": steps,
        "outreach_enabled": outreach_enabled,
    }


@router.put("/onboarding", response_model=OnboardingResponse)
def update_onboarding(
    payload: OnboardingRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> OnboardingResponse:
    if payload.outreach_enabled is True:
        _require_operator(context)
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            state = _onboarding_state(clinic)
            steps = dict(state["onboarding_steps"])
            if payload.completed_step is not None:
                if payload.completed_step not in steps:
                    raise HTTPException(status_code=422, detail="Invalid onboarding step")
                if payload.completed_step != "connect_data":
                    # connect_data completes only from durable import/PMS
                    # evidence; the client value is ignored (PR-08).
                    steps[payload.completed_step] = True
            if payload.onboarding_steps is not None:
                for step, complete in payload.onboarding_steps.items():
                    if step not in steps:
                        raise HTTPException(status_code=422, detail="Invalid onboarding step")
                    if step == "connect_data":
                        continue  # server-derived; client input ignored (PR-08)
                    steps[step] = bool(complete)
            onboarding_required = not all(steps.values())
            onboarding_step = payload.onboarding_step or _next_onboarding_step(steps)
            if onboarding_step not in {*_ONBOARDING_STEPS, "complete"}:
                raise HTTPException(status_code=422, detail="Invalid onboarding step")
            outreach_enabled = state["outreach_enabled"]
            if payload.outreach_enabled is not None:
                outreach_enabled = payload.outreach_enabled
            status = state["status"]
            if not onboarding_required and outreach_enabled:
                status = "active"
            elif not onboarding_required and status == "pending":
                status = "setup_complete"
            state = {
                "status": status,
                "onboarding_required": onboarding_required,
                "onboarding_step": onboarding_step,
                "onboarding_steps": steps,
                "outreach_enabled": outreach_enabled,
            }
            _write_onboarding_state(clinic, state)
            session.flush()
            response = OnboardingResponse(
                **_overlay_connect_data_evidence(session, clinic, state)
            )
        session.commit()
    return response


@router.get("/monitor", response_model=MonitorResponse)
def get_monitor(context: StaffContext = STAFF_CONTEXT) -> MonitorResponse:
    recent_since = datetime.now(UTC) - timedelta(hours=24)
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            open_escalations = session.scalar(
                sa.select(sa.func.count())
                .select_from(Escalation)
                .where(Escalation.clinic_id == context.clinic_id)
                .where(Escalation.status == EscalationStatus.OPEN)
            ) or 0
            pending_bookings = session.scalar(
                sa.select(sa.func.count())
                .select_from(BookingAction)
                .where(BookingAction.clinic_id == context.clinic_id)
                .where(BookingAction.status == BookingActionStatus.PENDING)
            ) or 0
            queued_outbox = session.scalar(
                sa.select(sa.func.count())
                .select_from(OutreachJob)
                .where(OutreachJob.clinic_id == context.clinic_id)
                .where(OutreachJob.state == OutreachState.QUEUED)
            ) or 0
            active_campaigns = session.scalar(
                sa.select(sa.func.count())
                .select_from(Campaign)
                .where(Campaign.clinic_id == context.clinic_id)
                .where(Campaign.status == CampaignStatus.ACTIVE)
            ) or 0
            recent_interactions = session.scalar(
                sa.select(sa.func.count())
                .select_from(Interaction)
                .where(Interaction.clinic_id == context.clinic_id)
                .where(Interaction.occurred_at >= recent_since)
            ) or 0
            latest_escalation_at = session.scalar(
                sa.select(sa.func.max(Escalation.created_at)).where(
                    Escalation.clinic_id == context.clinic_id
                )
            )
            voice_rows = session.execute(
                sa.select(OutreachJob.state, sa.func.count())
                .where(OutreachJob.clinic_id == context.clinic_id)
                .where(OutreachJob.channel == Channel.CALL)
                .group_by(OutreachJob.state)
            ).all()
            latest_call_at = session.scalar(
                sa.select(sa.func.max(Interaction.occurred_at))
                .where(Interaction.clinic_id == context.clinic_id)
                .where(Interaction.channel == Channel.CALL)
            )
    return MonitorResponse(
        open_queue_count=int(open_escalations) + int(pending_bookings),
        queued_outbox_count=int(queued_outbox),
        active_campaigns=int(active_campaigns),
        recent_interactions_count=int(recent_interactions),
        latest_escalation_at=latest_escalation_at,
        voice_fallback_summary={
            "call_jobs_by_state": {state.value: int(count) for state, count in voice_rows},
            "latest_call_interaction_at": latest_call_at,
        },
    )


@router.get("/phone-numbers", response_model=PhoneNumbersResponse)
def get_phone_numbers(context: StaffContext = STAFF_CONTEXT) -> PhoneNumbersResponse:
    with get_sessionmaker()() as session:
        records = list(
            session.execute(
                sa.select(ClinicPhoneNumber)
                .where(ClinicPhoneNumber.clinic_id == context.clinic_id)
                .order_by(ClinicPhoneNumber.provider, ClinicPhoneNumber.phone_number)
            ).scalars()
        )
    return PhoneNumbersResponse(items=[_phone_number_record(record) for record in records])


@router.post("/phone-numbers/{number_id}/status", response_model=PhoneNumberRecord)
def update_phone_number_status(
    number_id: str,
    status: ClinicPhoneStatus,
    context: StaffContext = STAFF_CONTEXT,
) -> PhoneNumberRecord:
    _require_operator(context)
    with get_sessionmaker()() as session:
        record = session.execute(
            sa.select(ClinicPhoneNumber).where(
                ClinicPhoneNumber.id == number_id,
                ClinicPhoneNumber.clinic_id == context.clinic_id,
            )
        ).scalar_one_or_none()
        if record is None:
            raise HTTPException(status_code=404, detail="phone number not found")
        record.status = status
        session.commit()
        return _phone_number_record(record)


@router.get("/inbound-calls", response_model=InboundCallsResponse)
def get_inbound_calls(
    limit: int = 100,
    context: StaffContext = STAFF_CONTEXT,
) -> InboundCallsResponse:
    bounded_limit = max(1, min(limit, 250))
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            calls = list(
                session.execute(
                    tenant_select(InboundCall)
                    .order_by(InboundCall.created_at.desc(), InboundCall.id)
                    .limit(bounded_limit)
                ).scalars()
            )
    return InboundCallsResponse(
        items=[
            InboundCallRecord(
                id=call.id,
                provider=call.provider.value,
                provider_call_id=call.provider_call_id,
                called_number=call.called_number,
                caller_number_redacted=_redacted_caller_hash(call.caller_number_hash),
                status=call.status.value,
                outcome=call.outcome,
                created_at=call.created_at,
            )
            for call in calls
        ]
    )


@router.get("/call-records", response_model=CallLedgerStatusResponse)
def get_call_records(
    limit: int = 100,
    context: StaffContext = STAFF_CONTEXT,
) -> CallLedgerStatusResponse:
    """Return content-free consent and recording status for operator verification."""
    _require_operator(context)
    bounded_limit = max(1, min(limit, 250))
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            records = list(
                session.execute(
                    tenant_select(CallRecord)
                    .order_by(CallRecord.created_at.desc(), CallRecord.id)
                    .limit(bounded_limit)
                ).scalars()
            )
    return CallLedgerStatusResponse(
        items=[
            CallLedgerStatusRecord(
                id=record.id,
                provider=record.provider.value,
                direction=record.direction.value,
                scenario=record.scenario,
                patient_linked=record.patient_id is not None,
                provider_call_bound=record.provider_call_id is not None,
                consent_state=record.consent_state.value,
                consent_decision_source=(
                    record.consent_decision_source.value
                    if record.consent_decision_source is not None
                    else None
                ),
                consent_version=record.consent_version,
                consent_asked_at=record.consent_asked_at,
                consent_decided_at=record.consent_decided_at,
                recording_status=record.recording_status.value,
                recording_identity_bound=record.recording_sid is not None,
                recording_requested_at=record.recording_requested_at,
                recording_started_at=record.recording_started_at,
                recording_stop_requested_at=record.recording_stop_requested_at,
                recording_stopped_at=record.recording_stopped_at,
                deletion_state=record.deletion_state.value,
                started_at=record.started_at,
                ended_at=record.ended_at,
            )
            for record in records
        ]
    )


@router.get("/inbound-messages", response_model=InboundMessagesResponse)
def get_inbound_messages(
    limit: int = 100,
    context: StaffContext = STAFF_CONTEXT,
) -> InboundMessagesResponse:
    bounded_limit = max(1, min(limit, 250))
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            messages = list(
                session.execute(
                    tenant_select(InboundMessage)
                    .order_by(InboundMessage.created_at.desc(), InboundMessage.id)
                    .limit(bounded_limit)
                ).scalars()
            )
    return InboundMessagesResponse(items=[_message_record(message) for message in messages])


@router.get("/inbound-tasks", response_model=InboundTasksResponse)
def get_inbound_tasks(
    open_only: bool = True,
    limit: int = 100,
    context: StaffContext = STAFF_CONTEXT,
) -> InboundTasksResponse:
    bounded_limit = max(1, min(limit, 250))
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            stmt = tenant_select(InboundStaffTask).order_by(
                InboundStaffTask.created_at.desc(), InboundStaffTask.id
            )
            if open_only:
                stmt = stmt.where(InboundStaffTask.status != InboundStaffTaskStatus.RESOLVED)
            tasks = list(session.execute(stmt.limit(bounded_limit)).scalars())
            records = [
                _task_record(
                    task,
                    session.execute(
                        tenant_select(HandoffReceipt).where(
                            HandoffReceipt.inbound_staff_task_id == task.id
                        )
                    ).scalar_one_or_none(),
                )
                for task in tasks
            ]
    return InboundTasksResponse(items=records)


@router.post("/inbound-tasks/{task_id}/resolve", response_model=InboundTaskRecord)
def resolve_inbound_task(
    task_id: str,
    payload: InboundTaskResolveRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> InboundTaskRecord:
    now = datetime.now(UTC)
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            task = session.execute(
                tenant_select(InboundStaffTask).where(InboundStaffTask.id == task_id)
            ).scalar_one_or_none()
            if task is None:
                raise HTTPException(status_code=404, detail="inbound task not found")
            receipt = session.execute(
                tenant_select(HandoffReceipt).where(
                    HandoffReceipt.inbound_staff_task_id == task.id
                )
            ).scalar_one_or_none()
            if task.status == InboundStaffTaskStatus.CANCELLED:
                raise HTTPException(status_code=409, detail="cancelled inbound task cannot resolve")
            transitioned = task.status != InboundStaffTaskStatus.RESOLVED
            if transitioned:
                receipt, _acknowledged = acknowledge_handoff_owner(
                    session,
                    clinic_id=context.clinic_id,
                    owner=task,
                    actor=context.actor,
                    now=now,
                )
                task.status = InboundStaffTaskStatus.RESOLVED
                if payload.reason:
                    task.summary = (
                        (task.summary or "") + f"\nResolution: {payload.reason}"
                    ).strip()
                mark_handoff_resolved(
                    session,
                    receipt,
                    actor=context.actor,
                    now=now,
                )
                audit_action(
                    session,
                    context.clinic_id,
                    AuditAction.RESOLVE,
                    task.id,
                    {
                        "reason": payload.reason or "",
                        "resolved_by": context.actor,
                        "occurred_at": now,
                    },
                    actor=context.actor,
                )
                session.flush()
            response = _task_record(task, receipt)
        session.commit()
    return response


@router.post("/inbound-tasks/{task_id}/acknowledge", response_model=InboundTaskRecord)
def acknowledge_inbound_task(
    task_id: str,
    context: StaffContext = STAFF_CONTEXT,
) -> InboundTaskRecord:
    now = datetime.now(UTC)
    try:
        with get_sessionmaker()() as session:
            with clinic_scope(session, context.clinic_id):
                task = session.execute(
                    tenant_select(InboundStaffTask).where(
                        InboundStaffTask.id == task_id
                    )
                ).scalar_one_or_none()
                if task is None:
                    raise HTTPException(status_code=404, detail="inbound task not found")
                receipt, _transitioned = acknowledge_handoff_owner(
                    session,
                    clinic_id=context.clinic_id,
                    owner=task,
                    actor=context.actor,
                    now=now,
                )
                response = _task_record(task, receipt)
            session.commit()
        return response
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/inbound-config", response_model=InboundConfigResponse)
def get_inbound_config(context: StaffContext = STAFF_CONTEXT) -> InboundConfigResponse:
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            return _inbound_config_from_clinic(clinic)


@router.put("/inbound-config", response_model=InboundConfigResponse)
def update_inbound_config(
    payload: InboundConfigRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> InboundConfigResponse:
    _require_operator(context)
    if payload.recording_enabled is True:
        raise HTTPException(
            status_code=409,
            detail="Recording remains off pending approved wording, privacy, and carrier qualification",
        )
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            current = _inbound_config_from_clinic(clinic).model_dump()
            for field in ("greeting", "callback_sla_hours", "escalation_destination"):
                value = getattr(payload, field)
                if value is not None:
                    current[field] = value
            policy = dict(clinic.consent_policy or {})
            policy["inbound_config"] = current
            clinic.consent_policy = policy
            session.flush()
            response = InboundConfigResponse(**current)
        session.commit()
    return response


@router.get("/inbound-metrics", response_model=InboundMetricsResponse)
def get_inbound_metrics(context: StaffContext = STAFF_CONTEXT) -> InboundMetricsResponse:
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            calls_total = session.scalar(
                sa.select(sa.func.count())
                .select_from(InboundCall)
                .where(InboundCall.clinic_id == context.clinic_id)
            ) or 0
            calls_completed = session.scalar(
                sa.select(sa.func.count())
                .select_from(InboundCall)
                .where(InboundCall.clinic_id == context.clinic_id)
                .where(InboundCall.status == InboundCallStatus.COMPLETED)
            ) or 0
            open_tasks = session.scalar(
                sa.select(sa.func.count())
                .select_from(InboundStaffTask)
                .where(InboundStaffTask.clinic_id == context.clinic_id)
                .where(InboundStaffTask.status != InboundStaffTaskStatus.RESOLVED)
            ) or 0
            callbacks_open = session.scalar(
                sa.select(sa.func.count())
                .select_from(InboundStaffTask)
                .where(InboundStaffTask.clinic_id == context.clinic_id)
                .where(InboundStaffTask.kind == InboundStaffTaskKind.CALLBACK)
                .where(InboundStaffTask.status != InboundStaffTaskStatus.RESOLVED)
            ) or 0
            escalations_open = session.scalar(
                sa.select(sa.func.count())
                .select_from(InboundStaffTask)
                .where(InboundStaffTask.clinic_id == context.clinic_id)
                .where(InboundStaffTask.kind == InboundStaffTaskKind.ESCALATION)
                .where(InboundStaffTask.status != InboundStaffTaskStatus.RESOLVED)
            ) or 0
            booking_requests_open = session.scalar(
                sa.select(sa.func.count())
                .select_from(InboundStaffTask)
                .where(InboundStaffTask.clinic_id == context.clinic_id)
                .where(InboundStaffTask.kind == InboundStaffTaskKind.BOOKING_REQUEST)
                .where(InboundStaffTask.status != InboundStaffTaskStatus.RESOLVED)
            ) or 0
            texts_total = session.scalar(
                sa.select(sa.func.count())
                .select_from(InboundMessage)
                .where(InboundMessage.clinic_id == context.clinic_id)
            ) or 0
            texts_routed = session.scalar(
                sa.select(sa.func.count())
                .select_from(InboundMessage)
                .where(InboundMessage.clinic_id == context.clinic_id)
                .where(InboundMessage.status == "routed")
            ) or 0
            text_callbacks_open = _text_task_count(session, context.clinic_id, InboundStaffTaskKind.CALLBACK)
            text_escalations_open = _text_task_count(session, context.clinic_id, InboundStaffTaskKind.ESCALATION)
            text_booking_requests_open = _text_task_count(
                session, context.clinic_id, InboundStaffTaskKind.BOOKING_REQUEST
            )
            text_identity_unclear_open = _text_task_count(
                session, context.clinic_id, InboundStaffTaskKind.IDENTITY_UNCLEAR
            )
    return InboundMetricsResponse(
        calls_total=int(calls_total),
        calls_completed=int(calls_completed),
        texts_total=int(texts_total),
        texts_routed=int(texts_routed),
        open_tasks=int(open_tasks),
        callbacks_open=int(callbacks_open),
        escalations_open=int(escalations_open),
        booking_requests_open=int(booking_requests_open),
        text_callbacks_open=int(text_callbacks_open),
        text_escalations_open=int(text_escalations_open),
        text_booking_requests_open=int(text_booking_requests_open),
        text_identity_unclear_open=int(text_identity_unclear_open),
    )


def _text_task_count(session: Any, clinic_id: str, kind: InboundStaffTaskKind) -> int:
    return int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(InboundStaffTask)
            .where(InboundStaffTask.clinic_id == clinic_id)
            .where(InboundStaffTask.inbound_message_id.is_not(None))
            .where(InboundStaffTask.kind == kind)
            .where(InboundStaffTask.status != InboundStaffTaskStatus.RESOLVED)
        )
        or 0
    )


def _staff_queue_pilot_gate() -> JobPilotGate:
    patient_gate = patient_gate_for_snapshot(operational_switch_snapshot_from_environment())

    def gate(
        session: Any,
        clinic_id: str,
        job: OutreachJob,
        now: datetime,
    ):
        return patient_gate(session, clinic_id, job.patient_id, Channel.SMS, now)

    return gate


@router.get("/queue", response_model=QueueResponse)
def get_queue(context: StaffContext = STAFF_CONTEXT) -> QueueResponse:
    with get_sessionmaker()() as session:
        items = list_staff_queue(session, context.clinic_id)
    return QueueResponse(items=items)


@router.get("/inbox", response_model=QueueResponse)
def get_inbox(context: StaffContext = STAFF_CONTEXT) -> QueueResponse:
    with get_sessionmaker()() as session:
        items = list_staff_queue(session, context.clinic_id)
    return QueueResponse(items=items)


@router.post("/queue/{item_id:path}/resolve")
def resolve_queue(
    item_id: str,
    payload: ResolveQueueRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    try:
        now = datetime.now(UTC)
        with get_sessionmaker()() as session:
            result = resolve_queue_item(
                session,
                context.clinic_id,
                item_id,
                payload.decision,
                staff_actor=context.actor,
                now=now,
                pilot_gate=_staff_queue_pilot_gate(),
                write_back_enabled=durable_cliniko_write_enabled(),
                reason=payload.reason,
                identity_service=runtime_identity_service(now),
            )
            session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump(mode="json")


@router.post(
    "/callbacks/acs-email-delivery",
    include_in_schema=False,
)
async def receive_handoff_email_delivery(request: Request) -> dict[str, int | str]:
    if not handoff_delivery_callback_enabled():
        raise HTTPException(status_code=404, detail="not found")
    await _authenticate_handoff_delivery_request(request)
    _subscription_name, topic_id = _handoff_event_grid_authority(request)
    raw_payload = await request.body()
    try:
        events = json.loads(raw_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid Event Grid payload") from exc
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="invalid Event Grid payload")
    if (
        len(events) == 1
        and isinstance(events[0], dict)
        and events[0].get("eventType")
        == "Microsoft.EventGrid.SubscriptionValidationEvent"
    ):
        data = events[0].get("data")
        validation_code = data.get("validationCode") if isinstance(data, dict) else None
        if not isinstance(validation_code, str) or not 1 <= len(validation_code) <= 128:
            raise HTTPException(status_code=400, detail="invalid validation event")
        return {"validationResponse": validation_code}
    if any(
        not isinstance(event, dict) or str(event.get("topic") or "") != topic_id
        for event in events
    ):
        raise HTTPException(status_code=403, detail="Event Grid topic mismatch")
    try:
        with get_sessionmaker()() as session:
            result = receive_acs_email_events(
                session,
                events=events,
                raw_payload=raw_payload,
                received_at=datetime.now(UTC),
            )
            session.commit()
    except HandoffDeliveryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HandoffDeliveryCorrelationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "received": result.received,
        "created": result.created,
        "delivered": result.delivered,
        "definitive_failures": result.definitive_failures,
        "nonterminal": result.nonterminal,
        "alternate_requested": result.alternate_requested,
        "programmes_paused": result.programmes_paused,
    }


@router.post("/inbox/{item_id:path}/acknowledge")
def acknowledge_inbox_item(
    item_id: str,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    try:
        now = datetime.now(UTC)
        with get_sessionmaker()() as session:
            result = acknowledge_queue_item(
                session,
                context.clinic_id,
                item_id,
                staff_actor=context.actor,
                now=now,
            )
            session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump(mode="json")


class CreateIncidentRequest(BaseModel):
    """Anonymous staff incident report. Deliberately has NO reporter fields."""

    model_config = ConfigDict(extra="forbid")

    category: str = Field(default="other")
    severity: str = Field(default="no_harm")
    description: str = Field(min_length=1, max_length=4000)
    related_job_id: str | None = None


class IncidentStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


def _incident_json(report: Any) -> dict[str, Any]:
    return {
        "id": report.id,
        "source": report.source.value,
        "category": report.category.value,
        "severity": report.severity.value,
        "description": report.description,
        "related_job_id": report.related_job_id,
        "status": report.status.value,
        "occurred_hour": report.occurred_hour.isoformat(),
        "reviewed_at": report.reviewed_at.isoformat() if report.reviewed_at else None,
    }


@router.get("/incidents")
def get_incidents(
    status: str | None = None,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    """List anonymous incident reports for the staff clinic (server-scoped)."""
    try:
        with get_sessionmaker()() as session, clinic_scope(session, context.clinic_id):
            items = list_incidents(session, context.clinic_id, status=status)
            payload = [_incident_json(item) for item in items]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"items": payload}


@router.post("/incidents", status_code=201)
def post_incident(
    payload: CreateIncidentRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    """File an anonymous STAFF incident report. No reporter identity is stored."""
    try:
        with get_sessionmaker()() as session, clinic_scope(session, context.clinic_id):
            report = create_incident(
                session,
                context.clinic_id,
                source="staff",
                description=payload.description,
                category=payload.category,
                severity=payload.severity,
                related_job_id=payload.related_job_id,
                now=datetime.now(UTC),
            )
            body = _incident_json(report)
            session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return body


@router.post("/incidents/{incident_id:path}/status")
def post_incident_status(
    incident_id: str,
    payload: IncidentStatusRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    """Advance an incident through the governance workflow."""
    try:
        with get_sessionmaker()() as session, clinic_scope(session, context.clinic_id):
            report = update_incident_status(
                session,
                context.clinic_id,
                incident_id,
                status=payload.status,
                now=datetime.now(UTC),
            )
            body = _incident_json(report)
            session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return body


@router.get("/outbox")
def get_outbox(
    now: datetime | None = None,
    limit: int = 100,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    try:
        with get_sessionmaker()() as session:
            items = list_outbox_items(
                session,
                context.clinic_id,
                _aware(now or datetime.now(UTC)),
                limit=limit,
                pilot_gate=patient_gate_for_snapshot(
                    operational_switch_snapshot_from_environment()
                ),
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.get("/interactions")
def get_interactions(
    limit: int = 100,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    try:
        with get_sessionmaker()() as session:
            items = list_interaction_timeline(session, context.clinic_id, limit=limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.get("/roi")
def roi(
    start: datetime,
    end: datetime,
    subscription_cost: Decimal = Decimal("199.00"),
    usage_cost: Decimal = Decimal("0.00"),
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    with get_sessionmaker()() as session:
        metrics = get_roi_metrics(
            session,
            context.clinic_id,
            start=_aware(start),
            end=_aware(end),
            subscription_cost=subscription_cost,
            usage_cost=usage_cost,
        )
    return metrics.model_dump(mode="json")


@router.get("/roi.csv")
def roi_csv(
    start: datetime,
    end: datetime,
    subscription_cost: Decimal = Decimal("199.00"),
    usage_cost: Decimal = Decimal("0.00"),
    context: StaffContext = STAFF_CONTEXT,
) -> Response:
    with get_sessionmaker()() as session:
        metrics = get_roi_metrics(
            session,
            context.clinic_id,
            start=_aware(start),
            end=_aware(end),
            subscription_cost=subscription_cost,
            usage_cost=usage_cost,
        )
    return Response(content=roi_metrics_csv(metrics), media_type="text/csv")


@router.get("/campaign/settings", response_model=CampaignSettingsResponse)
def get_campaign_settings(context: StaffContext = STAFF_CONTEXT) -> CampaignSettingsResponse:
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            return CampaignSettingsResponse.model_validate(clinic)


@router.put("/campaign/settings", response_model=CampaignSettingsResponse)
def update_campaign_settings(
    payload: CampaignSettingsRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> CampaignSettingsResponse:
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            if payload.daily_caps is not None:
                clinic.daily_caps = payload.daily_caps
            if payload.branding is not None:
                clinic.branding = payload.branding
            if payload.contact_hours is not None:
                clinic.contact_hours = payload.contact_hours
            session.flush()
            response = CampaignSettingsResponse.model_validate(clinic)
        session.commit()
    return response


@router.post("/campaigns/launch")
def launch_campaign(
    payload: CampaignLaunchRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    now = _aware(payload.now or datetime.now(UTC))
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            _require_outreach_enabled(clinic)
        result = generate_candidate_queue(
            session,
            context.clinic_id,
            now,
            channel=payload.channel,
            pilot_gate=patient_gate_for_snapshot(
                operational_switch_snapshot_from_environment()
            ),
        )
        session.commit()
    return {"candidate_queue": result.as_summary()}


@router.post("/voice/fallback/run")
def run_voice_fallback(
    payload: VoiceFallbackRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    _require_operator(context)
    now = _aware(payload.now or datetime.now(UTC))
    with get_sessionmaker()() as session:
        switches = operational_switch_snapshot_from_environment()
        result = run_voice_cadence(
            session,
            context.clinic_id,
            now,
            programme_gate=job_gate_for_snapshot(switches, Channel.CALL),
        )
        session.commit()
    return {"voice_fallback": result.as_summary()}


@router.get("/operator/script-templates", response_model=ScriptTemplatesResponse)
def get_script_templates(context: StaffContext = STAFF_CONTEXT) -> ScriptTemplatesResponse:
    _require_operator(context)
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            branding = _branding_dict(clinic)
            templates = branding.get("script_templates")
            if not isinstance(templates, dict):
                templates = _default_script_templates()
            return ScriptTemplatesResponse(templates={str(k): str(v) for k, v in templates.items()})


@router.put("/operator/script-templates", response_model=ScriptTemplatesResponse)
def update_script_templates(
    payload: ScriptTemplatesRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> ScriptTemplatesResponse:
    _require_operator(context)
    templates = _validate_script_templates(payload.templates)
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            branding = _branding_dict(clinic)
            branding["script_templates"] = templates
            clinic.branding = branding
            session.flush()
        session.commit()
    return ScriptTemplatesResponse(templates=templates)


@router.get("/operator/voice-persona", response_model=VoicePersonaResponse)
def get_voice_persona(context: StaffContext = STAFF_CONTEXT) -> VoicePersonaResponse:
    _require_operator(context)
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            return _voice_persona_from_branding(_branding_dict(clinic), clinic.name)


@router.put("/operator/voice-persona", response_model=VoicePersonaResponse)
def update_voice_persona(
    payload: VoicePersonaRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> VoicePersonaResponse:
    _require_operator(context)
    display_name = (payload.display_name or "Clinic Recall").strip()
    tone = (payload.tone or "warm, concise, and professional").strip()
    voice_name = (payload.voice_name or "").strip() or None
    if len(display_name) < 2:
        raise HTTPException(status_code=422, detail="display_name is too short")
    if len(tone) < 5:
        raise HTTPException(status_code=422, detail="tone is too short")
    persona = {"display_name": display_name, "tone": tone, "voice_name": voice_name}
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            clinic = session.get(Clinic, context.clinic_id)
            if clinic is None:
                raise HTTPException(status_code=404, detail="clinic not found")
            branding = _branding_dict(clinic)
            branding["voice_persona"] = persona
            clinic.branding = branding
            session.flush()
        session.commit()
    return VoicePersonaResponse(**persona)


@router.post("/operator/prompt-proposal", response_model=PromptProposalResponse)
def preview_prompt_proposal(
    payload: PromptProposalRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> PromptProposalResponse:
    _require_operator(context)
    prompt_path, _proposed, diff = _prompt_proposal_diff(payload)
    return PromptProposalResponse(prompt_path=str(prompt_path), diff=diff)


@router.post("/operator/prompt-proposals", response_model=PromptProposalRecord)
def submit_prompt_proposal(
    payload: PromptProposalRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> PromptProposalRecord:
    _require_operator(context)
    _prompt_path, proposed, diff = _prompt_proposal_diff(payload)
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            proposal = PromptProposal(
                id=f"prompt-proposal-{uuid.uuid4().hex}",
                clinic_id=context.clinic_id,
                actor=context.actor,
                status=PromptProposalStatus.SUBMITTED,
                proposed_prompt=proposed,
                diff=diff,
            )
            session.add(proposal)
            session.flush()
            session.refresh(proposal)
            record = _prompt_proposal_record(proposal)
        session.commit()
    return record


@router.get("/operator/prompt-proposals", response_model=PromptProposalListResponse)
def list_prompt_proposals(
    limit: int = 50,
    context: StaffContext = STAFF_CONTEXT,
) -> PromptProposalListResponse:
    _require_operator(context)
    safe_limit = max(1, min(limit, 100))
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            proposals = list(
                session.execute(
                    tenant_select(PromptProposal)
                    .order_by(PromptProposal.created_at.desc())
                    .limit(safe_limit)
                ).scalars()
            )
            return PromptProposalListResponse(
                proposals=[_prompt_proposal_record(proposal) for proposal in proposals]
            )


@router.get("/operator/prompt-proposals/{proposal_id}", response_model=PromptProposalRecord)
def get_prompt_proposal(
    proposal_id: str,
    context: StaffContext = STAFF_CONTEXT,
) -> PromptProposalRecord:
    _require_operator(context)
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            proposal = session.execute(
                tenant_select(PromptProposal).where(PromptProposal.id == proposal_id)
            ).scalar_one_or_none()
            if proposal is None:
                raise HTTPException(status_code=404, detail="prompt proposal not found")
            return _prompt_proposal_record(proposal)


@router.post("/campaigns/{campaign_id}/approve")
def approve_campaign(
    campaign_id: str,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    return _set_campaign_status(campaign_id, CampaignStatus.ACTIVE, context)


@router.post("/operator/pilot/programmes")
def create_pilot_programme_endpoint(
    payload: PilotProgrammeCreateRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    _require_operator(context)
    try:
        with get_sessionmaker()() as session:
            programme = create_programme(
                session,
                clinic_id=context.clinic_id,
                programme_id=payload.programme_id,
                environment=payload.environment,
                release_identity=payload.release_identity,
            )
            response = _pilot_programme_record(session, programme)
            session.commit()
            return response
    except PilotControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/operator/pilot/programmes/{programme_id}/participants")
def enroll_pilot_participant_endpoint(
    programme_id: str,
    payload: PilotParticipantEnrollRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    _require_operator(context)
    try:
        with get_sessionmaker()() as session:
            participant = enroll_participant(
                session,
                clinic_id=context.clinic_id,
                programme_id=programme_id,
                patient_id=payload.patient_id,
                now=datetime.now(UTC),
            )
            response = {
                "participant_id": participant.id,
                "ordinal": participant.ordinal,
                "wave": participant.wave,
                "released": participant.released_at is not None,
            }
            session.commit()
            return response
    except PilotControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/operator/pilot/programmes/{programme_id}/release")
def release_pilot_wave_endpoint(
    programme_id: str,
    payload: PilotReleaseRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    _require_operator(context)
    try:
        with get_sessionmaker()() as session:
            programme = release_cumulative_limit(
                session,
                clinic_id=context.clinic_id,
                programme_id=programme_id,
                cumulative_limit=payload.cumulative_limit,
                actor=context.actor,
                evidence_hash=payload.evidence_hash,
                now=datetime.now(UTC),
            )
            response = _pilot_programme_record(session, programme)
            session.commit()
            return response
    except PilotControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/operator/pilot/programmes/{programme_id}/dark")
def dark_pilot_programme_endpoint(
    programme_id: str,
    payload: PilotDarkRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    _require_operator(context)
    try:
        with get_sessionmaker()() as session:
            programme = mark_programme_dark(
                session,
                clinic_id=context.clinic_id,
                programme_id=programme_id,
                actor=context.actor,
                evidence_hash=payload.evidence_hash,
                now=datetime.now(UTC),
            )
            response = _pilot_programme_record(session, programme)
            session.commit()
            return response
    except PilotControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/operator/pilot/programmes/{programme_id}/pause")
def pause_pilot_programme_endpoint(
    programme_id: str,
    payload: PilotPauseRequest,
    background_tasks: BackgroundTasks,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    _require_operator(context)
    try:
        with get_sessionmaker()() as session:
            programme = pause_programme(
                session,
                clinic_id=context.clinic_id,
                programme_id=programme_id,
                actor=context.actor,
                reason=payload.reason,
                now=datetime.now(UTC),
            )
            response = _pilot_programme_record(session, programme)
            session.commit()
            background_tasks.add_task(_dispatch_recording_stop_batch, context.clinic_id)
            return response
    except PilotControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/operator/pilot/programmes/{programme_id}/close")
def close_pilot_programme_endpoint(
    programme_id: str,
    payload: PilotPauseRequest,
    background_tasks: BackgroundTasks,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    _require_operator(context)
    try:
        with get_sessionmaker()() as session:
            programme = close_programme(
                session,
                clinic_id=context.clinic_id,
                programme_id=programme_id,
                actor=context.actor,
                reason=payload.reason,
                now=datetime.now(UTC),
            )
            response = _pilot_programme_record(session, programme)
            session.commit()
            background_tasks.add_task(_dispatch_recording_stop_batch, context.clinic_id)
            return response
    except PilotControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _dispatch_recording_stop_batch(clinic_id: str) -> None:
    from src.clinic_recall.durable.recording_worker import run_runtime_batch

    try:
        run_runtime_batch(
            clinic_id=clinic_id,
            worker_id=f"recording-pilot-stop-{uuid.uuid4().hex[:12]}",
            limit=50,
        )
    except Exception:  # noqa: BLE001 - committed stop effects remain recoverable
        logger.error("Recording stop batch failed after pilot state commit")


@router.get("/operator/pilot/programmes")
def list_pilot_programmes_endpoint(
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    _require_operator(context)
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            programmes = list(
                session.execute(
                    tenant_select(PilotProgramme).order_by(
                        PilotProgramme.created_at,
                        PilotProgramme.id,
                    )
                ).scalars()
            )
            return {
                "programmes": [
                    _pilot_programme_record(session, programme)
                    for programme in programmes
                ]
            }


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(
    campaign_id: str,
    context: StaffContext = STAFF_CONTEXT,
) -> dict[str, Any]:
    return _set_campaign_status(campaign_id, CampaignStatus.PAUSED, context)


@router.get("/campaigns")
def list_campaigns(context: StaffContext = STAFF_CONTEXT) -> dict[str, Any]:
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            campaigns = list(session.query(Campaign).filter(Campaign.clinic_id == context.clinic_id))
            jobs = list(session.query(OutreachJob).filter(OutreachJob.clinic_id == context.clinic_id))
    job_counts: dict[str, int] = {}
    for job in jobs:
        job_counts[job.campaign_id] = job_counts.get(job.campaign_id, 0) + 1
    return {
        "campaigns": [
            {
                "id": campaign.id,
                "type": campaign.type.value,
                "status": campaign.status.value,
                "jobs": job_counts.get(campaign.id, 0),
                "is_launchable": campaign.status in {CampaignStatus.DRAFT, CampaignStatus.ACTIVE},
                "is_approvable": campaign.status in {CampaignStatus.DRAFT, CampaignStatus.PAUSED},
                "is_pausable": campaign.status == CampaignStatus.ACTIVE,
            }
            for campaign in campaigns
        ]
    }


def _set_campaign_status(
    campaign_id: str,
    status: CampaignStatus,
    context: StaffContext,
) -> dict[str, Any]:
    with get_sessionmaker()() as session:
        with clinic_scope(session, context.clinic_id):
            campaign = session.execute(
                tenant_select(Campaign).where(Campaign.id == campaign_id)
            ).scalar_one_or_none()
            if campaign is None:
                raise HTTPException(status_code=404, detail="campaign not found")
            if status == CampaignStatus.ACTIVE:
                clinic = session.get(Clinic, context.clinic_id)
                if clinic is None:
                    raise HTTPException(status_code=404, detail="clinic not found")
                _require_outreach_enabled(clinic)
            campaign.status = status
            response = {
                "id": campaign.id,
                "type": campaign.type.value,
                "status": campaign.status.value,
            }
        session.commit()
    return response


def _pilot_programme_record(
    session,
    programme: PilotProgramme,
) -> dict[str, Any]:
    participant_count = int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(PilotParticipant)
            .where(
                PilotParticipant.clinic_id == programme.clinic_id,
                PilotParticipant.pilot_programme_id == programme.id,
            )
        )
        or 0
    )
    released_count = int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(PilotParticipant)
            .where(
                PilotParticipant.clinic_id == programme.clinic_id,
                PilotParticipant.pilot_programme_id == programme.id,
                PilotParticipant.released_at.is_not(None),
            )
        )
        or 0
    )
    return {
        "id": programme.id,
        "environment": programme.environment,
        "release_identity": programme.release_identity,
        "state": programme.state.value,
        "maximum_participants": programme.maximum_unique_patients,
        "active_cumulative_limit": programme.active_cumulative_limit,
        "participant_count": participant_count,
        "released_count": released_count,
        "pause_reason": programme.pause_reason,
    }


@router.post("/patients/{patient_id}/erase", status_code=202)
def erase_patient_endpoint(
    patient_id: str,
    payload: ErasePatientRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> RightsRequestResponse:
    actor_role = _require_rights_access(context)
    try:
        with get_privacy_sessionmaker()() as session:
            result = request_patient_erasure(
                session,
                clinic_id=context.clinic_id,
                patient_id=patient_id,
                confirm_token=payload.confirm_token,
                request_identity=f"api:{uuid.uuid4().hex}",
                actor_role=actor_role,
                actor_reference=context.actor,
                keyring=get_rights_subject_keyring(),
                policy=get_rights_policy(),
                now=datetime.now(UTC),
            )
            session.commit()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Clinic Recall rights configuration blocked") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Rights subject not found") from exc
    return RightsRequestResponse(
        request_id=result.request_id,
        state=result.state.value,
        created=result.created,
        target_count=result.target_count,
        due_at=result.due_at,
    )


@router.get("/rights/operations")
def rights_operations_status_endpoint(
    context: StaffContext = STAFF_CONTEXT,
) -> RightsOperationsResponse:
    _require_rights_access(context)
    with get_privacy_sessionmaker()() as session:
        status = get_rights_operations_status(
            session,
            clinic_id=context.clinic_id,
            now=datetime.now(UTC),
        )
    return RightsOperationsResponse(
        request_count=status.request_count,
        incomplete_request_count=status.incomplete_request_count,
        target_count=status.target_count,
        pending_count=status.pending_count,
        reconcile_required_count=status.reconcile_required_count,
        handoff_count=status.handoff_count,
        unapproved_residual_count=status.unapproved_residual_count,
        overdue_count=status.overdue_count,
        zero_overdue=status.zero_overdue,
        ready=status.ready,
    )


@router.get("/rights/{request_id}")
def rights_request_status_endpoint(
    request_id: str,
    context: StaffContext = STAFF_CONTEXT,
) -> RightsStatusResponse:
    _require_rights_access(context)
    try:
        with get_privacy_sessionmaker()() as session:
            status = get_rights_request_status(
                session,
                clinic_id=context.clinic_id,
                request_id=request_id,
                now=datetime.now(UTC),
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Rights request not found") from exc
    return RightsStatusResponse(
        request_id=status.request_id,
        state=status.state.value,
        target_count=status.target_count,
        pending_count=status.pending_count,
        verified_count=status.verified_count,
        residual_count=status.residual_count,
        unapproved_residual_count=status.unapproved_residual_count,
        overdue_count=status.overdue_count,
        requested_at=status.requested_at,
        due_at=status.due_at,
        completed_at=status.completed_at,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# --------------------------------------------------------------------------- #
# PR-08: controlled CSV preview and import
# --------------------------------------------------------------------------- #

_CSV_FILE_FIELD = "file"
_CSV_FORM_FIELDS = frozenset(
    {
        "source_system",
        "export_at",
        "attestation_version",
        "attested_channels",
        "confirm_clinic_authority",
    }
)
_CSV_PREVIEW_FIELDS = frozenset({"source_system", "export_at"})
_CSV_APPROVAL_FIELDS = _CSV_FORM_FIELDS
_CSV_MULTIPART_OVERHEAD = 64 * 1024
_CSV_READ_CHUNK = 1024 * 1024
_CSV_METADATA_MAX_CHARS = 256


class CsvImportBatchRecord(BaseModel):
    """Aggregate, metadata-only provenance for one import batch."""

    id: str
    state: str
    file_sha256: str
    schema_version: str
    source_system: str
    export_at: datetime
    total_rows: int
    valid_row_count: int
    invalid_row_count: int
    patient_count: int
    appointment_count: int
    error_count: int
    error_reason_counts: dict[str, int] | None = None
    patients_inserted: int
    patients_updated: int
    appointments_inserted: int
    appointments_updated: int
    consent_granted_count: int
    consent_unknown_count: int
    opt_out_count: int
    consent_authority_granted: bool
    preview_expires_at: datetime
    approved_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class CsvPreviewErrorRecord(BaseModel):
    """One bounded row/file validation finding (response-only, never stored)."""

    reason: str
    field: str
    record: int | None = None
    line: int | None = None


class CsvImportConfigResponse(BaseModel):
    enabled: bool
    matching_enabled: bool
    schema_version: str
    source_systems: list[str]
    attestation_statement: str
    attestation_version: str
    consent_policy_version: str
    consent_channels: list[str]
    consent_authority_available: bool
    preview_ttl_minutes: int
    max_bytes: int
    max_rows: int
    required_columns: list[str]
    optional_columns: list[str]


class CsvPreviewResponse(BaseModel):
    batch: CsvImportBatchRecord
    importable: bool
    errors: list[CsvPreviewErrorRecord]


class CsvApproveResponse(BaseModel):
    batch: CsvImportBatchRecord
    replayed: bool


class CsvImportHistoryResponse(BaseModel):
    batches: list[CsvImportBatchRecord]


class ImportMatchReviewRecord(BaseModel):
    """Minimum authorized matching facts; no provider payloads or raw refs."""

    id: str
    import_batch_id: str
    provider: str
    strategy: str
    strategy_version: str
    state: str
    candidate_count: int
    reason: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class ImportMatchListResponse(BaseModel):
    reviews: list[ImportMatchReviewRecord]
    unmatched_count: int
    ambiguous_count: int
    pending_count: int


class ImportMatchCandidateRecord(BaseModel):
    token: str
    ordinal: int
    active: bool
    expires_at: datetime


class ImportMatchCandidatesResponse(BaseModel):
    review: ImportMatchReviewRecord
    candidates: list[ImportMatchCandidateRecord]


class ResolveImportMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(pattern="^(link|dismiss)$")
    candidate_token: str | None = Field(default=None, min_length=80, max_length=2048)


_CSV_IMPORT_HTTP_STATUS: dict[str, int] = {
    "batch_not_found": 404,
    "not_importable": 409,
    "preview_expired": 409,
    "file_hash_mismatch": 409,
    "source_metadata_mismatch": 409,
    "attestation_invalid": 422,
    "subject_frozen": 409,
    "source_link_conflict": 409,
    "policy_mismatch": 409,
    "invalid_state": 409,
    "source_system_not_allowed": 422,
    "export_time_invalid": 422,
    "import_disabled": 403,
}


def _batch_record(batch: ImportBatch) -> CsvImportBatchRecord:
    return CsvImportBatchRecord(
        id=batch.id,
        state=batch.state.value,
        file_sha256=batch.file_sha256,
        schema_version=batch.schema_version,
        source_system=batch.source_system.value,
        export_at=_aware(batch.export_at),
        total_rows=batch.total_rows,
        valid_row_count=batch.valid_row_count,
        invalid_row_count=batch.invalid_row_count,
        patient_count=batch.patient_count,
        appointment_count=batch.appointment_count,
        error_count=batch.error_count,
        error_reason_counts=batch.error_reason_counts,
        patients_inserted=batch.patients_inserted,
        patients_updated=batch.patients_updated,
        appointments_inserted=batch.appointments_inserted,
        appointments_updated=batch.appointments_updated,
        consent_granted_count=batch.consent_granted_count,
        consent_unknown_count=batch.consent_unknown_count,
        opt_out_count=batch.opt_out_count,
        consent_authority_granted=batch.consent_authority_granted,
        preview_expires_at=_aware(batch.preview_expires_at),
        approved_at=_aware(batch.approved_at) if batch.approved_at else None,
        completed_at=_aware(batch.completed_at) if batch.completed_at else None,
        created_at=_aware(batch.created_at),
    )


def _csv_import_http_error(exc: Exception) -> HTTPException:
    from src.clinic_recall.sync.csv_import import CsvImportError

    if isinstance(exc, CsvImportError):
        return HTTPException(
            status_code=_CSV_IMPORT_HTTP_STATUS.get(exc.reason, 409),
            detail=exc.reason,
        )
    return HTTPException(status_code=500, detail="csv import failed")


def _require_csv_import_enabled() -> None:
    if not csv_import_enabled():
        raise HTTPException(status_code=403, detail="CSV import is not enabled")


async def _read_csv_multipart(
    request: Request, *, expected_fields: frozenset[str]
) -> tuple[bytes, dict[str, str], datetime]:
    """Read exactly one bounded CSV file plus closed form fields.

    Runs only after ``StaffContext`` authorization. The multipart form context
    closes and disposes every part (including the spooled upload) before this
    function returns; the returned timestamp is the disposal evidence. The
    original client filename is never read, logged, or persisted.
    """
    from src.clinic_recall.sync.csv_adapter import MAX_BYTES

    length_header = request.headers.get("content-length", "")
    if not length_header.isdigit():
        raise HTTPException(status_code=411, detail="Content-Length required")
    if int(length_header) > MAX_BYTES + _CSV_MULTIPART_OVERHEAD:
        raise HTTPException(status_code=413, detail="upload exceeds the size limit")

    data: bytes | None = None
    fields: dict[str, str] = {}
    async with request.form(
        max_files=1,
        max_fields=len(expected_fields) + 1,
        max_part_size=MAX_BYTES + _CSV_MULTIPART_OVERHEAD,
    ) as form:
        for key, value in form.multi_items():
            if hasattr(value, "read"):  # an upload part
                if key != _CSV_FILE_FIELD or data is not None:
                    raise HTTPException(
                        status_code=422, detail="exactly one csv file part is required"
                    )
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = await value.read(_CSV_READ_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(
                            status_code=413, detail="upload exceeds the size limit"
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
            else:
                if key == "clinic_id":
                    raise HTTPException(
                        status_code=422, detail="clinic_id is derived server-side"
                    )
                if key not in expected_fields:
                    raise HTTPException(status_code=422, detail="unknown form field")
                if key in fields:
                    raise HTTPException(status_code=422, detail="duplicate form field")
                text = str(value)
                if len(text) > _CSV_METADATA_MAX_CHARS:
                    raise HTTPException(
                        status_code=422,
                        detail="form field exceeds the size limit",
                    )
                fields[key] = text
    if data is None:
        raise HTTPException(status_code=422, detail="exactly one csv file part is required")
    if set(fields) != expected_fields:
        raise HTTPException(status_code=422, detail="required form field is missing")
    return data, fields, datetime.now(UTC)


def _parse_source_fields(fields: dict[str, str]) -> tuple[Any, datetime]:
    from src.clinic_recall.enums import SourceSystem

    try:
        source_system = SourceSystem(fields.get("source_system", ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid source_system") from exc
    raw_export = (fields.get("export_at") or "").strip()
    if len(raw_export) > 64 or ("Z" in raw_export and not raw_export.endswith("Z")):
        raise HTTPException(status_code=422, detail="invalid export_at")
    if raw_export.endswith("Z"):
        raw_export = raw_export[:-1] + "+00:00"
    try:
        export_at = datetime.fromisoformat(raw_export)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid export_at") from exc
    if export_at.tzinfo is None or export_at.utcoffset() is None:
        raise HTTPException(status_code=422, detail="export_at must be timezone-aware")
    return source_system, export_at.astimezone(UTC)


@router.get("/imports/csv/config", response_model=CsvImportConfigResponse)
def get_csv_import_config(context: StaffContext = STAFF_CONTEXT) -> CsvImportConfigResponse:
    from src.clinic_recall.config import CSV_ATTESTATION_STATEMENT, CSV_ATTESTATION_VERSION
    from src.clinic_recall.schemas import REQUIRED_COLUMNS
    from src.clinic_recall.sync.csv_adapter import (
        MAX_BYTES,
        MAX_ROWS,
        OPTIONAL_COLUMNS,
    )

    policy = get_csv_import_policy()
    return CsvImportConfigResponse(
        enabled=csv_import_enabled(),
        matching_enabled=csv_matching_enabled(),
        schema_version="wulo-csv-v1",
        source_systems=[system.value for system in policy.allowed_source_systems],
        attestation_statement=CSV_ATTESTATION_STATEMENT,
        attestation_version=CSV_ATTESTATION_VERSION,
        consent_policy_version=policy.version,
        consent_channels=list(policy.channels),
        consent_authority_available=policy.max_evidence_age is not None,
        preview_ttl_minutes=int(policy.preview_ttl.total_seconds() // 60),
        max_bytes=MAX_BYTES,
        max_rows=MAX_ROWS,
        required_columns=list(REQUIRED_COLUMNS),
        optional_columns=list(OPTIONAL_COLUMNS),
    )


@router.post("/imports/csv/preview", response_model=CsvPreviewResponse)
async def preview_csv_upload(
    request: Request,
    context: StaffContext = STAFF_CONTEXT,
) -> CsvPreviewResponse:
    """Authorized metadata-only preview of one uploaded CSV."""
    _require_csv_import_enabled()
    data, fields, disposed_at = await _read_csv_multipart(
        request, expected_fields=_CSV_PREVIEW_FIELDS
    )
    source_system, export_at = _parse_source_fields(fields)

    from src.clinic_recall.sync import CsvSyncSource
    from src.clinic_recall.sync.csv_import import CsvImportError, preview_csv_import

    materialization = CsvSyncSource.materialize(data)
    del data
    if await request.is_disconnected():
        raise HTTPException(status_code=400, detail="request_disconnected")
    policy = get_csv_import_policy()
    now = datetime.now(UTC)
    with get_sessionmaker()() as session:
        try:
            result = preview_csv_import(
                session,
                context.clinic_id,
                materialization=materialization,
                source_system=source_system,
                export_at=export_at,
                actor=context.actor,
                now=now,
                policy=policy,
                upload_disposed_at=disposed_at,
            )
        except CsvImportError as exc:
            session.rollback()
            raise _csv_import_http_error(exc) from exc
        except sa.exc.IntegrityError as exc:
            session.rollback()
            logger.warning("csv import preview conflict")
            raise HTTPException(status_code=409, detail="import_conflict") from exc
        except Exception as exc:
            session.rollback()
            logger.warning("csv import preview failed")
            raise HTTPException(status_code=500, detail="csv_import_failed") from exc
        session.commit()
        batch = _batch_record(result.batch)
    return CsvPreviewResponse(
        batch=batch,
        importable=(
            batch.state == ImportBatchState.PREVIEW_VALID.value
            and batch.error_count == 0
        ),
        errors=[
            CsvPreviewErrorRecord(
                reason=error.reason.value,
                field=error.field,
                record=error.record,
                line=error.line,
            )
            for error in result.errors
        ],
    )


@router.post("/imports/csv/{batch_id}/approve", response_model=CsvApproveResponse)
async def approve_csv_upload(
    batch_id: str,
    request: Request,
    context: StaffContext = STAFF_CONTEXT,
) -> CsvApproveResponse:
    """Explicit same-bytes re-upload approval; the one import command."""
    _require_csv_import_enabled()
    data, fields, disposed_at = await _read_csv_multipart(
        request, expected_fields=_CSV_APPROVAL_FIELDS
    )
    source_system, export_at = _parse_source_fields(fields)
    attested_channels = tuple(
        channel.strip()
        for channel in (fields.get("attested_channels") or "").split(",")
        if channel.strip()
    )
    confirm = (fields.get("confirm_clinic_authority") or "").strip().lower() == "true"

    from src.clinic_recall.sync import CsvSyncSource
    from src.clinic_recall.sync.csv_import import (
        CsvImportAttestation,
        CsvImportError,
        approve_csv_import,
    )

    materialization = CsvSyncSource.materialize(data)
    del data
    if await request.is_disconnected():
        raise HTTPException(status_code=400, detail="request_disconnected")
    attestation = CsvImportAttestation(
        source_system=source_system,
        export_at=export_at,
        attestation_version=(fields.get("attestation_version") or "").strip(),
        attested_channels=attested_channels,
        confirm_clinic_authority=confirm,
    )
    policy = get_csv_import_policy()
    now = datetime.now(UTC)
    with get_sessionmaker()() as session:
        try:
            result = approve_csv_import(
                session,
                context.clinic_id,
                batch_id,
                materialization=materialization,
                attestation=attestation,
                actor=context.actor,
                now=now,
                policy=policy,
                keyring=get_rights_subject_keyring(),
                upload_disposed_at=disposed_at,
            )
        except CsvImportError as exc:
            session.rollback()
            raise _csv_import_http_error(exc) from exc
        except sa.exc.IntegrityError as exc:
            session.rollback()
            logger.warning("csv import approval conflict")
            raise HTTPException(status_code=409, detail="import_conflict") from exc
        except Exception as exc:
            session.rollback()
            logger.warning("csv import approval failed")
            raise HTTPException(status_code=500, detail="csv_import_failed") from exc
        session.commit()
        if not result.replayed:
            _run_post_import_matching(session, context, batch_id, materialization, now)
        batch_record = _batch_record(result.batch)
    return CsvApproveResponse(batch=batch_record, replayed=result.replayed)


def _csv_match_candidates(clinic_id: str, source_refs: tuple[str, ...]):
    """Materialize provider candidates for matching; ``None`` = unavailable.

    The runtime has no qualified Cliniko read authority (PR-05 sandbox gate),
    so this default always reports the provider unavailable. Tests inject
    synthetic snapshots by patching this hook.
    """
    return None


def _csv_match_candidates_for_review(
    clinic_id: str, provider: Any, source_ref: str
):
    """Refresh one review's candidates outside database scope.

    Runtime remains unavailable until the separately approved PR-05 sandbox
    read qualification is supplied. Tests inject synthetic snapshots.
    """
    return None


def _run_post_import_matching(
    session: Any,
    context: StaffContext,
    batch_id: str,
    materialization: Any,
    now: datetime,
) -> None:
    """Optional read-only matching pass after the committed import."""
    if not csv_matching_enabled():
        return
    from src.clinic_recall.enums import SourceSystem
    from src.clinic_recall.sync.csv_matching import run_source_matching

    refs = tuple(patient.source_ref for patient in materialization.patients)
    try:
        candidates = _csv_match_candidates(context.clinic_id, refs)
        run_source_matching(
            session,
            context.clinic_id,
            batch_id,
            provider=SourceSystem.CLINIKO,
            patient_source_refs=refs,
            candidates_by_ref=candidates,
            keyring=get_rights_subject_keyring(),
            actor=context.actor,
            now=now,
            auto_link=csv_matching_enabled(),
        )
        session.commit()
    except Exception:  # matching never invalidates the completed import
        session.rollback()
        logger.warning("post-import source matching failed; reviews stay pending")


@router.get("/imports/csv", response_model=CsvImportHistoryResponse)
def list_csv_imports(context: StaffContext = STAFF_CONTEXT) -> CsvImportHistoryResponse:
    from src.clinic_recall.sync.csv_import import list_import_batches

    with get_sessionmaker()() as session:
        batches = list_import_batches(session, context.clinic_id)
        return CsvImportHistoryResponse(batches=[_batch_record(batch) for batch in batches])


@router.get("/imports/csv/{batch_id}", response_model=CsvImportBatchRecord)
def get_csv_import(batch_id: str, context: StaffContext = STAFF_CONTEXT) -> CsvImportBatchRecord:
    from src.clinic_recall.sync.csv_import import get_import_batch

    with get_sessionmaker()() as session:
        batch = get_import_batch(session, context.clinic_id, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch_not_found")
        return _batch_record(batch)


def _match_record(review: Any) -> ImportMatchReviewRecord:
    return ImportMatchReviewRecord(
        id=review.id,
        import_batch_id=review.import_batch_id,
        provider=review.provider.value,
        strategy=review.strategy.value,
        strategy_version=review.strategy_version,
        state=review.state.value,
        candidate_count=review.candidate_count,
        reason=review.reason,
        resolved_by=review.resolved_by,
        resolved_at=_aware(review.resolved_at) if review.resolved_at else None,
        created_at=_aware(review.created_at),
    )


def _match_review_source_context(
    clinic_id: str, review_id: str
) -> tuple[Any, str]:
    """Load minimum internal context, closing DB scope before provider reads."""
    from src.clinic_recall.sync.csv_matching import get_match_review

    with get_sessionmaker()() as session:
        review = get_match_review(session, clinic_id, review_id)
        if review is None:
            raise HTTPException(status_code=404, detail="review_not_found")
        with clinic_scope(session, clinic_id):
            patient = session.execute(
                tenant_select(Patient).where(Patient.id == review.patient_id)
            ).scalar_one_or_none()
            if patient is None:
                raise HTTPException(status_code=404, detail="review_not_found")
            return review.provider, patient.source_ref


@router.get("/operator/import-matches", response_model=ImportMatchListResponse)
def list_import_matches(context: StaffContext = STAFF_CONTEXT) -> ImportMatchListResponse:
    _require_operator(context)
    from src.clinic_recall.enums import ImportMatchReviewState
    from src.clinic_recall.sync.csv_matching import list_match_reviews

    with get_sessionmaker()() as session:
        reviews = list_match_reviews(session, context.clinic_id)
        records = [_match_record(review) for review in reviews]
    return ImportMatchListResponse(
        reviews=records,
        unmatched_count=sum(
            1 for r in records if r.state == ImportMatchReviewState.UNMATCHED.value
        ),
        ambiguous_count=sum(
            1 for r in records if r.state == ImportMatchReviewState.AMBIGUOUS.value
        ),
        pending_count=sum(
            1
            for r in records
            if r.state
            in {
                ImportMatchReviewState.PENDING.value,
                ImportMatchReviewState.NOT_RUN.value,
            }
        ),
    )


@router.post(
    "/operator/import-matches/{review_id}/refresh",
    response_model=ImportMatchCandidatesResponse,
)
def refresh_import_match_route(
    review_id: str,
    context: StaffContext = STAFF_CONTEXT,
) -> ImportMatchCandidatesResponse:
    """Refresh provider candidates and issue opaque, short-lived choices."""
    _require_operator(context)
    if not csv_matching_enabled():
        raise HTTPException(status_code=403, detail="source matching is not enabled")
    provider, source_ref = _match_review_source_context(context.clinic_id, review_id)
    candidates = _csv_match_candidates_for_review(
        context.clinic_id, provider, source_ref
    )
    if candidates is None:
        raise HTTPException(status_code=503, detail="provider_unavailable")

    from src.clinic_recall.rights import SubjectFrozenError
    from src.clinic_recall.sync.csv_matching import (
        SourceMatchError,
        issue_candidate_tokens,
        refresh_import_match,
    )

    now = datetime.now(UTC)
    keyring = get_rights_subject_keyring()
    with get_sessionmaker()() as session:
        try:
            review, exact = refresh_import_match(
                session,
                context.clinic_id,
                review_id,
                candidates=candidates,
                now=now,
            )
            options = issue_candidate_tokens(
                review, exact, keyring=keyring, now=now
            ) if exact else ()
        except SubjectFrozenError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="subject_frozen") from exc
        except SourceMatchError as exc:
            session.rollback()
            status = 404 if exc.reason == "review_not_found" else 409
            raise HTTPException(status_code=status, detail=exc.reason) from exc
        session.commit()
        return ImportMatchCandidatesResponse(
            review=_match_record(review),
            candidates=[
                ImportMatchCandidateRecord(
                    token=option.token,
                    ordinal=option.ordinal,
                    active=option.active,
                    expires_at=option.expires_at,
                )
                for option in options
            ],
        )


@router.post(
    "/operator/import-matches/{review_id}/resolve",
    response_model=ImportMatchReviewRecord,
)
def resolve_import_match_route(
    review_id: str,
    payload: ResolveImportMatchRequest,
    context: StaffContext = STAFF_CONTEXT,
) -> ImportMatchReviewRecord:
    _require_operator(context)
    from src.clinic_recall.rights import SubjectFrozenError
    from src.clinic_recall.sync.csv_matching import (
        SourceMatchError,
        refresh_import_match,
        resolve_import_match,
    )

    if payload.action == "link" and not csv_matching_enabled():
        raise HTTPException(status_code=403, detail="source matching is not enabled")
    candidates = None
    if payload.action == "link":
        provider, source_ref = _match_review_source_context(
            context.clinic_id, review_id
        )
        candidates = _csv_match_candidates_for_review(
            context.clinic_id, provider, source_ref
        )
        if candidates is None:
            raise HTTPException(status_code=503, detail="provider_unavailable")
    with get_sessionmaker()() as session:
        try:
            if payload.action == "link":
                _, candidates = refresh_import_match(
                    session,
                    context.clinic_id,
                    review_id,
                    candidates=candidates or (),
                    now=datetime.now(UTC),
                )
            review = resolve_import_match(
                session,
                context.clinic_id,
                review_id,
                action=payload.action,
                keyring=get_rights_subject_keyring(),
                actor=context.actor,
                now=datetime.now(UTC),
                candidate_token=payload.candidate_token,
                candidates=candidates,
            )
        except SubjectFrozenError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="subject_frozen") from exc
        except SourceMatchError as exc:
            session.rollback()
            status = 404 if exc.reason == "review_not_found" else 409
            raise HTTPException(status_code=status, detail=exc.reason) from exc
        session.commit()
        return _match_record(review)