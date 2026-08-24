"""Tests for anonymous incident reporting (Loop G).

Covers the hard anonymity invariants, per-clinic isolation, the deterministic
SMS ``REPORT`` keyword flow, and the governance status workflow.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from src.clinic_recall.enums import (
    CampaignStatus,
    CampaignType,
    Channel,
    IncidentCategory,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
    OutreachState,
)
from src.clinic_recall.incidents import (
    SMS_REPORT_CONFIRMATION,
    SMS_REPORT_INSTRUCTIONS,
    create_incident,
    handle_sms_incident_report,
    list_incidents,
    parse_sms_incident_report,
    update_incident_status,
)
from src.clinic_recall.models import (
    AuditLog,
    Base,
    Campaign,
    Clinic,
    IncidentReport,
    OutreachJob,
    Patient,
)
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectFrozenError,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)

NOW = datetime(2026, 7, 3, 14, 37, 22, tzinfo=UTC)


@pytest.fixture
def session():
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(Clinic(id="clinic-a", name="Clinic A", timezone="Europe/London"))
    db.add(Clinic(id="clinic-b", name="Clinic B", timezone="Europe/London"))
    db.flush()
    yield db
    db.close()


class TestAnonymityInvariants:
    def test_schema_has_no_identifier_columns(self):
        """The table must not be able to store reporter/patient identity."""
        columns = {column.name for column in IncidentReport.__table__.columns}
        forbidden = {
            "patient_id",
            "phone",
            "phone_number",
            "from_number",
            "from_number_hash",
            "reporter",
            "reporter_id",
            "actor",
            "email",
            "ip",
            "ip_address",
            "name",
        }
        assert not columns & forbidden
        assert columns == {
            "id",
            "clinic_id",
            "source",
            "category",
            "severity",
            "description",
            "related_job_id",
            "status",
            "occurred_hour",
            "reviewed_at",
            "created_at",
            "updated_at",
        }

    def test_occurrence_time_is_coarsened_to_hour(self, session):
        report = create_incident(
            session,
            "clinic-a",
            source=IncidentSource.PATIENT,
            description="The agent kept calling after I asked it to stop.",
            now=NOW,
        )
        assert report.occurred_hour.minute == 0
        assert report.occurred_hour.second == 0
        assert report.occurred_hour.hour == NOW.hour

    def test_patient_reports_cannot_reference_a_job(self, session):
        with pytest.raises(ValueError, match="must not reference a job"):
            create_incident(
                session,
                "clinic-a",
                source=IncidentSource.PATIENT,
                description="text",
                related_job_id="job-1",
                now=NOW,
            )

    def test_audit_actor_is_generic(self, session):
        create_incident(
            session,
            "clinic-a",
            source=IncidentSource.STAFF,
            description="Wrong patient contacted for recall.",
            category=IncidentCategory.WRONG_PATIENT_CONTACTED,
            severity=IncidentSeverity.LOW,
            now=NOW,
        )
        audit = session.execute(select(AuditLog)).scalars().one()
        assert audit.actor == "system:incident-reporting"
        # Hash-only payload; the description never reaches the audit log.
        assert audit.payload_hash and "Wrong patient" not in (audit.payload_hash or "")

    def test_staff_report_cannot_link_frozen_outreach_job(self, session):
        session.add(
            Patient(
                id="patient-incident",
                clinic_id="clinic-a",
                source_ref="P-INCIDENT",
                name="Synthetic Incident Patient",
            )
        )
        session.add(
            Campaign(
                id="campaign-incident",
                clinic_id="clinic-a",
                type=CampaignType.RECOVERY,
                status=CampaignStatus.ACTIVE,
            )
        )
        session.flush()
        session.add(
            OutreachJob(
                id="job-incident",
                clinic_id="clinic-a",
                campaign_id="campaign-incident",
                patient_id="patient-incident",
                channel=Channel.SMS,
                state=OutreachState.SENT,
            )
        )
        session.flush()
        request_patient_erasure(
            session,
            clinic_id="clinic-a",
            patient_id="patient-incident",
            confirm_token="ERASE patient-incident",
            request_identity="tests-incident-freeze",
            actor_role="dpo",
            actor_reference="tests-incident-operator",
            keyring=SubjectKeyring(
                current=SubjectKey("tests-incident-v1", b"tests-incident-freeze-key")
            ),
            policy=RightsPolicy("tests-incident-policy-v1", "a" * 64, timedelta(days=28)),
            now=NOW,
        )

        with pytest.raises(SubjectFrozenError, match="subject_frozen"):
            create_incident(
                session,
                "clinic-a",
                source=IncidentSource.STAFF,
                description="must not persist after freeze",
                related_job_id="job-incident",
                now=NOW,
            )

        assert session.execute(select(IncidentReport)).scalars().all() == []


class TestClinicIsolation:
    def test_listing_is_scoped_to_clinic(self, session):
        create_incident(
            session, "clinic-a", source=IncidentSource.STAFF, description="a", now=NOW
        )
        create_incident(
            session, "clinic-b", source=IncidentSource.STAFF, description="b", now=NOW
        )
        items_a = list_incidents(session, "clinic-a")
        assert [item.clinic_id for item in items_a] == ["clinic-a"]

    def test_status_update_rejects_cross_clinic_access(self, session):
        report = create_incident(
            session, "clinic-a", source=IncidentSource.STAFF, description="a", now=NOW
        )
        with pytest.raises(LookupError):
            update_incident_status(
                session, "clinic-b", report.id, status=IncidentStatus.UNDER_REVIEW, now=NOW
            )


class TestStatusWorkflow:
    def test_valid_transitions(self, session):
        report = create_incident(
            session, "clinic-a", source=IncidentSource.STAFF, description="a", now=NOW
        )
        update_incident_status(
            session, "clinic-a", report.id, status=IncidentStatus.UNDER_REVIEW, now=NOW
        )
        update_incident_status(
            session, "clinic-a", report.id, status=IncidentStatus.ACTIONED, now=NOW
        )
        final = update_incident_status(
            session, "clinic-a", report.id, status=IncidentStatus.CLOSED, now=NOW
        )
        assert final.status is IncidentStatus.CLOSED
        assert final.reviewed_at is not None

    def test_invalid_transition_fails_closed(self, session):
        report = create_incident(
            session, "clinic-a", source=IncidentSource.STAFF, description="a", now=NOW
        )
        with pytest.raises(ValueError, match="invalid incident transition"):
            update_incident_status(
                session, "clinic-a", report.id, status=IncidentStatus.ACTIONED, now=NOW
            )


class TestSmsReportFlow:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("REPORT the agent gave out my details", "the agent gave out my details"),
            ("report: something happened", "something happened"),
            ("Report", ""),
            ("REPORT  ", ""),
            ("Please report me to reception", None),
            ("YES", None),
            ("", None),
        ],
    )
    def test_parse_keyword(self, body, expected):
        assert parse_sms_incident_report(body) == expected

    def test_bare_keyword_returns_instructions_without_a_record(self, session):
        reply = handle_sms_incident_report(session, "clinic-a", description="", now=NOW)
        assert reply == SMS_REPORT_INSTRUCTIONS
        assert session.execute(select(IncidentReport)).scalars().all() == []

    def test_report_creates_anonymous_record(self, session):
        reply = handle_sms_incident_report(
            session,
            "clinic-a",
            description="The message went to my ex-partner's phone.",
            now=NOW,
        )
        assert reply == SMS_REPORT_CONFIRMATION
        report = session.execute(select(IncidentReport)).scalars().one()
        assert report.source is IncidentSource.PATIENT
        assert report.related_job_id is None
        # Serialize the whole row: no phone-number-shaped value may appear.
        row = {c.name: getattr(report, c.name) for c in IncidentReport.__table__.columns}
        serialized = " ".join(str(v) for v in row.values())
        assert "+44" not in serialized
