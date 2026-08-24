/**
 * E2E coverage for Clinic Recall Phase 4 surfaces.
 *
 * Backend calls are route-mocked so these tests prove UI wiring without a live
 * Postgres/backend. API isolation is covered by pytest endpoint tests.
 */

import { Buffer } from 'node:buffer';
import { test, expect } from '@playwright/test';
import { installApiMocks, buildScenariosResponse, BANKING } from './helpers/scenario-mocks.js';

async function installClinicRecallMocks(page) {
  const calls = [];
  let queueItems = [
    {
      item_id: 'escalation:urgent-1',
      kind: 'escalation',
      priority: 'high',
      reason: 'urgent',
      status: 'open',
      patient_name: 'Amina Patient',
      outreach_state: 'escalated',
      appointment_start: '2026-06-20T09:00:00Z',
      slot_start: null,
      booking_type: null,
      context_summary: 'call inbound routed_to_staff urgent',
      created_at: '2026-06-27T12:00:00Z',
      severity: 'critical',
      delivery_state: 'queued',
      queued_at: '2026-06-27T12:00:00Z',
      due_at: '2026-06-27T12:05:00Z',
      overdue: true,
      acknowledged_at: null,
      acknowledged_by: null,
      alternate_requested: true,
      owner_resolved: false,
    },
    {
      item_id: 'booking_action:pending-1',
      kind: 'booking_action',
      priority: 'normal',
      reason: 'pending_booking_approval',
      status: 'pending',
      patient_name: 'Bayo Patient',
      outreach_state: 'escalated',
      appointment_start: '2026-06-20T09:00:00Z',
      slot_start: '2026-06-28T13:00:00Z',
      booking_type: 'book',
      context_summary: 'pending book approval from escalated',
      created_at: '2026-06-27T12:05:00Z',
      severity: 'normal',
      delivery_state: 'sent',
      queued_at: '2026-06-27T12:05:00Z',
      due_at: '2026-06-27T16:05:00Z',
      overdue: false,
      acknowledged_at: null,
      acknowledged_by: null,
      alternate_requested: false,
      owner_resolved: false,
    },
  ];
  let onboardingState = {
    status: 'pending',
    onboarding_required: true,
    onboarding_step: 'connect_data',
    onboarding_steps: {
      connect_data: false,
      confirm_number: false,
      choose_script: false,
      set_rules: false,
      first_campaign: false,
    },
    outreach_enabled: false,
  };
  let promptProposals = [];
  let csvBatches = [];
  let matchReviews = [];
  const csvConfig = {
    enabled: true,
    matching_enabled: true,
    schema_version: 'wulo-csv-v1',
    source_systems: ['csv', 'cliniko'],
    attestation_statement: 'I confirm this file was exported from the selected clinic system at the stated time, that the clinic is the data controller for every record in it, and that any consent values it carries were collected by the clinic.',
    attestation_version: 'csv-attest-v1',
    consent_policy_version: 'csv-consent-v0-unapproved',
    consent_channels: ['sms', 'email', 'call'],
    consent_authority_available: false,
    preview_ttl_minutes: 30,
    max_bytes: 52428800,
    max_rows: 200000,
    required_columns: ['appointment_source_ref', 'patient_source_ref', 'patient_name', 'status', 'start_at'],
    optional_columns: ['patient_phone', 'patient_email', 'value', 'consent_sms', 'consent_email', 'consent_call', 'opt_out_sms', 'opt_out_email', 'opt_out_call'],
  };
  let pilotProgrammes = [{
    id: 'pilot-r1', environment: 'production', release_identity: 'sha256:test-r1',
    state: 'active', maximum_participants: 50, active_cumulative_limit: 5,
    participant_count: 15, released_count: 5, pause_reason: null,
  }];

  await page.route('**/api/v1/clinic-recall/inbox', async (route) => {
    calls.push({ type: 'inbox', url: route.request().url() });
    await route.fulfill({ json: { items: queueItems } });
  });

  await page.route('**/api/v1/clinic-recall/imports/csv/config', async (route) => {
    calls.push({ type: 'csv-config', url: route.request().url() });
    await route.fulfill({ json: csvConfig });
  });

  await page.route('**/api/v1/clinic-recall/imports/csv/preview', async (route) => {
    const body = route.request().postData() || '';
    calls.push({
      type: 'csv-preview',
      url: route.request().url(),
      hasFilePart: body.includes('name="file"'),
      hasClinicId: body.includes('name="clinic_id"'),
    });
    const invalid = body.includes('BADROW');
    const batch = {
      id: `impb-e2e-${csvBatches.length + 1}`,
      state: invalid ? 'preview_invalid' : 'preview_valid',
      file_sha256: 'e'.repeat(64),
      schema_version: 'wulo-csv-v1',
      source_system: 'csv',
      export_at: '2026-07-25T18:00:00+00:00',
      total_rows: 4,
      valid_row_count: invalid ? 2 : 4,
      invalid_row_count: invalid ? 2 : 0,
      patient_count: 3,
      appointment_count: 4,
      error_count: invalid ? 2 : 0,
      error_reason_counts: invalid ? { invalid_phone: 1, missing_value: 1 } : null,
      patients_inserted: 0,
      patients_updated: 0,
      appointments_inserted: 0,
      appointments_updated: 0,
      consent_granted_count: 0,
      consent_unknown_count: 0,
      opt_out_count: 0,
      consent_authority_granted: false,
      preview_expires_at: '2026-07-26T23:59:00+00:00',
      approved_at: null,
      completed_at: null,
      created_at: '2026-07-26T12:00:00+00:00',
    };
    csvBatches = [batch, ...csvBatches];
    await route.fulfill({
      json: {
        batch,
        importable: !invalid,
        errors: invalid
          ? [
            { reason: 'invalid_phone', field: 'patient_phone', record: 1, line: 2 },
            { reason: 'missing_value', field: 'patient_name', record: 2, line: 3 },
          ]
          : [],
      },
    });
  });

  await page.route('**/api/v1/clinic-recall/imports/csv/*/approve', async (route) => {
    const body = route.request().postData() || '';
    const batchId = route.request().url().split('/imports/csv/')[1].split('/approve')[0];
    calls.push({
      type: 'csv-approve',
      url: route.request().url(),
      batchId,
      hasFilePart: body.includes('name="file"'),
      hasClinicId: body.includes('name="clinic_id"'),
      attested: body.includes('name="confirm_clinic_authority"') && body.includes('true'),
    });
    if (body.includes('MISMATCH')) {
      await route.fulfill({ status: 409, json: { detail: 'file_hash_mismatch' } });
      return;
    }
    const batch = csvBatches.find((item) => item.id === batchId);
    const completed = {
      ...(batch || {}),
      id: batchId,
      state: 'completed',
      patients_inserted: 3,
      appointments_inserted: 4,
      approved_at: '2026-07-26T12:05:00+00:00',
      completed_at: '2026-07-26T12:05:00+00:00',
    };
    csvBatches = csvBatches.map((item) => (item.id === batchId ? completed : item));
    onboardingState = {
      ...onboardingState,
      onboarding_step: 'confirm_number',
      onboarding_steps: { ...onboardingState.onboarding_steps, connect_data: true },
    };
    matchReviews = [
      {
        id: 'imr-e2e-1', import_batch_id: batchId, provider: 'cliniko',
        strategy: 'exact_source_ref', strategy_version: 'v1', state: 'unmatched', candidate_count: 0,
        reason: 'no_exact_match', resolved_by: null, resolved_at: null,
        created_at: '2026-07-26T12:05:00+00:00',
      },
      {
        id: 'imr-e2e-2', import_batch_id: batchId, provider: 'cliniko',
        strategy: 'exact_source_ref', strategy_version: 'v1', state: 'ambiguous', candidate_count: 2,
        reason: 'multiple_exact_matches', resolved_by: null,
        resolved_at: null, created_at: '2026-07-26T12:05:00+00:00',
      },
    ];
    await route.fulfill({ json: { batch: completed, replayed: false } });
  });

  await page.route('**/api/v1/clinic-recall/imports/csv', async (route) => {
    calls.push({ type: 'csv-history', url: route.request().url() });
    await route.fulfill({ json: { batches: csvBatches } });
  });

  await page.route('**/api/v1/clinic-recall/operator/import-matches', async (route) => {
    calls.push({ type: 'csv-matches', url: route.request().url() });
    await route.fulfill({
      json: {
        reviews: matchReviews,
        unmatched_count: matchReviews.filter((review) => review.state === 'unmatched').length,
        ambiguous_count: matchReviews.filter((review) => review.state === 'ambiguous').length,
        pending_count: matchReviews.filter((review) => ['pending', 'not_run'].includes(review.state)).length,
      },
    });
  });

  await page.route('**/api/v1/clinic-recall/operator/import-matches/*/refresh', async (route) => {
    const reviewId = route.request().url().split('/import-matches/')[1].split('/refresh')[0];
    const review = matchReviews.find((item) => item.id === reviewId);
    calls.push({ type: 'csv-match-refresh', url: route.request().url(), reviewId });
    const candidates = review?.state === 'ambiguous'
      ? [
        { token: `opaque-token-${reviewId}-1`, ordinal: 1, active: true, expires_at: '2026-07-26T12:15:00+00:00' },
        { token: `opaque-token-${reviewId}-2`, ordinal: 2, active: true, expires_at: '2026-07-26T12:15:00+00:00' },
      ]
      : [];
    await route.fulfill({ json: { review, candidates } });
  });

  await page.route('**/api/v1/clinic-recall/operator/import-matches/*/resolve', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    const reviewId = route.request().url().split('/import-matches/')[1].split('/resolve')[0];
    calls.push({ type: 'csv-match-resolve', url: route.request().url(), body, reviewId });
    matchReviews = matchReviews.map((review) => (review.id === reviewId
    ? {
      ...review,
      state: body.action === 'link' ? 'linked' : 'dismissed',
      resolved_by: 'operator:test',
      resolved_at: '2026-07-26T12:10:00+00:00',
    }
    : review));
    await route.fulfill({ json: matchReviews.find((review) => review.id === reviewId) });
  });

  await page.route('**/api/v1/clinic-recall/queue/**/resolve', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.push({ type: 'resolve', url: route.request().url(), body });
    queueItems = queueItems.filter((item) => !route.request().url().includes(encodeURIComponent(item.item_id)));
    await route.fulfill({ json: { resolved: true, booking_status: body.decision === 'approve' ? 'completed' : undefined, escalation_status: body.decision === 'reject' ? 'resolved' : undefined } });
  });

  await page.route('**/api/v1/clinic-recall/inbox/**/acknowledge', async (route) => {
    const url = route.request().url();
    const item = queueItems.find((candidate) => url.includes(encodeURIComponent(candidate.item_id)));
    calls.push({ type: 'acknowledge', url, itemId: item?.item_id });
    queueItems = queueItems.map((candidate) => candidate.item_id === item?.item_id
      ? { ...candidate, status: candidate.kind === 'escalation' ? 'acknowledged' : candidate.status, acknowledged_at: '2026-06-27T12:06:00Z', acknowledged_by: 'staff:test', overdue: false }
      : candidate);
    await route.fulfill({ json: {
      acknowledged: true,
      resolved: false,
      idempotent: false,
      booking_status: item?.kind === 'booking_action' ? 'pending' : undefined,
      escalation_status: item?.kind === 'escalation' ? 'acknowledged' : undefined,
    } });
  });

  await page.route('**/api/v1/clinic-recall/roi?**', async (route) => {
    calls.push({ type: 'roi', url: route.request().url() });
    await route.fulfill({
      json: {
        start: '2026-06-01T00:00:00Z',
        end: '2026-07-01T00:00:00Z',
        contacted: 12,
        rebooked: 5,
        conversion_rate: 0.4167,
        recovered_revenue: '420.00',
        no_show_delta: 0.2,
        opt_out_rate: 0.0833,
        monthly_net: '221.00',
        roi_multiple: '2.11',
        subscription_cost: '199.00',
        usage_cost: '0.00',
      },
    });
  });

  await page.route('**/api/v1/clinic-recall/roi.csv?**', async (route) => {
    calls.push({ type: 'csv', url: route.request().url() });
    await route.fulfill({
      contentType: 'text/csv',
      body: 'metric,value\ncontacted,12\nrebooked,5\n',
    });
  });

  await page.route('**/api/v1/clinic-recall/outbox?**', async (route) => {
    calls.push({ type: 'outbox', url: route.request().url() });
    await route.fulfill({ json: { items: [{ item_id: 'outbox-1', patient_name: 'Queued Patient', channel: 'sms', status: 'ready', preview: 'Reminder copy' }] } });
  });

  await page.route('**/api/v1/clinic-recall/interactions', async (route) => {
    calls.push({ type: 'interactions', url: route.request().url() });
    await route.fulfill({ json: { items: [{ id: 'sent-1', patient_name: 'Sent Patient', channel: 'sms', direction: 'outbound', occurred_at: '2026-06-27T12:10:00Z' }] } });
  });

  await page.route('**/api/v1/clinic-recall/campaign/settings', async (route) => {
    calls.push({ type: route.request().method() === 'PUT' ? 'settings-put' : 'settings-get', url: route.request().url(), body: route.request().postDataJSON?.() });
    await route.fulfill({
      json: {
        name: 'Clinic A',
        timezone: 'Europe/London',
        daily_caps: route.request().method() === 'PUT' ? 25 : 200,
        branding: { sms_sender: 'Clinic Recall' },
        contact_hours: { start_hour: 9, end_hour: 17 },
      },
    });
  });

  await page.route('**/api/v1/clinic-recall/campaigns', async (route) => {
    calls.push({ type: 'campaigns', url: route.request().url() });
    await route.fulfill({ json: { campaigns: [{ id: 'campaign-a', type: 'recovery', status: 'active', jobs: 7, is_launchable: true }] } });
  });

  await page.route('**/api/v1/clinic-recall/campaigns/launch', async (route) => {
    calls.push({ type: 'launch', url: route.request().url(), body: JSON.parse(route.request().postData() || '{}') });
    await route.fulfill({ json: { candidate_queue: { detected_total: 9, queued: 3, already_queued: 2, detected: { missed: 9 }, skipped: {} } } });
  });

  await page.route('**/api/v1/clinic-recall/onboarding', async (route) => {
    const body = route.request().method() === 'PUT' ? JSON.parse(route.request().postData() || '{}') : null;
    calls.push({ type: route.request().method() === 'PUT' ? 'onboarding-put' : 'onboarding-get', url: route.request().url(), body });
    if (body?.completed_step) {
      onboardingState = {
        ...onboardingState,
        onboarding_step: 'confirm_number',
        onboarding_steps: { ...onboardingState.onboarding_steps, [body.completed_step]: true },
      };
    }
    await route.fulfill({ json: onboardingState });
  });

  let inboundTasks = [
    {
      id: 'inbound-task-1',
      inbound_call_id: 'inbound-call-1',
      kind: 'callback',
      status: 'open',
      priority: 'normal',
      reason: 'callback',
      summary: 'Caller asked reception to call back',
      created_at: '2026-06-27T12:20:00Z',
      severity: 'normal', delivery_state: 'queued', queued_at: '2026-06-27T12:20:00Z', due_at: '2026-06-27T16:20:00Z', overdue: false, acknowledged_at: null, acknowledged_by: null, alternate_requested: false, owner_resolved: false,
    },
    {
      id: 'inbound-task-2',
      inbound_call_id: 'inbound-call-2',
      kind: 'escalation',
      status: 'open',
      priority: 'high',
      reason: 'clinical',
      summary: 'Clinical concern routed to staff',
      created_at: '2026-06-27T12:25:00Z',
      severity: 'high', delivery_state: 'sent', queued_at: '2026-06-27T12:25:00Z', due_at: '2026-06-27T12:40:00Z', overdue: true, acknowledged_at: null, acknowledged_by: null, alternate_requested: true, owner_resolved: false,
    },
    {
      id: 'inbound-task-3',
      inbound_message_id: 'inbound-message-1',
      source: 'sms',
      kind: 'booking_request',
      status: 'open',
      priority: 'normal',
      reason: 'booking_request',
      summary: 'Booking request from inbound SMS',
      created_at: '2026-06-27T12:28:00Z',
      severity: 'normal', delivery_state: 'queued', queued_at: '2026-06-27T12:28:00Z', due_at: '2026-06-27T16:28:00Z', overdue: false, acknowledged_at: null, acknowledged_by: null, alternate_requested: false, owner_resolved: false,
    },
  ];
  await page.route('**/api/v1/clinic-recall/monitor', async (route) => {
    calls.push({ type: 'monitor', url: route.request().url() });
    await route.fulfill({ json: {
      open_queue_count: 2,
      queued_outbox_count: 3,
      active_campaigns: 1,
      recent_interactions_count: 8,
      latest_escalation_at: '2026-06-27T12:00:00Z',
      voice_fallback_summary: { call_jobs_by_state: { no_reply: 1 }, latest_call_interaction_at: null },
    } });
  });

  await page.route('**/api/v1/clinic-recall/voice/fallback/run', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.push({ type: 'voice-fallback', url: route.request().url(), body });
    await route.fulfill({ json: { voice_fallback: { calls_initiated: 1, failed_calls: 0, idempotent_skips: 0, skipped: {} } } });
  });

  await page.route('**/api/v1/clinic-recall/signup', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.push({ type: 'signup', url: route.request().url(), body });
    await route.fulfill({ json: { clinic_id: 'clinic-new-smile-clinic', status: 'pending', onboarding_next: 'connect_data' } });
  });

  await page.route('**/api/v1/clinic-recall/operator/prompt-proposal', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.push({ type: 'prompt-proposal', url: route.request().url(), body });
    await route.fulfill({
      json: {
        prompt_path: '.agentops/prompts/recall-agent.prompt.md',
        gate_required: true,
        diff: '--- recall-agent.prompt.md\n+++ recall-agent.prompt.md (proposed)\n+Add calmer clinic tone.\n',
      },
    });
  });

  await page.route('**/api/v1/clinic-recall/phone-numbers', async (route) => {
    calls.push({ type: 'phone-numbers', url: route.request().url() });
    await route.fulfill({ json: { items: [
      { id: 'phone-1', provider: 'twilio', phone_number: '+15551230000', purpose: 'inbound', status: 'active', webhook_url: '/api/v1/voice/twilio/twiml', test_status: 'green' },
      { id: 'phone-2', provider: 'acs', phone_number: '+15551239999', purpose: 'inbound', status: 'inactive', webhook_url: '/api/v1/calls/event', test_status: 'not_tested' },
    ] } });
  });

  await page.route('**/api/v1/clinic-recall/inbound-calls', async (route) => {
    calls.push({ type: 'inbound-calls', url: route.request().url() });
    await route.fulfill({ json: { items: [
      { id: 'inbound-call-1', provider: 'twilio', provider_call_id: 'CA123', called_number: '+15551230000', caller_number_redacted: 'hash:abcd1234', status: 'started', outcome: null, created_at: '2026-06-27T12:20:00Z' },
    ] } });
  });

  await page.route('**/api/v1/clinic-recall/inbound-messages', async (route) => {
    calls.push({ type: 'inbound-messages', url: route.request().url() });
    await route.fulfill({ json: { items: [
      { id: 'inbound-message-1', provider: 'twilio', from_number_redacted: 'hash:efgh5678', intent: 'booking_request', status: 'routed', summary: 'Booking request from inbound SMS', created_at: '2026-06-27T12:28:00Z' },
    ] } });
  });

  await page.route('**/api/v1/clinic-recall/inbound-tasks', async (route) => {
    calls.push({ type: 'inbound-tasks', url: route.request().url() });
    await route.fulfill({ json: { items: inboundTasks } });
  });

  await page.route('**/api/v1/clinic-recall/inbound-tasks/*/resolve', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.push({ type: 'inbound-task-resolve', url: route.request().url(), body });
    const taskId = route.request().url().split('/inbound-tasks/')[1].split('/resolve')[0];
    inboundTasks = inboundTasks.map((task) => task.id === taskId ? { ...task, status: 'resolved' } : task).filter((task) => task.status !== 'resolved');
    await route.fulfill({ json: { id: taskId, inbound_call_id: 'inbound-call-1', source: 'call', kind: 'callback', status: 'resolved', priority: 'normal', reason: 'callback', summary: 'resolved', created_at: '2026-06-27T12:20:00Z' } });
  });

  await page.route('**/api/v1/clinic-recall/inbound-tasks/*/acknowledge', async (route) => {
    const taskId = route.request().url().split('/inbound-tasks/')[1].split('/acknowledge')[0];
    calls.push({ type: 'inbound-task-acknowledge', url: route.request().url(), taskId });
    inboundTasks = inboundTasks.map((task) => task.id === taskId
      ? { ...task, status: 'acknowledged', acknowledged_at: '2026-06-27T12:30:00Z', acknowledged_by: 'staff:test', overdue: false }
      : task);
    await route.fulfill({ json: inboundTasks.find((task) => task.id === taskId) });
  });

  await page.route('**/api/v1/clinic-recall/inbound-metrics', async (route) => {
    calls.push({ type: 'inbound-metrics', url: route.request().url() });
    await route.fulfill({ json: { calls_total: 12, calls_completed: 8, texts_total: 1, texts_routed: 1, open_tasks: inboundTasks.length, callbacks_open: inboundTasks.filter((task) => task.kind === 'callback').length, escalations_open: inboundTasks.filter((task) => task.kind === 'escalation').length, booking_requests_open: inboundTasks.filter((task) => task.kind === 'booking_request').length, text_callbacks_open: 0, text_escalations_open: 0, text_booking_requests_open: 1, text_identity_unclear_open: 0 } });
  });

  await page.route('**/api/v1/clinic-recall/inbound-config', async (route) => {
    const body = route.request().method() === 'PUT' ? JSON.parse(route.request().postData() || '{}') : null;
    calls.push({ type: route.request().method() === 'PUT' ? 'inbound-config-put' : 'inbound-config-get', url: route.request().url(), body });
    await route.fulfill({ json: body || { greeting: 'Hello, thanks for calling.', callback_sla_hours: 4, escalation_destination: 'Front desk', recording_enabled: false } });
  });

  await page.route('**/api/v1/clinic-recall/operator/prompt-proposals', async (route) => {
    const body = route.request().method() === 'POST' ? JSON.parse(route.request().postData() || '{}') : null;
    calls.push({ type: route.request().method() === 'POST' ? 'prompt-proposal' : 'prompt-proposals-get', url: route.request().url(), body });
    if (route.request().method() === 'POST') {
      const proposal = {
        id: 'proposal-1',
        actor: 'operator:test',
        status: 'submitted',
        proposed_prompt: body.proposed_prompt,
        gate_required: true,
        created_at: '2026-06-27T12:00:00Z',
        updated_at: '2026-06-27T12:00:00Z',
        diff: '--- recall-agent.prompt.md\n+++ recall-agent.prompt.md (proposed)\n+Add calmer clinic tone.\n',
      };
      promptProposals = [proposal, ...promptProposals];
      await route.fulfill({ json: proposal });
      return;
    }
    await route.fulfill({ json: { proposals: promptProposals } });
  });

  await page.route('**/api/v1/clinic-recall/operator/script-templates', async (route) => {
    const body = route.request().method() === 'PUT' ? JSON.parse(route.request().postData() || '{}') : null;
    calls.push({ type: route.request().method() === 'PUT' ? 'scripts-put' : 'scripts-get', url: route.request().url(), body });
    await route.fulfill({
      json: {
        templates: body?.templates || {
          missed: 'We missed you. Let us help you rebook.',
          overdue: 'You may be due for follow-up.',
          feedback: 'Thanks for visiting. Share feedback with the clinic.',
        },
      },
    });
  });

  await page.route('**/api/v1/clinic-recall/operator/voice-persona', async (route) => {
    const body = route.request().method() === 'PUT' ? JSON.parse(route.request().postData() || '{}') : null;
    calls.push({ type: route.request().method() === 'PUT' ? 'voice-persona-put' : 'voice-persona-get', url: route.request().url(), body });
    await route.fulfill({
      json: body || { display_name: 'Clinic Recall', tone: 'warm, concise, professional', voice_name: 'Ada' },
    });
  });

  await page.route('**/api/v1/clinic-recall/operator/pilot/programmes**', async (route) => {
    const method = route.request().method();
    const body = method === 'POST' ? JSON.parse(route.request().postData() || '{}') : null;
    const url = route.request().url();
    let type = 'pilot-get';
    if (url.endsWith('/release')) {
      type = 'pilot-release';
      if (body.cumulative_limit > pilotProgrammes[0].participant_count) {
        calls.push({ type, url, body });
        await route.fulfill({
          status: 409,
          json: { detail: 'cumulative release requires every preceding ordinal' },
        });
        return;
      }
      pilotProgrammes = pilotProgrammes.map((programme) => ({ ...programme, state: 'active', active_cumulative_limit: body.cumulative_limit, released_count: body.cumulative_limit }));
    } else if (url.endsWith('/dark')) {
      type = 'pilot-dark';
      pilotProgrammes = pilotProgrammes.map((programme) => ({ ...programme, state: 'dark' }));
    } else if (url.endsWith('/pause')) {
      type = 'pilot-pause';
      pilotProgrammes = pilotProgrammes.map((programme) => ({ ...programme, state: 'paused', pause_reason: body.reason }));
    } else if (url.endsWith('/close')) {
      type = 'pilot-close';
      pilotProgrammes = pilotProgrammes.map((programme) => ({ ...programme, state: 'closed', pause_reason: body.reason }));
    } else if (url.endsWith('/participants')) {
      type = 'pilot-enroll';
      pilotProgrammes = pilotProgrammes.map((programme) => ({ ...programme, participant_count: programme.participant_count + 1 }));
    } else if (method === 'POST') {
      type = 'pilot-create';
      pilotProgrammes = [{ ...body, id: body.programme_id, state: 'draft', maximum_participants: 50, active_cumulative_limit: 0, participant_count: 0, released_count: 0, pause_reason: null }];
    }
    calls.push({ type, url, body });
    await route.fulfill({ json: method === 'GET' ? { programmes: pilotProgrammes } : pilotProgrammes[0] });
  });

  return calls;
}

test.describe('Clinic Recall Surfaces', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => window.localStorage.clear());
  });

  test('renders landing at root without booting clinic data calls', async ({ page }) => {
    const calls = await installClinicRecallMocks(page);

    await page.goto('/', { waitUntil: 'domcontentloaded' });

    await expect(page.getByRole('heading', { name: /Recover missed appointments/ })).toBeVisible();
    await expect(page.getByRole('link', { name: /Open Clinic Recall/ })).toHaveAttribute('href', /\/app$/);
    await expect(page.getByRole('heading', { name: 'Escalation and pending booking queue' })).toHaveCount(0);
    expect(calls.length).toBe(0);
  });

  test('landing self-serve signup creates a pending clinic workspace', async ({ page }) => {
    const calls = await installClinicRecallMocks(page);

    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.getByLabel('Clinic name').fill('New Smile Clinic');
    await page.getByLabel('Work email').fill('hello@example.test');
    await page.getByRole('button', { name: 'Create sandbox clinic' }).click();

    await expect(page.getByText('Created clinic-new-smile-clinic. Status: pending; sandbox setup starts at connect_data.')).toBeVisible();
    const signup = calls.find((call) => call.type === 'signup');
    expect(signup.body).toEqual({ clinic_name: 'New Smile Clinic', contact_email: 'hello@example.test' });
  });

  test('renders queue, ROI, and campaign controls from scoped APIs', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    const calls = await installClinicRecallMocks(page);

    await page.goto('/app');

    await expect(page.getByRole('heading', { name: 'Escalation and pending booking queue' })).toBeVisible();
    await expect(page.getByRole('button', { name: /Setup/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Scripts - operator required/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Voice - operator required/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Agent - operator required/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Health - operator required/ })).toBeVisible();
    const shellFit = await page.evaluate(() => ({
      horizontalOverflowPx: document.documentElement.scrollWidth - window.innerWidth,
      railButtonsFit: [...document.querySelectorAll('.shell-rail button')]
        .every((button) => button.scrollWidth <= button.clientWidth + 1),
    }));
    expect(shellFit.horizontalOverflowPx).toBeLessThanOrEqual(0);
    expect(shellFit.railButtonsFit).toBe(true);
    await expect(page.getByRole('heading', { name: /Phone Assistant/ })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /Create Demo Profile/ })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /Sessions/ })).toHaveCount(0);
    await expect(page.getByText('Amina Patient')).toBeVisible();
    await expect(page.getByText('Bayo Patient')).toBeVisible();

    await page.getByRole('button', { name: /ROI/ }).click();
    await expect(page.getByText('Recovered revenue')).toBeVisible();
    await expect(page.getByText('£420.00')).toBeVisible();
    await expect(page.getByRole('link', { name: /CSV/ })).toHaveAttribute('href', /roi\.csv/);

    await page.getByRole('button', { name: /Outbox/ }).click();
    await expect(page.getByRole('heading', { name: 'Queued outbound review' })).toBeVisible();
    await expect(page.getByText('Queued Patient')).toBeVisible();

    await page.getByRole('button', { name: /Sent/ }).click();
    await expect(page.getByRole('heading', { name: 'Interaction timeline' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'sms · outbound' })).toBeVisible();

    expect(calls.filter((call) => call.url.includes('clinic_id=')).length).toBe(0);
  });

  test('renders Phone surface with provider state, redaction, and task resolution', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    const calls = await installClinicRecallMocks(page);

    await page.goto('/app');
    await page.getByRole('button', { name: /Phone/ }).click();

    await expect(page.getByRole('heading', { name: 'Inbound assistant' })).toBeVisible();
    await expect(page.getByRole('heading', { name: '+15551230000', exact: true })).toBeVisible();
    await expect(page.getByText('twilio · inbound')).toBeVisible();
    await expect(page.getByText('acs · inbound')).toBeVisible();
    await expect(page.getByText('hash:abcd1234')).toBeVisible();
    await expect(page.getByText('hash:efgh5678')).toBeVisible();
    await expect(page.getByText('Caller asked reception to call back')).toBeVisible();
    await expect(page.getByText('Clinical concern routed to staff')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'twilio · booking request' })).toBeVisible();
    await expect(page.getByText('Booking request from inbound SMS').first()).toBeVisible();

    await page.getByRole('button', { name: /Acknowledge/ }).first().click();
    await expect(page.getByText('Acknowledged inbound task inbound-task-1.')).toBeVisible();
    await expect(page.getByText('Caller asked reception to call back')).toBeVisible();
    await page.getByRole('button', { name: /Resolve/ }).first().click();
    await expect(page.getByText('Resolved inbound task inbound-task-1.')).toBeVisible();

    const endpointCalls = calls.filter((call) => call.type?.startsWith('inbound') || call.type === 'phone-numbers');
    expect(endpointCalls.length).toBeGreaterThanOrEqual(6);
    expect(endpointCalls.every((call) => !call.url.includes('clinic_id='))).toBeTruthy();
    expect(calls.some((call) => call.type === 'inbound-task-acknowledge' && call.taskId === 'inbound-task-1')).toBe(true);
    expect(calls.find((call) => call.type === 'inbound-task-resolve')?.body).toEqual({ status: 'resolved', reason: 'Resolved from Phone surface.' });
  });

  test('shows setup guidance and staff-safe locked control-room tools', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    const calls = await installClinicRecallMocks(page);

    await page.goto('/app');
    await page.getByRole('button', { name: /Setup/ }).click();
    await expect(page.getByRole('heading', { name: /Launch without configuration expertise/ })).toBeVisible();
    await expect(page.getByLabel('Onboarding status')).toContainText('Setup status: pending');
    // connect_data completes only from durable server-side import evidence.
    await expect(page.getByRole('button', { name: 'Mark Connect data complete' })).toHaveCount(0);
    await expect(page.getByText('Completes automatically after a successful CSV import below.')).toBeVisible();
    await page.getByRole('button', { name: 'Mark Confirm number complete' }).click();
    await expect(page.getByRole('button', { name: 'Complete' }).first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Connect data' })).toBeVisible();

    await page.getByRole('button', { name: /Monitor/ }).click();
    await expect(page.getByRole('heading', { name: 'Clinic status monitor' })).toBeVisible();
    await expect(page.getByLabel('Clinic status metrics')).toContainText('Open queue');
    await expect(page.getByText('no_reply: 1')).toBeVisible();

    await page.getByTestId('operator-tool-scripts').click();
    await expect(page.getByRole('heading', { name: 'Scripts/Templates' })).toBeVisible();
    await expect(page.getByLabel('Scripts/Templates access state')).toContainText('Operator review required');
    await expect(page.getByRole('button', { name: 'Save script templates' })).toHaveCount(0);

    await page.getByTestId('operator-tool-voice').click();
    await expect(page.getByRole('heading', { name: 'Voice persona' })).toBeVisible();
    await expect(page.getByLabel('Voice persona access state')).toContainText('Operator review required');
    await expect(page.getByRole('button', { name: 'Run voice fallback' })).toHaveCount(0);

    await page.getByTestId('operator-tool-agent').click();
    await expect(page.getByRole('heading', { name: 'Agent tuning' })).toBeVisible();
    await expect(page.getByLabel('Agent tuning access state')).toContainText('Operator review required');
    await expect(page.getByLabel('Proposed Recall Agent prompt')).toHaveCount(0);

    await page.getByTestId('operator-tool-system').click();
    await expect(page.getByRole('heading', { name: 'Health' })).toBeVisible();
    await expect(page.getByLabel('Health access state')).toContainText('Staff-safe view');

    expect(calls.some((call) => ['scripts-get', 'scripts-put', 'voice-persona-get', 'voice-persona-put', 'voice-fallback', 'prompt-proposal', 'prompt-proposals-get'].includes(call.type))).toBe(false);

  });

  test('operator agent panel generates a gated prompt diff without live edits', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    const calls = await installClinicRecallMocks(page);

    await page.goto('/app?role=operator');
    await page.getByTestId('operator-tool-agent').click();
    await page.getByLabel('Proposed Recall Agent prompt').fill('You are Clinic Recall. Add calmer clinic tone.');
    await page.getByRole('button', { name: 'Generate gated diff' }).click();

    await expect(page.getByLabel('Prompt proposal diff')).toContainText('Add calmer clinic tone');
    await expect(page.getByText('Prompt proposal submitted for AgentOps-gated review.')).toBeVisible();
    await expect(page.getByLabel('Submitted prompt proposals')).toContainText('submitted');
    await expect(page.getByText('AgentOps eval gate required before this ships.')).toBeVisible();
    const proposal = calls.find((call) => call.type === 'prompt-proposal');
    expect(proposal.body).toEqual({ proposed_prompt: 'You are Clinic Recall. Add calmer clinic tone.' });
  });

  test('monitor fetch failures are visible', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    await installClinicRecallMocks(page);
    await page.route('**/api/v1/clinic-recall/monitor', async (route) => {
      await route.fulfill({ status: 503, json: { detail: 'Monitor unavailable' } });
    });

    await page.goto('/app');
    await page.getByRole('button', { name: /Monitor/ }).click();

    await expect(page.getByText('Monitor unavailable')).toBeVisible();
  });

  test('operator scripts and voice persona persist clinic settings', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    const calls = await installClinicRecallMocks(page);

    await page.goto('/app?role=operator');
    await page.getByTestId('operator-tool-scripts').click();
    const missedScript = page.getByLabel('missed script');
    await expect(missedScript).toHaveValue('We missed you. Let us help you rebook.');
    await missedScript.fill('Please choose a new appointment time.');
    await page.getByRole('button', { name: 'Save script templates' }).click();
    await expect(page.getByText('Script templates saved.')).toBeVisible();

    await page.getByTestId('operator-tool-voice').click();
    await expect(page.getByLabel('Tone')).toHaveValue('warm, concise, professional');
    await page.getByLabel('Tone').fill('calm, clear, brief');
    await page.getByRole('button', { name: 'Save voice persona' }).click();
    await expect(page.getByText('Voice persona saved.')).toBeVisible();

    const scriptsPut = calls.find((call) => call.type === 'scripts-put');
    const personaPut = calls.find((call) => call.type === 'voice-persona-put');
    expect(scriptsPut.body.templates.missed).toBe('Please choose a new appointment time.');
    expect(personaPut.body.tone).toBe('calm, clear, brief');
  });

  test('operator voice fallback uses provider-routed Clinic Recall endpoint', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    const calls = await installClinicRecallMocks(page);

    await page.goto('/app?role=operator');
    await page.getByTestId('operator-tool-voice').click();
    await expect(page.getByRole('heading', { name: 'Voice persona' })).toBeVisible();
    await page.getByRole('button', { name: 'Run voice fallback' }).click();
    await expect(page.getByText('Voice fallback queued 1 call(s).')).toBeVisible();

    const voiceCall = calls.find((call) => call.type === 'voice-fallback');
    expect(voiceCall).toBeTruthy();
    expect(voiceCall.body).toEqual({});
    expect(voiceCall.url).toContain('/api/v1/clinic-recall/voice/fallback/run');
    expect(voiceCall.url).not.toContain('/api/v1/calls/initiate');
  });

  test('operator voice fallback failures are visible', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    await installClinicRecallMocks(page);
    await page.route('**/api/v1/clinic-recall/voice/fallback/run', async (route) => {
      await route.fulfill({ status: 500, json: { detail: 'Twilio unavailable' } });
    });

    await page.goto('/app?role=operator');
    await page.getByTestId('operator-tool-voice').click();
    await page.getByRole('button', { name: 'Run voice fallback' }).click();

    await expect(page.getByText('Twilio unavailable')).toBeVisible();
  });

  test('staff sees pilot controls locked without operator API access', async ({ page }) => {
    const calls = await installClinicRecallMocks(page);
    await page.goto('/app?role=clinic_staff', { waitUntil: 'domcontentloaded' });

    await page.getByTestId('operator-tool-pilot').click();
    await expect(page.getByRole('heading', { name: 'Pilot controls' })).toBeVisible();
    await expect(page.getByLabel('Pilot controls access state')).toContainText('Operator review required');
    expect(calls.some((call) => call.type.startsWith('pilot-'))).toBe(false);
  });

  test('operator releases the next pilot wave and pauses the programme', async ({ page }) => {
    const calls = await installClinicRecallMocks(page);
    await page.goto('/app?role=operator', { waitUntil: 'domcontentloaded' });

    await page.getByTestId('operator-tool-pilot').click();
    await expect(page.getByText(/5\/15 released/)).toBeVisible();
    const evidenceInput = page.getByLabel('Release evidence SHA-256');
    const releaseButton = page.getByRole('button', { name: 'Release next wave' });
    await evidenceInput.fill('g'.repeat(64));
    await expect(releaseButton).toBeDisabled();
    await evidenceInput.fill(`  ${'a'.repeat(64)}  `);
    await expect(releaseButton).toBeEnabled();
    await releaseButton.click();
    await expect(page.getByText(/15\/15 released/)).toBeVisible();
    await page.getByRole('button', { name: 'Release next wave' }).click();
    await expect(page.getByText('cumulative release requires every preceding ordinal')).toBeVisible();
    await expect(page.getByText(/15\/15 released/)).toBeVisible();
    await expect(page.getByText(/30\/15 released/)).toHaveCount(0);
    await page.getByRole('button', { name: 'Pause' }).click();
    await expect(page.getByText(/pilot-r1 · paused/)).toBeVisible();
    page.once('dialog', (dialog) => dialog.accept());
    await page.getByRole('button', { name: 'Close' }).click();
    await expect(page.getByText(/pilot-r1 · closed/)).toBeVisible();
    await expect(releaseButton).toBeDisabled();

    await page.getByLabel('Programme ID').fill('pilot-draft');
    await page.getByLabel('Release identity').fill('sha256:test-draft');
    await page.getByRole('button', { name: 'Create programme' }).click();
    await expect(page.getByText(/pilot-draft · draft/)).toBeVisible();
    await expect(releaseButton).toBeDisabled();
    await page.getByRole('button', { name: 'Enter dark' }).click();
    await expect(page.getByText(/pilot-draft · dark/)).toBeVisible();
    await expect(releaseButton).toBeEnabled();

    expect(calls.some((call) => call.type === 'pilot-release' && call.body.cumulative_limit === 15 && call.body.evidence_hash === 'a'.repeat(64))).toBe(true);
    expect(calls.some((call) => call.type === 'pilot-pause')).toBe(true);
    expect(calls.some((call) => call.type === 'pilot-close')).toBe(true);
  });

  test('approves pending bookings and launches a campaign without client clinic ids', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    const calls = await installClinicRecallMocks(page);

    await page.goto('/app');
    await page.getByTestId('clinic-queue-item').filter({ hasText: 'Bayo Patient' }).getByRole('button', { name: /Approve/ }).click();
    await expect(page.getByText(/Approved booking_action:pending-1/)).toBeVisible();

    await page.getByRole('button', { name: /Campaigns/ }).click();
    await expect(page.getByRole('heading', { name: 'Launch review and cadence controls' })).toBeVisible();
    await page.getByRole('button', { name: /^Launch$/ }).click();
    await expect(page.getByText('Recovery campaign refreshed for review.')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Queued outbound review' })).toBeVisible();

    const resolveCall = calls.find((call) => call.type === 'resolve');
    const launchCall = calls.find((call) => call.type === 'launch');
    expect(resolveCall.body).toEqual({ decision: 'approve' });
    expect(launchCall.body).toEqual({ channel: 'sms' });
    expect(JSON.stringify(calls)).not.toContain('clinic_id');
  });

  test('keeps acknowledgement separate from resolution and booking authority', async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width: 390, height: 844 });
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    const calls = await installClinicRecallMocks(page);

    await page.goto('/app');
    const urgent = page.getByTestId('clinic-queue-item').filter({ hasText: 'Amina Patient' });
    await expect(urgent).toContainText(/critical/i);
    await expect(urgent).toContainText('Overdue');
    await expect(urgent).toContainText('Alternate requested');
    await expect(urgent.getByRole('button', { name: 'Acknowledge' })).toBeVisible();
    await expect(urgent.getByRole('button', { name: 'Resolve' })).toBeVisible();

    await urgent.getByRole('button', { name: 'Acknowledge' }).click();
    await expect(page.getByText(/Acknowledged escalation:urgent-1/)).toBeVisible();
    const acknowledged = page.getByTestId('clinic-queue-item').filter({ hasText: 'Amina Patient' });
    await expect(acknowledged).toContainText('staff:test');
    await expect(acknowledged.getByRole('button', { name: 'Resolve' })).toBeVisible();

    const booking = page.getByTestId('clinic-queue-item').filter({ hasText: 'Bayo Patient' });
    await booking.getByRole('button', { name: 'Acknowledge' }).click();
    await expect(page.getByText(/Acknowledged booking_action:pending-1/)).toBeVisible();
    await expect(booking.getByRole('button', { name: 'Approve' })).toBeVisible();
    expect(calls.filter((call) => call.type === 'resolve')).toHaveLength(0);

    const mobileActions = await acknowledged.locator('.cr-queue-actions').boundingBox();
    const mobileMain = await acknowledged.locator('.cr-queue-main').boundingBox();
    expect(mobileActions).not.toBeNull();
    expect(mobileMain).not.toBeNull();
    expect(mobileActions.y).toBeGreaterThanOrEqual(mobileMain.y);
    await page.screenshot({ path: testInfo.outputPath('pr12-mobile.png'), fullPage: true });

    await page.setViewportSize({ width: 1280, height: 900 });
    const desktopActions = await acknowledged.locator('.cr-queue-actions').boundingBox();
    const desktopMain = await acknowledged.locator('.cr-queue-main').boundingBox();
    expect(desktopActions).not.toBeNull();
    expect(desktopMain).not.toBeNull();
    expect(desktopActions.x).toBeGreaterThanOrEqual(desktopMain.x + desktopMain.width);
    await page.screenshot({ path: testInfo.outputPath('pr12-desktop.png'), fullPage: true });

    await acknowledged.getByRole('button', { name: 'Resolve' }).click();
    await expect(page.getByTestId('clinic-queue-item').filter({ hasText: 'Amina Patient' })).toHaveCount(0);
    expect(calls.some((call) => call.type === 'acknowledge' && call.itemId === 'escalation:urgent-1')).toBe(true);
    expect(calls.some((call) => call.type === 'acknowledge' && call.itemId === 'booking_action:pending-1')).toBe(true);
    expect(calls.some((call) => call.type === 'resolve' && call.body.decision === 'resolve')).toBe(true);
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 1280);
  });

  test('staff previews and imports a synthetic CSV with re-upload approval', async ({ page }) => {
    test.setTimeout(60_000);
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    const calls = await installClinicRecallMocks(page);
    const consoleErrors = [];
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });

    await page.goto('/app');
    await page.getByRole('button', { name: /Setup/ }).click();
    await expect(page.getByLabel('Controlled CSV import')).toBeVisible();

    const csvBody = [
      'appointment_source_ref,patient_source_ref,patient_name,status,start_at',
      'APPT-E2E-1,PAT-E2E-1,Test Patient E2E,missed,2026-06-20T09:00:00+00:00',
    ].join('\n');
    await page.getByLabel('CSV file').setInputFiles({
      name: 'synthetic-export.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(csvBody, 'utf-8'),
    });
    await page.getByLabel('Export time (when the file was created)').fill('2026-07-25T18:00');
    await page.getByRole('button', { name: 'Preview' }).click();

    await expect(page.getByLabel('CSV preview result')).toContainText('4 row(s) (4 valid, 0 invalid) · 3 patient(s) · 4 appointment(s)');
    // Approve stays disabled until the structured attestation is confirmed.
    const approveButton = page.getByRole('button', { name: 'Approve & Import' });
    await expect(approveButton).toBeDisabled();
    await page.getByText(/I confirm this file was exported/).click();
    await expect(approveButton).toBeEnabled();
    await page.getByRole('checkbox', { name: 'sms' }).check();
    await approveButton.click();

    await expect(page.getByText(/Imported 3 patient record\(s\) and 4 appointment\(s\)\./)).toBeVisible();
    await expect(page.getByLabel('Import history')).toContainText('Imported');
    // Successful import refreshes server-derived onboarding state.
    await expect(page.getByText('Complete: clinic data is connected.')).toBeVisible();
    // The file input is cleared after success; approval reuploaded the file.
    await expect(page.getByLabel('CSV file')).toHaveValue('');

    const preview = calls.find((call) => call.type === 'csv-preview');
    const approve = calls.find((call) => call.type === 'csv-approve');
    expect(preview.hasFilePart).toBe(true);
    expect(preview.hasClinicId).toBe(false);
    expect(approve.hasFilePart).toBe(true);
    expect(approve.hasClinicId).toBe(false);
    expect(approve.attested).toBe(true);

    // No raw patient values or filenames leak into storage or the DOM.
    const stored = await page.evaluate(() => JSON.stringify(window.localStorage) + JSON.stringify(window.sessionStorage));
    expect(stored).not.toContain('PAT-E2E-1');
    expect(stored).not.toContain('synthetic-export');
    const bodyText = await page.evaluate(() => document.body.innerText);
    expect(bodyText).not.toContain('PAT-E2E-1');
    expect(bodyText).not.toContain('Test Patient E2E');
    expect(consoleErrors).toEqual([]);
  });

  test('invalid preview shows bounded safe errors only', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    await installClinicRecallMocks(page);

    await page.goto('/app');
    await page.getByRole('button', { name: /Setup/ }).click();
    await page.getByLabel('CSV file').setInputFiles({
      name: 'invalid.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from('BADROW content that should not appear in the UI', 'utf-8'),
    });
    await page.getByLabel('Export time (when the file was created)').fill('2026-07-25T18:00');
    await page.getByRole('button', { name: 'Preview' }).click();

    await expect(page.getByText('2 validation issue(s). Fix the file and preview again.')).toBeVisible();
    const issues = page.getByLabel('Validation issues');
    await expect(issues).toContainText('invalid phone');
    await expect(issues).toContainText('missing value');
    await expect(page.getByRole('button', { name: 'Approve & Import' })).toHaveCount(0);
    const bodyText = await page.evaluate(() => document.body.innerText);
    expect(bodyText).not.toContain('BADROW');
  });

  test('hash mismatch at approval clears the preview for safe reselection', async ({ page }) => {
    test.setTimeout(60_000);
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    await installClinicRecallMocks(page);

    await page.goto('/app');
    await page.getByRole('button', { name: /Setup/ }).click();
    await page.getByLabel('CSV file').setInputFiles({
      name: 'changed.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from('MISMATCH sentinel body', 'utf-8'),
    });
    await page.getByLabel('Export time (when the file was created)').fill('2026-07-25T18:00');
    await page.getByRole('button', { name: 'Preview' }).click();
    await page.getByText(/I confirm this file was exported/).click();
    await page.getByRole('button', { name: 'Approve & Import' }).click();

    await expect(page.getByText('The preview no longer matches the selected file. Reselect the file and preview again.')).toBeVisible();
    await expect(page.getByLabel('CSV file')).toHaveValue('');
    await expect(page.getByLabel('CSV preview result')).toHaveCount(0);
  });

  test('staff cannot see operator match review; operator resolves outcomes', async ({ page }) => {
    test.setTimeout(60_000);
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    const calls = await installClinicRecallMocks(page);

    await page.goto('/app');
    await page.getByRole('button', { name: /Setup/ }).click();
    await expect(page.getByLabel('Controlled CSV import')).toBeVisible();
    await expect(page.getByLabel('Import source-match review')).toHaveCount(0);

    await page.goto('/app?role=operator');
    await page.getByRole('button', { name: /Setup/ }).click();
    const review = page.getByLabel('Import source-match review');
    await expect(review).toBeVisible();
    await expect(review).toContainText('No source-match reviews yet.');

    // Import a file so the mock seeds unmatched + ambiguous reviews.
    await page.getByLabel('CSV file').setInputFiles({
      name: 'ops.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from('appointment_source_ref\nAPPT-OPS-1', 'utf-8'),
    });
    await page.getByLabel('Export time (when the file was created)').fill('2026-07-25T18:00');
    await page.getByRole('button', { name: 'Preview' }).click();
    await page.getByText(/I confirm this file was exported/).click();
    await page.getByRole('button', { name: 'Approve & Import' }).click();
    await expect(page.getByText(/Imported 3 patient record\(s\)/)).toBeVisible();

    await page.goto('/app?role=operator');
    await page.getByRole('button', { name: /Setup/ }).click();
    await expect(page.getByLabel('Import source-match review')).toContainText('1 unmatched · 1 ambiguous · 0 pending');

    const ambiguous = page.locator('.csv-import-review', { hasText: 'Ambiguous' });
    await expect(ambiguous.getByRole('button', { name: /Link candidate/ })).toHaveCount(0);
    await ambiguous.getByRole('button', { name: 'Refresh candidates' }).click();
    const linkButton = ambiguous.getByRole('button', { name: 'Link candidate 1 (active)' });
    await expect(linkButton).toBeEnabled();
    await linkButton.click();
    await expect(page.getByText('Review linked.')).toBeVisible();

    const unmatched = page.locator('.csv-import-review', { hasText: 'Unmatched' });
    await unmatched.getByRole('button', { name: 'Refresh candidates' }).click();
    await expect(unmatched.getByRole('button', { name: /Link candidate/ })).toHaveCount(0);
    await unmatched.getByRole('button', { name: 'Dismiss' }).click();
    await expect(page.getByText('Review dismissed.')).toBeVisible();

    const linkCall = calls.find((call) => call.type === 'csv-match-resolve' && call.body.action === 'link');
    expect(linkCall.body).toEqual({
      action: 'link',
      candidate_token: 'opaque-token-imr-e2e-2-1',
    });
    expect(JSON.stringify(calls)).not.toContain('CLK-E2E');
    expect(JSON.stringify(calls)).not.toContain('"clinic_id"');
  });

  test('csv import setup stays coherent at mobile width and on backend failure', async ({ page }) => {
    await installApiMocks(page, buildScenariosResponse({ builtins: [BANKING(true)] }));
    await installClinicRecallMocks(page);

    await page.goto('/app');
    await page.getByRole('button', { name: /Setup/ }).click();
    await expect(page.getByLabel('Controlled CSV import')).toBeVisible();

    // Resize to a phone viewport with the import surface open: the PR-08
    // cards must wrap without horizontal overflow or clipped controls.
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByLabel('Controlled CSV import')).toBeVisible();
    const fit = await page.evaluate(() => {
      const card = document.querySelector('.csv-import');
      return {
        cardOverflowPx: card ? card.scrollWidth - card.clientWidth : 0,
        bodyOverflowPx: document.body.scrollWidth - document.body.clientWidth,
      };
    });
    expect(fit.cardOverflowPx).toBeLessThanOrEqual(1);
    expect(fit.bodyOverflowPx).toBeLessThanOrEqual(1);

    // A failing config endpoint degrades to a bounded, recoverable message.
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.route('**/api/v1/clinic-recall/imports/csv/config', async (route) => {
      await route.fulfill({ status: 500, json: { detail: 'boom' } });
    });
    await page.reload();
    await page.getByRole('button', { name: /Setup/ }).click();
    await expect(page.getByLabel('CSV import unavailable')).toBeVisible();
    await expect(page.getByRole('heading', { name: /Launch without configuration expertise/ })).toBeVisible();
  });
});
