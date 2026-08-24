"""PR-08 source-link rights and deterministic matching tests.

Anti-rehydration invariants: erasure freezes every provider alias, alias
tombstones survive key rotation and patient deletion, and no matching path
can resurrect or reassociate an erased subject. Matching is exact-only with
zero/multiple candidates entering operator review.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from src.clinic_recall.enums import (
    ImportMatchReviewState,
    MatchStrategy,
    SourceLinkState,
    SourceSystem,
)
from src.clinic_recall.models import (
    BookingAction,
    Campaign,
    Clinic,
    ExternalEffect,
    ImportMatchReview,
    Interaction,
    OutreachJob,
    Patient,
    PatientSourceLink,
    RightsAliasTombstone,
)
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectFrozenError,
    SubjectKey,
    SubjectKeyring,
    assert_source_writable,
    request_patient_erasure,
)
from src.clinic_recall.sync.csv_matching import (
    MATCH_STRATEGY_VERSION,
    ProviderPatientSnapshot,
    SourceMatchError,
    candidate_evidence_hash,
    create_patient_source_link,
    issue_candidate_tokens,
    list_match_reviews,
    resolve_import_match,
    run_source_matching,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

KEY_V1 = SubjectKey(version="tests-v1", secret=b"tests-only-secret-material-01")
KEY_V2 = SubjectKey(version="tests-v2", secret=b"tests-only-secret-material-02")
KEYRING = SubjectKeyring(current=KEY_V1)
ROTATED_KEYRING = SubjectKeyring(current=KEY_V2, previous=(KEY_V1,))

RIGHTS_POLICY = RightsPolicy(
    version="tests-rights-v1",
    approval_evidence_hash="c" * 64,
    request_due_after=timedelta(days=28),
)


def _add_clinic(session, clinic_id="clinic-pr08-a"):
    session.add(Clinic(id=clinic_id, name="PR08 Rights Clinic"))
    session.flush()
    return clinic_id


def _add_patient(session, clinic_id, *, patient_id="pat-1", source_ref="PAT-CSV-1"):
    session.add(
        Patient(
            id=patient_id,
            clinic_id=clinic_id,
            source_ref=source_ref,
            name="Test Patient Links",
            consent_flags={},
            opt_out_flags={},
        )
    )
    session.flush()
    return patient_id


def _link(session, clinic_id, patient_id, *, ref="CLK-REMOTE-1", batch=None):
    return create_patient_source_link(
        session,
        clinic_id,
        patient_id,
        provider=SourceSystem.CLINIKO,
        source_ref=ref,
        strategy=MatchStrategy.OPERATOR_RESOLVED,
        evidence_hash="e" * 64,
        actor="operator:test",
        now=NOW,
        keyring=KEYRING,
        import_batch_id=batch,
    )


def _erase(session, clinic_id, patient_id, keyring=KEYRING):
    return request_patient_erasure(
        session,
        clinic_id=clinic_id,
        patient_id=patient_id,
        confirm_token=f"ERASE {patient_id}",
        request_identity=f"tests:erase-{patient_id}",
        actor_role="staff",
        actor_reference="staff:test-alice",
        keyring=keyring,
        policy=RIGHTS_POLICY,
        now=NOW,
    )


def _count(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# --------------------------------------------------------------------------- #
# Alias freeze and anti-rehydration
# --------------------------------------------------------------------------- #
def test_erasure_freezes_aliases_and_blocks_sync_through_them(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    patient_id = _add_patient(sqlite_session, clinic_id)
    link = _link(sqlite_session, clinic_id, patient_id)
    sqlite_session.commit()

    _erase(sqlite_session, clinic_id, patient_id)
    sqlite_session.commit()

    refreshed = sqlite_session.get(PatientSourceLink, link.id)
    assert refreshed.state == SourceLinkState.FROZEN
    assert _count(sqlite_session, RightsAliasTombstone) == 1

    # Sync is blocked through the primary ref AND the alias ref.
    with pytest.raises(SubjectFrozenError):
        assert_source_writable(sqlite_session, clinic_id, "PAT-CSV-1", KEYRING)
    with pytest.raises(SubjectFrozenError):
        assert_source_writable(sqlite_session, clinic_id, "CLK-REMOTE-1", KEYRING)


def test_alias_tombstone_survives_key_rotation(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    patient_id = _add_patient(sqlite_session, clinic_id)
    _link(sqlite_session, clinic_id, patient_id)
    _erase(sqlite_session, clinic_id, patient_id)  # tombstoned under v1
    sqlite_session.commit()

    with pytest.raises(SubjectFrozenError):
        assert_source_writable(sqlite_session, clinic_id, "CLK-REMOTE-1", ROTATED_KEYRING)


def test_alias_tombstone_survives_patient_and_link_deletion(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    patient_id = _add_patient(sqlite_session, clinic_id)
    _link(sqlite_session, clinic_id, patient_id)
    _erase(sqlite_session, clinic_id, patient_id)
    sqlite_session.commit()

    # Simulate PR-10 finalization deleting the operational graph.
    sqlite_session.execute(delete(PatientSourceLink))
    sqlite_session.execute(delete(Patient).where(Patient.id == patient_id))
    sqlite_session.commit()

    assert _count(sqlite_session, RightsAliasTombstone) == 1
    with pytest.raises(SubjectFrozenError):
        assert_source_writable(sqlite_session, clinic_id, "CLK-REMOTE-1", KEYRING)


def test_erased_subject_cannot_receive_a_new_link(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    patient_id = _add_patient(sqlite_session, clinic_id)
    _erase(sqlite_session, clinic_id, patient_id)
    sqlite_session.commit()

    with pytest.raises(SubjectFrozenError):
        _link(sqlite_session, clinic_id, patient_id)
    sqlite_session.rollback()
    assert _count(sqlite_session, PatientSourceLink) == 0


def test_source_ref_cannot_link_two_patients(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    first = _add_patient(sqlite_session, clinic_id, patient_id="pat-1", source_ref="PAT-CSV-1")
    second = _add_patient(sqlite_session, clinic_id, patient_id="pat-2", source_ref="PAT-CSV-2")
    _link(sqlite_session, clinic_id, first, ref="CLK-SHARED")
    sqlite_session.commit()

    with pytest.raises(SourceMatchError) as excinfo:
        _link(sqlite_session, clinic_id, second, ref="CLK-SHARED")
    sqlite_session.rollback()
    assert excinfo.value.reason == "link_conflict"


def test_one_active_link_per_patient_and_provider(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    patient_id = _add_patient(sqlite_session, clinic_id)
    _link(sqlite_session, clinic_id, patient_id, ref="CLK-A")
    sqlite_session.commit()

    with pytest.raises(SourceMatchError) as excinfo:
        _link(sqlite_session, clinic_id, patient_id, ref="CLK-B")
    sqlite_session.rollback()
    assert excinfo.value.reason == "link_conflict"


# --------------------------------------------------------------------------- #
# Deterministic matching outcomes
# --------------------------------------------------------------------------- #
def _snapshot(ref):
    return ProviderPatientSnapshot(provider=SourceSystem.CLINIKO, source_ref=ref)


def test_matching_zero_one_multiple_outcomes(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    for index, ref in enumerate(("PAT-M-0", "PAT-M-1", "PAT-M-2")):
        _add_patient(sqlite_session, clinic_id, patient_id=f"pat-m-{index}", source_ref=ref)
    sqlite_session.commit()

    result = run_source_matching(
        sqlite_session,
        clinic_id,
        "impb-test-1",
        provider=SourceSystem.CLINIKO,
        patient_source_refs=("PAT-M-0", "PAT-M-1", "PAT-M-2"),
        candidates_by_ref={
            "PAT-M-0": (),
            "PAT-M-1": (_snapshot("PAT-M-1"),),
            "PAT-M-2": (_snapshot("PAT-M-2"), _snapshot("PAT-M-2")),
        },
        keyring=KEYRING,
        actor="operator:test",
        now=NOW,
        auto_link=True,
    )
    sqlite_session.commit()

    assert result.reviewed == 3
    assert result.unmatched == 1
    assert result.linked == 1
    assert result.ambiguous == 1
    states = {
        review.patient_id: review.state
        for review in sqlite_session.execute(select(ImportMatchReview)).scalars()
    }
    assert states["pat-m-0"] == ImportMatchReviewState.UNMATCHED
    assert states["pat-m-1"] == ImportMatchReviewState.LINKED
    assert states["pat-m-2"] == ImportMatchReviewState.AMBIGUOUS
    assert _count(sqlite_session, PatientSourceLink) == 1  # never first-match

    # Matching created no outreach/booking side effects.
    for model in (Campaign, OutreachJob, ExternalEffect, BookingAction, Interaction):
        assert _count(sqlite_session, model) == 0


def test_matching_default_off_leaves_exact_match_pending(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    _add_patient(sqlite_session, clinic_id, patient_id="pat-p", source_ref="PAT-P-1")
    sqlite_session.commit()

    result = run_source_matching(
        sqlite_session,
        clinic_id,
        "impb-test-2",
        provider=SourceSystem.CLINIKO,
        patient_source_refs=("PAT-P-1",),
        candidates_by_ref={"PAT-P-1": (_snapshot("PAT-P-1"),)},
        keyring=KEYRING,
        actor="operator:test",
        now=NOW,
        auto_link=False,
    )
    sqlite_session.commit()

    assert result.pending == 1
    assert result.linked == 0
    assert _count(sqlite_session, PatientSourceLink) == 0
    review = sqlite_session.execute(select(ImportMatchReview)).scalar_one()
    assert review.state == ImportMatchReviewState.PENDING
    assert review.reason == "auto_link_disabled"


def test_provider_unavailable_stays_pending_and_import_intact(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    _add_patient(sqlite_session, clinic_id, patient_id="pat-u", source_ref="PAT-U-1")
    sqlite_session.commit()

    result = run_source_matching(
        sqlite_session,
        clinic_id,
        "impb-test-3",
        provider=SourceSystem.CLINIKO,
        patient_source_refs=("PAT-U-1",),
        candidates_by_ref=None,  # provider unavailable
        keyring=KEYRING,
        actor="operator:test",
        now=NOW,
        auto_link=True,
    )
    sqlite_session.commit()

    assert result.pending == 1
    review = sqlite_session.execute(select(ImportMatchReview)).scalar_one()
    assert review.state == ImportMatchReviewState.PENDING
    assert review.reason == "provider_unavailable"
    assert _count(sqlite_session, Patient) == 1  # import untouched


def test_matching_erased_subject_records_failed_not_linked(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    _add_patient(sqlite_session, clinic_id, patient_id="pat-e", source_ref="PAT-E-1")
    _erase(sqlite_session, clinic_id, "pat-e")
    sqlite_session.commit()

    with pytest.raises(SubjectFrozenError):
        run_source_matching(
            sqlite_session,
            clinic_id,
            "impb-test-4",
            provider=SourceSystem.CLINIKO,
            patient_source_refs=("PAT-E-1",),
            candidates_by_ref={"PAT-E-1": (_snapshot("PAT-E-1"),)},
            keyring=KEYRING,
            actor="operator:test",
            now=NOW,
            auto_link=True,
        )
    sqlite_session.rollback()
    assert _count(sqlite_session, PatientSourceLink) == 0


# --------------------------------------------------------------------------- #
# Operator resolution
# --------------------------------------------------------------------------- #
def _pending_review(session, clinic_id, *, ref="PAT-R-1", patient_id="pat-r"):
    _add_patient(session, clinic_id, patient_id=patient_id, source_ref=ref)
    session.commit()
    run_source_matching(
        session,
        clinic_id,
        "impb-test-r",
        provider=SourceSystem.CLINIKO,
        patient_source_refs=(ref,),
        candidates_by_ref={ref: (_snapshot(ref),)},
        keyring=KEYRING,
        actor="operator:test",
        now=NOW,
        auto_link=False,
    )
    session.commit()
    return session.execute(select(ImportMatchReview)).scalar_one()


def test_operator_resolution_link_is_evidence_bound_and_idempotent(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    review = _pending_review(sqlite_session, clinic_id)
    candidates = (_snapshot("PAT-R-1"),)
    evidence = review.candidate_evidence_hash
    assert evidence == candidate_evidence_hash(candidates)
    option = issue_candidate_tokens(review, candidates, keyring=KEYRING, now=NOW)[0]
    assert "PAT-R-1" not in option.token

    # A tampered token is a bounded rejection.
    with pytest.raises(SourceMatchError) as excinfo:
        resolve_import_match(
            sqlite_session,
            clinic_id,
            review.id,
            action="link",
            keyring=KEYRING,
            actor="operator:test",
            now=NOW,
            candidate_token=option.token[:-1] + ("0" if option.token[-1] != "0" else "1"),
            candidates=candidates,
        )
    sqlite_session.rollback()
    assert excinfo.value.reason == "candidate_mismatch"

    resolved = resolve_import_match(
        sqlite_session,
        clinic_id,
        review.id,
        action="link",
        keyring=KEYRING,
        actor="operator:test",
        now=NOW,
        candidate_token=option.token,
        candidates=candidates,
    )
    sqlite_session.commit()
    assert resolved.state == ImportMatchReviewState.LINKED
    assert resolved.source_link_id is not None
    link = sqlite_session.get(PatientSourceLink, resolved.source_link_id)
    assert link.strategy == MatchStrategy.OPERATOR_RESOLVED
    assert link.strategy_version == MATCH_STRATEGY_VERSION

    # Idempotent replay; conflicting dismiss is bounded.
    replay = resolve_import_match(
        sqlite_session,
        clinic_id,
        review.id,
        action="link",
        keyring=KEYRING,
        actor="operator:test",
        now=NOW,
    )
    assert replay.state == ImportMatchReviewState.LINKED
    assert _count(sqlite_session, PatientSourceLink) == 1
    with pytest.raises(SourceMatchError) as conflict:
        resolve_import_match(
            sqlite_session,
            clinic_id,
            review.id,
            action="dismiss",
            keyring=KEYRING,
            actor="operator:test",
            now=NOW,
        )
    assert conflict.value.reason == "review_already_resolved"


def test_operator_cannot_link_reference_outside_reviewed_candidates(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    review = _pending_review(sqlite_session, clinic_id)
    reviewed = (_snapshot("PAT-R-1"),)
    option = issue_candidate_tokens(review, reviewed, keyring=KEYRING, now=NOW)[0]

    with pytest.raises(SourceMatchError) as excinfo:
        resolve_import_match(
            sqlite_session,
            clinic_id,
            review.id,
            action="link",
            keyring=KEYRING,
            actor="operator:test",
            now=NOW,
            candidate_token=option.token,
            candidates=(_snapshot("CLK-NOT-IN-REVIEWED-SNAPSHOT"),),
        )

    assert excinfo.value.reason == "candidate_mismatch"
    assert _count(sqlite_session, PatientSourceLink) == 0


def test_candidate_token_expires_and_is_bound_to_review(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    review = _pending_review(sqlite_session, clinic_id)
    candidates = (_snapshot("PAT-R-1"),)
    option = issue_candidate_tokens(review, candidates, keyring=KEYRING, now=NOW)[0]

    with pytest.raises(SourceMatchError) as expired:
        resolve_import_match(
            sqlite_session,
            clinic_id,
            review.id,
            action="link",
            keyring=KEYRING,
            actor="operator:test",
            now=option.expires_at + timedelta(seconds=1),
            candidate_token=option.token,
            candidates=candidates,
        )
    assert expired.value.reason == "candidate_mismatch"
    assert _count(sqlite_session, PatientSourceLink) == 0

    with pytest.raises(SourceMatchError) as malformed:
        resolve_import_match(
            sqlite_session,
            clinic_id,
            review.id,
            action="link",
            keyring=KEYRING,
            actor="operator:test",
            now=NOW,
            candidate_token="not-base64." + "!" * 64,
            candidates=candidates,
        )
    assert malformed.value.reason == "candidate_mismatch"


def test_candidate_token_cannot_cross_reviews(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    first = _pending_review(sqlite_session, clinic_id)
    candidates = (_snapshot("PAT-R-1"),)
    token = issue_candidate_tokens(first, candidates, keyring=KEYRING, now=NOW)[0].token

    _add_patient(
        sqlite_session,
        clinic_id,
        patient_id="pat-r-2",
        source_ref="PAT-R-2",
    )
    run_source_matching(
        sqlite_session,
        clinic_id,
        "impb-test-r-2",
        provider=SourceSystem.CLINIKO,
        patient_source_refs=("PAT-R-2",),
        candidates_by_ref={"PAT-R-2": (_snapshot("PAT-R-2"),)},
        keyring=KEYRING,
        actor="operator:test",
        now=NOW,
        auto_link=False,
    )
    sqlite_session.commit()
    second = sqlite_session.execute(
        select(ImportMatchReview).where(ImportMatchReview.patient_id == "pat-r-2")
    ).scalar_one()

    with pytest.raises(SourceMatchError) as cross_review:
        resolve_import_match(
            sqlite_session,
            clinic_id,
            second.id,
            action="link",
            keyring=KEYRING,
            actor="operator:test",
            now=NOW,
            candidate_token=token,
            candidates=(_snapshot("PAT-R-2"),),
        )
    assert cross_review.value.reason == "candidate_mismatch"
    assert _count(sqlite_session, PatientSourceLink) == 0


def test_operator_dismiss_and_cross_tenant_denial(sqlite_session):
    clinic_a = _add_clinic(sqlite_session, "clinic-pr08-a")
    clinic_b = _add_clinic(sqlite_session, "clinic-pr08-b")
    review = _pending_review(sqlite_session, clinic_a)

    with pytest.raises(SourceMatchError) as excinfo:
        resolve_import_match(
            sqlite_session,
            clinic_b,
            review.id,
            action="dismiss",
            keyring=KEYRING,
            actor="operator:test",
            now=NOW,
        )
    assert excinfo.value.reason == "review_not_found"

    dismissed = resolve_import_match(
        sqlite_session,
        clinic_a,
        review.id,
        action="dismiss",
        keyring=KEYRING,
        actor="operator:test",
        now=NOW,
    )
    sqlite_session.commit()
    assert dismissed.state == ImportMatchReviewState.DISMISSED
    assert _count(sqlite_session, PatientSourceLink) == 0
    queue = list_match_reviews(sqlite_session, clinic_a)
    assert [review.id for review in queue] == [dismissed.id]
