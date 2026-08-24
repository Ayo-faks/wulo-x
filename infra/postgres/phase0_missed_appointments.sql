-- SUPERSEDED (Phase 1): this single-table Phase 0 spike schema has been
-- reconciled into the real clinic/patient/appointment tables by the Alembic
-- migration infra/postgres/migrations/versions/0002_reconcile_phase0_seed.py,
-- which migrates these rows and then drops this table. Kept as an archived
-- reference only; the live schema is owned by `alembic upgrade head`.

create table if not exists phase0_missed_appointments (
    appointment_id text primary key,
    clinic_id text not null,
    patient_ref text not null,
    patient_display_name text not null,
    patient_phone text not null,
    appointment_start timestamptz not null,
    status text not null check (status in ('missed', 'cancelled', 'overdue_followup')),
    recall_reason text not null,
    consent_to_contact boolean not null,
    created_at timestamptz not null default now()
);

insert into phase0_missed_appointments (
    appointment_id,
    clinic_id,
    patient_ref,
    patient_display_name,
    patient_phone,
    appointment_start,
    status,
    recall_reason,
    consent_to_contact
) values
    ('appt-phase0-001', 'clinic-phase0-uk', 'patient-phase0-001', 'Synthetic Patient 01', '+447700900001', '2026-06-15 09:00:00+00', 'missed', 'missed appointment', true),
    ('appt-phase0-002', 'clinic-phase0-uk', 'patient-phase0-002', 'Synthetic Patient 02', '+447700900002', '2026-06-15 10:30:00+00', 'missed', 'missed appointment', true),
    ('appt-phase0-003', 'clinic-phase0-uk', 'patient-phase0-003', 'Synthetic Patient 03', '+447700900003', '2026-06-16 11:00:00+00', 'cancelled', 'late cancellation', true),
    ('appt-phase0-004', 'clinic-phase0-uk', 'patient-phase0-004', 'Synthetic Patient 04', '+447700900004', '2026-06-16 14:00:00+00', 'overdue_followup', 'overdue follow-up', true),
    ('appt-phase0-005', 'clinic-phase0-uk', 'patient-phase0-005', 'Synthetic Patient 05', '+447700900005', '2026-06-17 08:45:00+00', 'missed', 'missed appointment', true),
    ('appt-phase0-006', 'clinic-phase0-uk', 'patient-phase0-006', 'Synthetic Patient 06', '+447700900006', '2026-06-17 12:15:00+00', 'missed', 'missed appointment', true),
    ('appt-phase0-007', 'clinic-phase0-uk', 'patient-phase0-007', 'Synthetic Patient 07', '+447700900007', '2026-06-18 09:45:00+00', 'cancelled', 'late cancellation', true),
    ('appt-phase0-008', 'clinic-phase0-uk', 'patient-phase0-008', 'Synthetic Patient 08', '+447700900008', '2026-06-18 15:30:00+00', 'overdue_followup', 'overdue follow-up', true),
    ('appt-phase0-009', 'clinic-phase0-uk', 'patient-phase0-009', 'Synthetic Patient 09', '+447700900009', '2026-06-19 10:00:00+00', 'missed', 'missed appointment', true),
    ('appt-phase0-010', 'clinic-phase0-uk', 'patient-phase0-010', 'Synthetic Patient 10', '+447700900010', '2026-06-19 13:30:00+00', 'missed', 'missed appointment', true)
on conflict (appointment_id) do update set
    recall_reason = excluded.recall_reason,
    consent_to_contact = excluded.consent_to_contact;

select count(*) as phase0_missed_appointment_count
from phase0_missed_appointments;