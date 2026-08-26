/**
 * Deterministic API mocks for documentation screenshot specs.
 *
 * Purpose: render every Clinic Recall surface (staff + operator) with
 * representative, privacy-safe FAKE data so documentation screenshots
 * (e.g. e2e/control-room-screenshot.spec.js) are stable and reviewable.
 *
 * All patient names, phone numbers, and hashes below are invented.
 * Field shapes mirror the components in src/components/ClinicRecallSurfaces.jsx
 * and src/components/ProductShell.jsx, and the endpoint list mirrors
 * e2e/clinic-recall-surfaces.spec.js (the assertion source of truth).
 *
 * Route priority note: Playwright gives LATER-registered routes HIGHER
 * priority, so the catch-all is registered first.
 */

const NOW = '2026-07-09T10:00:00Z';

const QUEUE_ITEMS = [
  {
    item_id: 'escalation:urgent-1',
    kind: 'escalation',
    priority: 'high',
    reason: 'urgent',
    status: 'open',
    patient_name: 'Amina Example',
    outreach_state: 'escalated',
    appointment_start: '2026-07-02T09:00:00Z',
    slot_start: null,
    booking_type: null,
    context_summary: 'Inbound call flagged urgent by the safety gate and routed to staff.',
    created_at: '2026-07-09T08:45:00Z',
  },
  {
    item_id: 'booking_action:pending-1',
    kind: 'booking_action',
    priority: 'normal',
    reason: 'pending_booking_approval',
    status: 'pending',
    patient_name: 'Bayo Example',
    outreach_state: 'escalated',
    appointment_start: '2026-07-01T14:00:00Z',
    slot_start: '2026-07-11T13:00:00Z',
    booking_type: 'book',
    context_summary: 'Patient accepted a rebooking slot by SMS; staff approval required before it is confirmed.',
    created_at: '2026-07-09T09:05:00Z',
  },
  {
    item_id: 'escalation:clinical-1',
    kind: 'escalation',
    priority: 'high',
    reason: 'clinical',
    status: 'open',
    patient_name: 'Chidi Example',
    outreach_state: 'escalated',
    appointment_start: '2026-06-28T11:30:00Z',
    slot_start: null,
    booking_type: null,
    context_summary: 'Reply mentioned new symptoms — conversation stopped and handed to a human.',
    created_at: '2026-07-09T09:20:00Z',
  },
];

const OUTBOX_ITEMS = [
  {
    item_id: 'outbox-1',
    campaign_id: 'campaign-recovery-jul',
    patient_name: 'Dara Example',
    channel: 'sms',
    status: 'ready',
    reason_code: 'missed_appointment',
    eligible_now: true,
    skip_reason: null,
    message_preview: 'Hi Dara, we missed you at Example Clinic. Reply YES to rebook or STOP to opt out.',
    template_id: 'recall-missed-v3',
    campaign_status: 'pending_approval',
    scheduled_for: '2026-07-09T14:00:00Z',
  },
  {
    item_id: 'outbox-2',
    campaign_id: 'campaign-recovery-jul',
    patient_name: 'Efe Example',
    channel: 'sms',
    status: 'ready',
    reason_code: 'overdue_follow_up',
    eligible_now: false,
    skip_reason: 'outside_contact_hours',
    message_preview: 'Hi Efe, you may be due for a follow-up at Example Clinic. Reply YES to book.',
    template_id: 'recall-overdue-v2',
    campaign_status: 'pending_approval',
    scheduled_for: '2026-07-10T09:15:00Z',
  },
];

const SENT_ITEMS = [
  {
    id: 'sent-1',
    item_id: 'sent-1',
    channel: 'sms',
    direction: 'outbound',
    occurred_at: '2026-07-08T15:10:00Z',
    outcome: 'delivered',
    outreach_job_id: 'job-1041',
    template_id: 'recall-missed-v3',
    content_preview: 'Hi, we missed you at Example Clinic. Reply YES to rebook or STOP to opt out.',
  },
  {
    id: 'sent-2',
    item_id: 'sent-2',
    channel: 'sms',
    direction: 'inbound',
    occurred_at: '2026-07-08T15:14:00Z',
    intent: 'confirm_slot',
    outreach_job_id: 'job-1041',
    template_id: null,
    content_preview: 'YES please, Thursday works.',
  },
  {
    id: 'sent-3',
    item_id: 'sent-3',
    channel: 'call',
    direction: 'outbound',
    occurred_at: '2026-07-07T11:02:00Z',
    outcome: 'no_reply',
    outreach_job_id: 'job-1038',
    template_id: 'voice-fallback-v1',
    content_preview: null,
  },
];

const CAMPAIGNS = [
  { id: 'campaign-recovery-jul', type: 'recovery', status: 'draft', jobs: 9, is_launchable: true, is_approvable: true, is_pausable: false },
  { id: 'campaign-reminder-jun', type: 'reminder', status: 'active', jobs: 4, is_launchable: false, is_approvable: false, is_pausable: true },
];

const INCIDENTS = [
  {
    id: 'incident-1',
    category: 'communication_failure',
    severity: 'low',
    status: 'new',
    source: 'staff',
    description: 'Reminder SMS referred to the wrong clinic opening hours.',
    occurred_hour: '2026-07-08T16:00:00Z',
  },
  {
    id: 'incident-2',
    category: 'agent_behaviour',
    severity: 'no_harm',
    status: 'under_review',
    source: 'patient',
    description: 'Caller reported the voice agent repeated a question twice before handing off.',
    occurred_hour: '2026-07-07T10:00:00Z',
  },
];

const INBOUND_TASKS = [
  {
    id: 'inbound-task-1',
    inbound_call_id: 'inbound-call-1',
    source: 'call',
    kind: 'callback',
    status: 'open',
    priority: 'normal',
    reason: 'callback',
    summary: 'Caller asked reception to call back about parking access.',
    created_at: '2026-07-09T08:20:00Z',
  },
  {
    id: 'inbound-task-2',
    inbound_call_id: 'inbound-call-2',
    source: 'call',
    kind: 'escalation',
    status: 'open',
    priority: 'high',
    reason: 'clinical',
    summary: 'Clinical concern raised on an inbound call — routed to staff.',
    created_at: '2026-07-09T08:40:00Z',
  },
  {
    id: 'inbound-task-3',
    inbound_message_id: 'inbound-message-1',
    source: 'sms',
    kind: 'booking_request',
    status: 'open',
    priority: 'normal',
    reason: 'booking_request',
    summary: 'Booking request received by inbound SMS.',
    created_at: '2026-07-09T09:00:00Z',
  },
];

/**
 * Install every route mock the walkthrough needs.
 * Returns a mutable `state` object so tests can assert against post-action data.
 */
export async function installWalkthroughMocks(page) {
  const state = {
    queueItems: [...QUEUE_ITEMS],
    inboundTasks: [...INBOUND_TASKS],
    incidents: [...INCIDENTS],
    campaigns: CAMPAIGNS.map((campaign) => ({ ...campaign })),
    promptProposals: [],
    calls: [],
  };

  // Catch-all FIRST (lowest priority) so nothing escapes to a real network.
  await page.route('**/api/v1/**', async (route) => {
    state.calls.push({ type: 'unmatched', method: route.request().method(), url: route.request().url() });
    await route.fulfill({ json: {} });
  });

  // --- Backend health (System/Health panel via BackendIndicator) ------------
  await page.route('**/api/v1/health', async (route) => {
    await route.fulfill({ json: { status: 'healthy', app: 'clinic-recall', revision: 'walkthrough-mock' } });
  });
  await page.route('**/api/v1/readiness', async (route) => {
    await route.fulfill({
      json: {
        status: 'healthy',
        checks: [
          { component: 'redis', status: 'healthy' },
          { component: 'azure_openai', status: 'healthy' },
          { component: 'speech_services', status: 'healthy' },
          { component: 'acs_caller', status: 'healthy' },
          { component: 'rt_agents', status: 'healthy' },
        ],
      },
    });
  });
  await page.route('**/api/v1/agents', async (route) => {
    await route.fulfill({ json: { agents: [{ name: 'RecallAgent', description: 'Clinic Recall outbound agent' }] } });
  });

  // --- Dashboard load --------------------------------------------------------
  await page.route('**/api/v1/clinic-recall/inbox', async (route) => {
    await route.fulfill({ json: { items: state.queueItems } });
  });
  await page.route('**/api/v1/clinic-recall/roi?**', async (route) => {
    await route.fulfill({
      json: {
        start: '2026-07-01T00:00:00Z',
        end: '2026-08-01T00:00:00Z',
        contacted: 38,
        rebooked: 17,
        conversion_rate: 0.4473,
        recovered_revenue: '1445.00',
        no_show_delta: 0.18,
        opt_out_rate: 0.0526,
        monthly_net: '1246.00',
        roi_multiple: '7.26',
        subscription_cost: '199.00',
        usage_cost: '0.00',
      },
    });
  });
  await page.route('**/api/v1/clinic-recall/roi.csv?**', async (route) => {
    await route.fulfill({ contentType: 'text/csv', body: 'metric,value\ncontacted,38\nrebooked,17\n' });
  });
  await page.route('**/api/v1/clinic-recall/campaign/settings', async (route) => {
    const isPut = route.request().method() === 'PUT';
    const body = isPut ? JSON.parse(route.request().postData() || '{}') : null;
    state.calls.push({ type: isPut ? 'settings-put' : 'settings-get', body });
    await route.fulfill({
      json: {
        name: 'Example Clinic',
        timezone: 'Europe/London',
        daily_caps: body?.daily_caps ?? 40,
        branding: body?.branding ?? { sms_sender: 'ExampleClinic' },
        contact_hours: body?.contact_hours ?? { start_hour: 9, end_hour: 17 },
      },
    });
  });
  await page.route('**/api/v1/clinic-recall/campaigns', async (route) => {
    await route.fulfill({ json: { campaigns: state.campaigns } });
  });
  await page.route('**/api/v1/clinic-recall/outbox?**', async (route) => {
    await route.fulfill({ json: { items: OUTBOX_ITEMS } });
  });
  await page.route('**/api/v1/clinic-recall/interactions', async (route) => {
    await route.fulfill({ json: { items: SENT_ITEMS } });
  });
  await page.route('**/api/v1/clinic-recall/incidents', async (route) => {
    if (route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      state.calls.push({ type: 'incident-create', body });
      const incident = {
        id: `incident-${state.incidents.length + 1}`,
        category: body.category,
        severity: body.severity,
        status: 'new',
        source: 'staff',
        description: body.description,
        occurred_hour: NOW,
      };
      state.incidents = [incident, ...state.incidents];
      await route.fulfill({ json: incident });
      return;
    }
    await route.fulfill({ json: { items: state.incidents } });
  });
  await page.route('**/api/v1/clinic-recall/incidents/*/status', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    const incidentId = decodeURIComponent(route.request().url().split('/incidents/')[1].split('/status')[0]);
    state.calls.push({ type: 'incident-status', incidentId, body });
    state.incidents = state.incidents.map((item) => (item.id === incidentId ? { ...item, status: body.status } : item));
    await route.fulfill({ json: state.incidents.find((item) => item.id === incidentId) || {} });
  });

  // --- Inbox / queue actions -------------------------------------------------
  await page.route('**/api/v1/clinic-recall/queue/**/resolve', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    state.calls.push({ type: 'queue-resolve', url: route.request().url(), body });
    state.queueItems = state.queueItems.filter(
      (item) => !route.request().url().includes(encodeURIComponent(item.item_id)),
    );
    await route.fulfill({
      json: {
        resolved: true,
        booking_status: body.decision === 'approve' ? 'completed' : undefined,
        escalation_status: body.decision === 'reject' ? 'resolved' : undefined,
      },
    });
  });
  await page.route('**/api/v1/clinic-recall/inbox/**/acknowledge', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    state.calls.push({ type: 'inbox-acknowledge', url: route.request().url(), body });
    state.queueItems = state.queueItems.filter(
      (item) => !route.request().url().includes(encodeURIComponent(item.item_id)),
    );
    await route.fulfill({ json: { resolved: true, escalation_status: 'resolved' } });
  });

  // --- Phone surface ----------------------------------------------------------
  await page.route('**/api/v1/clinic-recall/phone-numbers', async (route) => {
    await route.fulfill({
      json: {
        items: [
          { id: 'phone-1', provider: 'twilio', phone_number: '+441632960001', purpose: 'inbound', status: 'active', webhook_url: '/api/v1/voice/twilio/twiml', test_status: 'green' },
          { id: 'phone-2', provider: 'acs', phone_number: '+441632960002', purpose: 'inbound', status: 'inactive', webhook_url: '/api/v1/calls/event', test_status: 'not_tested' },
        ],
      },
    });
  });
  await page.route('**/api/v1/clinic-recall/inbound-calls', async (route) => {
    await route.fulfill({
      json: {
        items: [
          { id: 'inbound-call-1', provider: 'twilio', provider_call_id: 'CAmock0001', called_number: '+441632960001', caller_number_redacted: 'hash:a1b2c3d4', status: 'completed', outcome: 'callback_requested', created_at: '2026-07-09T08:18:00Z' },
          { id: 'inbound-call-2', provider: 'twilio', provider_call_id: 'CAmock0002', called_number: '+441632960001', caller_number_redacted: 'hash:e5f6a7b8', status: 'completed', outcome: 'escalated', created_at: '2026-07-09T08:38:00Z' },
        ],
      },
    });
  });
  await page.route('**/api/v1/clinic-recall/inbound-messages', async (route) => {
    await route.fulfill({
      json: {
        items: [
          { id: 'inbound-message-1', provider: 'twilio', from_number_redacted: 'hash:c9d0e1f2', intent: 'booking_request', status: 'routed', summary: 'Booking request received by inbound SMS.', created_at: '2026-07-09T09:00:00Z' },
        ],
      },
    });
  });
  await page.route('**/api/v1/clinic-recall/inbound-tasks', async (route) => {
    await route.fulfill({ json: { items: state.inboundTasks } });
  });
  await page.route('**/api/v1/clinic-recall/inbound-tasks/*/resolve', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    const taskId = decodeURIComponent(route.request().url().split('/inbound-tasks/')[1].split('/resolve')[0]);
    state.calls.push({ type: 'inbound-task-resolve', taskId, body });
    state.inboundTasks = state.inboundTasks.filter((task) => task.id !== taskId);
    await route.fulfill({ json: { id: taskId, status: 'resolved' } });
  });
  await page.route('**/api/v1/clinic-recall/inbound-metrics', async (route) => {
    await route.fulfill({
      json: {
        calls_total: 12,
        calls_completed: 10,
        texts_total: 3,
        texts_routed: 3,
        open_tasks: state.inboundTasks.length,
        callbacks_open: state.inboundTasks.filter((task) => task.kind === 'callback').length,
        escalations_open: state.inboundTasks.filter((task) => task.kind === 'escalation').length,
        booking_requests_open: state.inboundTasks.filter((task) => task.kind === 'booking_request').length,
      },
    });
  });
  await page.route('**/api/v1/clinic-recall/inbound-config', async (route) => {
    const isPut = route.request().method() === 'PUT';
    const body = isPut ? JSON.parse(route.request().postData() || '{}') : null;
    state.calls.push({ type: isPut ? 'inbound-config-put' : 'inbound-config-get', body });
    await route.fulfill({
      json: body || {
        greeting: 'Hello, thanks for calling Example Clinic.',
        callback_sla_hours: 4,
        escalation_destination: 'Front desk',
        recording_enabled: false,
      },
    });
  });

  // --- Campaign lifecycle -----------------------------------------------------
  await page.route('**/api/v1/clinic-recall/campaigns/launch', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    state.calls.push({ type: 'campaign-launch', body });
    await route.fulfill({
      json: { candidate_queue: { detected_total: 9, queued: 3, already_queued: 2, detected: { missed: 6, overdue: 3 }, skipped: { opted_out: 1 } } },
    });
  });
  await page.route('**/api/v1/clinic-recall/campaigns/*/approve', async (route) => {
    const campaignId = decodeURIComponent(route.request().url().split('/campaigns/')[1].split('/approve')[0]);
    state.calls.push({ type: 'campaign-approve', campaignId });
    state.campaigns = state.campaigns.map((campaign) =>
      campaign.id === campaignId ? { ...campaign, status: 'active', is_approvable: false, is_pausable: true } : campaign,
    );
    await route.fulfill({ json: { id: campaignId, status: 'active' } });
  });
  await page.route('**/api/v1/clinic-recall/campaigns/*/pause', async (route) => {
    const campaignId = decodeURIComponent(route.request().url().split('/campaigns/')[1].split('/pause')[0]);
    state.calls.push({ type: 'campaign-pause', campaignId });
    state.campaigns = state.campaigns.map((campaign) =>
      campaign.id === campaignId ? { ...campaign, status: 'paused', is_pausable: false } : campaign,
    );
    await route.fulfill({ json: { id: campaignId, status: 'paused' } });
  });

  // --- Setup / onboarding -----------------------------------------------------
  let onboardingState = {
    status: 'pending',
    onboarding_required: true,
    onboarding_step: 'choose_script',
    onboarding_steps: {
      connect_data: true,
      confirm_number: true,
      choose_script: false,
      set_rules: false,
      first_campaign: false,
    },
    outreach_enabled: false,
  };
  await page.route('**/api/v1/clinic-recall/onboarding', async (route) => {
    if (route.request().method() === 'PUT') {
      const body = JSON.parse(route.request().postData() || '{}');
      state.calls.push({ type: 'onboarding-put', body });
      if (body.completed_step) {
        onboardingState = {
          ...onboardingState,
          onboarding_steps: { ...onboardingState.onboarding_steps, [body.completed_step]: true },
        };
      }
    }
    await route.fulfill({ json: onboardingState });
  });
  await page.route('**/api/v1/clinic-recall/signup', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    state.calls.push({ type: 'signup', body });
    await route.fulfill({ json: { clinic_id: 'clinic-example', status: 'pending', onboarding_next: 'connect_data' } });
  });

  // --- Operator panels ---------------------------------------------------------
  await page.route('**/api/v1/clinic-recall/monitor', async (route) => {
    await route.fulfill({
      json: {
        open_queue_count: state.queueItems.length,
        queued_outbox_count: OUTBOX_ITEMS.length,
        active_campaigns: state.campaigns.filter((campaign) => campaign.status === 'active').length,
        recent_interactions_count: 8,
        latest_escalation_at: '2026-07-09T09:20:00Z',
        voice_fallback_summary: { call_jobs_by_state: { no_reply: 1, completed: 3 }, latest_call_interaction_at: '2026-07-08T16:40:00Z' },
      },
    });
  });
  await page.route('**/api/v1/clinic-recall/voice/fallback/run', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    state.calls.push({ type: 'voice-fallback', body });
    await route.fulfill({ json: { voice_fallback: { calls_initiated: 2, failed_calls: 0, idempotent_skips: 1, skipped: {} } } });
  });
  await page.route('**/api/v1/clinic-recall/operator/script-templates', async (route) => {
    const isPut = route.request().method() === 'PUT';
    const body = isPut ? JSON.parse(route.request().postData() || '{}') : null;
    state.calls.push({ type: isPut ? 'scripts-put' : 'scripts-get', body });
    await route.fulfill({
      json: {
        templates: body?.templates || {
          missed: 'Hi {first_name}, we missed you at {clinic_name}. Reply YES to rebook or STOP to opt out.',
          overdue: 'Hi {first_name}, you may be due for a follow-up at {clinic_name}. Reply YES to book.',
          feedback: 'Thanks for visiting {clinic_name}. Reply 1-5 to rate your visit.',
        },
      },
    });
  });
  await page.route('**/api/v1/clinic-recall/operator/voice-persona', async (route) => {
    const isPut = route.request().method() === 'PUT';
    const body = isPut ? JSON.parse(route.request().postData() || '{}') : null;
    state.calls.push({ type: isPut ? 'voice-persona-put' : 'voice-persona-get', body });
    await route.fulfill({ json: body || { display_name: 'Clinic Recall', tone: 'warm, concise, professional', voice_name: 'Sonia' } });
  });
  await page.route('**/api/v1/clinic-recall/operator/prompt-proposals', async (route) => {
    if (route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      state.calls.push({ type: 'prompt-proposal', body });
      const proposal = {
        id: `proposal-${state.promptProposals.length + 1}`,
        actor: 'operator:walkthrough',
        status: 'submitted',
        proposed_prompt: body.proposed_prompt,
        gate_required: true,
        created_at: NOW,
        updated_at: NOW,
        diff: '--- recall-agent.prompt.md\n+++ recall-agent.prompt.md (proposed)\n+Add calmer clinic tone.\n',
      };
      state.promptProposals = [proposal, ...state.promptProposals];
      await route.fulfill({ json: proposal });
      return;
    }
    await route.fulfill({ json: { proposals: state.promptProposals } });
  });

  return state;
}
