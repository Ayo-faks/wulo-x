import React, { useEffect, useMemo, useState } from 'react';
import AssessmentRoundedIcon from '@mui/icons-material/AssessmentRounded';
import CampaignRoundedIcon from '@mui/icons-material/CampaignRounded';
import HealthAndSafetyRoundedIcon from '@mui/icons-material/HealthAndSafetyRounded';
import ManageAccountsRoundedIcon from '@mui/icons-material/ManageAccountsRounded';
import RecordVoiceOverRoundedIcon from '@mui/icons-material/RecordVoiceOverRounded';
import SettingsRoundedIcon from '@mui/icons-material/SettingsRounded';
import SupportAgentRoundedIcon from '@mui/icons-material/SupportAgentRounded';
import TuneRoundedIcon from '@mui/icons-material/TuneRounded';
import BackendIndicator from './BackendIndicator.jsx';
import HelpButton from './HelpButton.jsx';
import CsvImportSetup, { OperatorImportMatches } from './CsvImportSetup.jsx';
import { API_BASE_URL } from '../config/constants.js';

const OPERATOR_ROLES = new Set(['operator']);
const SHA256_HEX_PATTERN = /^[0-9a-f]{64}$/;
const PILOT_ACTIVE_RELEASE_LIMITS = new Set([5, 15, 30]);

function canReleasePilotWave(programme) {
  return (programme.state === 'dark' && programme.active_cumulative_limit === 0)
    || (programme.state === 'active' && PILOT_ACTIVE_RELEASE_LIMITS.has(programme.active_cumulative_limit));
}

const PANEL_COPY = {
  monitor: {
    title: 'Clinic status monitor',
    body: 'Track queue movement, outbound review, campaign state, and recent activity without exposing model or provider internals.',
  },
  pilot: {
    title: 'Pilot controls',
    body: 'Manage the bounded clinic programme, cumulative release wave, and immediate database pause.',
  },
  scripts: {
    title: 'Scripts/Templates',
    body: 'Recall script templates replace the ART scenario selector for product use: missed appointment, overdue follow-up, recurring care, and feedback.',
  },
  voice: {
    title: 'Voice persona',
    body: 'Clinic voice and tone settings live here. Provider/orchestration mode remains server-side and operator controlled.',
  },
  agent: {
    title: 'Agent tuning',
    body: 'Agent changes should propose prompt/config diffs for review and AgentOps gating. No live hot-editing of production behaviour.',
  },
  system: {
    title: 'Health',
    body: 'Check whether the Clinic Recall control room can reach the backend. Sensitive diagnostics remain operator-only.',
  },
};

const RAIL_ITEMS = [
  { id: 'dashboard', label: 'Dashboard', icon: AssessmentRoundedIcon },
  { id: 'setup', label: 'Setup', icon: CampaignRoundedIcon },
  { id: 'monitor', label: 'Monitor', icon: SupportAgentRoundedIcon },
  { id: 'pilot', label: 'Pilot', icon: ManageAccountsRoundedIcon, gated: true },
  { id: 'scripts', label: 'Scripts', icon: SettingsRoundedIcon, gated: true },
  { id: 'voice', label: 'Voice', icon: RecordVoiceOverRoundedIcon, gated: true },
  { id: 'agent', label: 'Agent', icon: TuneRoundedIcon, gated: true },
  { id: 'system', label: 'Health', icon: HealthAndSafetyRoundedIcon, gated: true },
];

const STAFF_LOCKED_COPY = {
  pilot: {
    eyebrow: 'Operator required',
    title: 'Pilot controls',
    body: 'Pilot enrollment, cumulative release, and programme pause are operator-managed.',
    detail: 'Staff can continue queue work but cannot change cohort or release state.',
  },
  scripts: {
    eyebrow: 'Read-only',
    title: 'Scripts/Templates',
    body: 'The clinic uses approved recall, overdue follow-up, and feedback scripts. Template edits are reviewed by an operator so patient messages stay deterministic and auditable.',
    detail: 'Ask an operator to update script wording or submit a new template for review.',
  },
  voice: {
    eyebrow: 'Read-only',
    title: 'Voice persona',
    body: 'The clinic voice and tone are operator-managed. Staff can see that the voice is controlled here, but provider routing and fallback controls stay locked.',
    detail: 'Voice fallback and persona changes require operator access.',
  },
  agent: {
    eyebrow: 'Operator required',
    title: 'Agent tuning',
    body: 'Recall Agent behavior is governed as prompt-as-code. Staff cannot hot-edit production behavior from the control room.',
    detail: 'Operators can create prompt proposals and diffs; real changes still require AgentOps evaluation, ASSERT, Red Team, and review.',
  },
  system: {
    eyebrow: 'Basic status',
    title: 'Health',
    body: 'Basic connectivity is visible to clinic staff. Deeper backend diagnostics and operational controls stay operator-only.',
    detail: 'No provider names, model settings, secrets, or voice pipeline internals are shown here.',
  },
};

const ONBOARDING_STEPS = [
  ['connect_data', 'Connect data', 'Upload CSV or connect calendar/PMS.'],
  ['confirm_number', 'Confirm number', 'Use the configured Twilio/ACS number for patient outreach.'],
  ['choose_script', 'Choose script', 'Start from a safe appointment-recovery template.'],
  ['set_rules', 'Set rules', 'Review contact hours, caps, branding, and opt-out language.'],
  ['first_campaign', 'Run first campaign', 'Launch into review before outreach sends.'],
];

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `Request failed (${response.status})`);
  }
  return payload;
}

function rolesFromPrincipal(payload) {
  const roles = new Set();
  const principal = Array.isArray(payload) ? payload[0] : payload;
  for (const claim of principal?.user_claims || principal?.claims || []) {
    const type = String(claim.typ || claim.type || '').toLowerCase();
    const value = String(claim.val || claim.value || '').trim().toLowerCase();
    if (!value) continue;
    if (type.includes('role')) {
      roles.add(value);
    }
  }
  return roles;
}

function useShellIdentity() {
  const [identity, setIdentity] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    const developmentRole = import.meta.env.DEV ? params.get('role') : null;
    return {
      displayName: 'Clinic staff',
      roles: new Set([developmentRole || import.meta.env.VITE_APP_ROLE || 'clinic_staff']),
      source: developmentRole ? 'development' : 'fallback',
    };
  });

  useEffect(() => {
    let cancelled = false;
    fetch('/.auth/me', { credentials: 'include' })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        if (!payload || cancelled) return;
        const principal = Array.isArray(payload) ? payload[0] : payload;
        const roles = rolesFromPrincipal(payload);
        if (!roles.size) roles.add('clinic_staff');
        setIdentity({
          displayName: principal?.user_claims?.find?.((claim) => claim.typ?.includes('name'))?.val
            || principal?.user_id
            || principal?.userDetails
            || 'Clinic staff',
          roles,
          source: 'easyauth',
        });
      })
      .catch(() => null);
    return () => {
      cancelled = true;
    };
  }, []);

  return identity;
}

function RoleBadge({ identity }) {
  const roleText = Array.from(identity.roles).join(', ');
  return (
    <div className="shell-user" aria-label={`Signed in as ${identity.displayName}`}>
      <span>{identity.displayName}</span>
      <small>{roleText}</small>
      <a className="shell-signout" href="/.auth/logout?post_logout_redirect_uri=/">Sign out</a>
    </div>
  );
}

function SetupPanel({ isOperator }) {
  const [onboarding, setOnboarding] = useState(null);
  const [setupStatus, setSetupStatus] = useState({ busyStep: '', error: '' });

  const refreshOnboarding = React.useCallback(() => {
    fetch(`${API_BASE_URL}/api/v1/clinic-recall/onboarding`)
      .then(readJson)
      .then(setOnboarding)
      .catch((error) => setSetupStatus({ busyStep: '', error: error.message }));
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE_URL}/api/v1/clinic-recall/onboarding`)
      .then(readJson)
      .then((payload) => {
        if (!cancelled) setOnboarding(payload);
      })
      .catch((error) => {
        if (!cancelled) setSetupStatus({ busyStep: '', error: error.message });
      });
    return () => { cancelled = true; };
  }, []);

  const completeStep = async (step) => {
    setSetupStatus({ busyStep: step, error: '' });
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/onboarding`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ completed_step: step }),
      });
      setOnboarding(await readJson(response));
      setSetupStatus({ busyStep: '', error: '' });
    } catch (error) {
      setSetupStatus({ busyStep: '', error: error.message });
    }
  };

  return (
    <section className="shell-panel shell-setup" aria-labelledby="setup-title">
      <p className="shell-eyebrow">Guided setup</p>
      <h2 id="setup-title">Launch without configuration expertise.</h2>
      {onboarding ? (
        <div className="shell-action-card" aria-label="Onboarding status">
          <strong>Setup status: {onboarding.status}</strong>
          <p>{onboarding.outreach_enabled ? 'Outreach enabled' : 'Sandbox mode: outreach disabled'}</p>
        </div>
      ) : null}
      <div className="setup-steps">
        {ONBOARDING_STEPS.map(([step, title, body], index) => {
          const complete = Boolean(onboarding?.onboarding_steps?.[step]);
          return (
          <article key={step} className="setup-step">
            <strong>{index + 1}</strong>
            <div>
              <h3>{title}</h3>
              <p>{body}</p>
              {onboarding && step === 'connect_data' ? (
                <p className="setup-step-note">
                  {complete
                    ? 'Complete: clinic data is connected.'
                    : 'Completes automatically after a successful CSV import below.'}
                </p>
              ) : null}
              {onboarding && step !== 'connect_data' ? (
                <button
                  type="button"
                  className="shell-secondary-button"
                  disabled={complete || setupStatus.busyStep === step}
                  onClick={() => completeStep(step)}
                >
                  {complete ? 'Complete' : `Mark ${title} complete`}
                </button>
              ) : null}
            </div>
          </article>
          );
        })}
      </div>
      <CsvImportSetup onImported={refreshOnboarding} />
      {isOperator ? <OperatorImportMatches /> : null}
      {setupStatus.error ? <div className="shell-inline-error" role="alert">{setupStatus.error}</div> : null}
      <a className="shell-primary-link" href="/app?panel=dashboard">Continue to dashboard</a>
    </section>
  );
}

function LockedPanel({ activePanel }) {
  const copy = STAFF_LOCKED_COPY[activePanel] || STAFF_LOCKED_COPY.agent;
  return (
    <section className="shell-panel" aria-labelledby={`${activePanel}-title`}>
      <p className="shell-eyebrow">{copy.eyebrow}</p>
      <h2 id={`${activePanel}-title`}>{copy.title}</h2>
      <p>{copy.body}</p>
      <div className="shell-action-card shell-locked-card" aria-label={`${copy.title} access state`}>
        <ManageAccountsRoundedIcon fontSize="small" />
        <div>
          <strong>{activePanel === 'system' ? 'Staff-safe view' : 'Operator review required'}</strong>
          <p>{copy.detail}</p>
        </div>
      </div>
      {activePanel === 'system' ? (
        <div className="shell-embedded-health shell-basic-health">
          <BackendIndicator url={API_BASE_URL} compact />
        </div>
      ) : null}
    </section>
  );
}

function OperatorPanel({ activePanel, isOperator }) {
  const [voiceStatus, setVoiceStatus] = useState({ busy: false, message: '', error: '' });
  const [voicePersona, setVoicePersona] = useState({ display_name: '', tone: '', voice_name: '' });
  const [voicePersonaStatus, setVoicePersonaStatus] = useState({ busy: false, message: '', error: '' });
  const [scriptTemplates, setScriptTemplates] = useState({ missed: '', overdue: '', feedback: '' });
  const [scriptTemplatesLoaded, setScriptTemplatesLoaded] = useState(false);
  const [scriptStatus, setScriptStatus] = useState({ busy: false, message: '', error: '' });
  const [promptProposal, setPromptProposal] = useState('');
  const [proposalStatus, setProposalStatus] = useState({ busy: false, diff: '', message: '', error: '' });
  const [promptProposals, setPromptProposals] = useState([]);
  const [monitorStatus, setMonitorStatus] = useState({ busy: false, data: null, error: '' });
  const [pilotStatus, setPilotStatus] = useState({ busy: false, programmes: [], message: '', error: '' });
  const [pilotDraft, setPilotDraft] = useState({
    programme_id: '',
    environment: 'production',
    release_identity: '',
    evidence_hash: '',
  });
  const pilotEvidenceHash = pilotDraft.evidence_hash.trim();
  const hasValidPilotEvidence = SHA256_HEX_PATTERN.test(pilotEvidenceHash);

  useEffect(() => {
    if (activePanel !== 'pilot' || !isOperator) return;
    let cancelled = false;
    setPilotStatus((current) => ({ ...current, busy: true, error: '' }));
    fetch(`${API_BASE_URL}/api/v1/clinic-recall/operator/pilot/programmes`)
      .then(readJson)
      .then((payload) => {
        if (!cancelled) setPilotStatus({ busy: false, programmes: payload.programmes || [], message: '', error: '' });
      })
      .catch((error) => {
        if (!cancelled) setPilotStatus({ busy: false, programmes: [], message: '', error: error.message });
      });
    return () => { cancelled = true; };
  }, [activePanel, isOperator]);

  const refreshPilot = async (message = '') => {
    const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/operator/pilot/programmes`);
    const payload = await readJson(response);
    setPilotStatus({ busy: false, programmes: payload.programmes || [], message, error: '' });
  };

  const pilotMutation = async (url, body, message) => {
    setPilotStatus((current) => ({ ...current, busy: true, message: '', error: '' }));
    try {
      const response = await fetch(`${API_BASE_URL}${url}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      await readJson(response);
      await refreshPilot(message);
    } catch (error) {
      setPilotStatus((current) => ({ ...current, busy: false, message: '', error: error.message }));
    }
  };

  const createPilot = async (event) => {
    event.preventDefault();
    await pilotMutation(
      '/api/v1/clinic-recall/operator/pilot/programmes',
      {
        programme_id: pilotDraft.programme_id,
        environment: pilotDraft.environment,
        release_identity: pilotDraft.release_identity,
      },
      'Pilot programme created.',
    );
  };

  const releasePilot = async (programme) => {
    const nextLimit = { 0: 5, 5: 15, 15: 30, 30: 50 }[programme.active_cumulative_limit];
    if (!nextLimit || !hasValidPilotEvidence || !canReleasePilotWave(programme)) return;
    await pilotMutation(
      `/api/v1/clinic-recall/operator/pilot/programmes/${encodeURIComponent(programme.id)}/release`,
      { cumulative_limit: nextLimit, evidence_hash: pilotEvidenceHash },
      `Cumulative wave released to ${nextLimit}.`,
    );
  };

  const darkPilot = async (programme) => {
    if (!hasValidPilotEvidence) return;
    await pilotMutation(
      `/api/v1/clinic-recall/operator/pilot/programmes/${encodeURIComponent(programme.id)}/dark`,
      { evidence_hash: pilotEvidenceHash },
      'Pilot programme moved to dark qualification.',
    );
  };

  const pausePilot = async (programme) => {
    await pilotMutation(
      `/api/v1/clinic-recall/operator/pilot/programmes/${encodeURIComponent(programme.id)}/pause`,
      { reason: 'operator_pause' },
      'Pilot programme paused.',
    );
  };

  const closePilot = async (programme) => {
    if (!window.confirm('Close this pilot programme permanently?')) return;
    await pilotMutation(
      `/api/v1/clinic-recall/operator/pilot/programmes/${encodeURIComponent(programme.id)}/close`,
      { reason: 'pilot_complete' },
      'Pilot programme closed.',
    );
  };

  useEffect(() => {
    if (activePanel !== 'monitor') return;
    let cancelled = false;
    setMonitorStatus({ busy: true, data: null, error: '' });
    fetch(`${API_BASE_URL}/api/v1/clinic-recall/monitor`)
      .then(readJson)
      .then((payload) => {
        if (!cancelled) setMonitorStatus({ busy: false, data: payload, error: '' });
      })
      .catch((error) => {
        if (!cancelled) setMonitorStatus({ busy: false, data: null, error: error.message });
      });
    return () => { cancelled = true; };
  }, [activePanel]);

  useEffect(() => {
    if (activePanel !== 'agent' || !isOperator) return;
    let cancelled = false;
    fetch(`${API_BASE_URL}/api/v1/clinic-recall/operator/prompt-proposals`)
      .then(readJson)
      .then((payload) => {
        if (!cancelled) setPromptProposals(payload.proposals || []);
      })
      .catch((error) => {
        if (!cancelled) setProposalStatus((current) => ({ ...current, error: error.message }));
      });
    return () => { cancelled = true; };
  }, [activePanel, isOperator]);

  useEffect(() => {
    if (activePanel !== 'scripts' || !isOperator) return;
    let cancelled = false;
    setScriptTemplatesLoaded(false);
    setScriptStatus({ busy: true, message: '', error: '' });
    fetch(`${API_BASE_URL}/api/v1/clinic-recall/operator/script-templates`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`Scripts failed (${response.status})`)))
      .then((payload) => {
        if (!cancelled) {
          setScriptTemplates(payload.templates || {});
          setScriptTemplatesLoaded(true);
          setScriptStatus({ busy: false, message: '', error: '' });
        }
      })
      .catch((error) => {
        if (!cancelled) setScriptStatus({ busy: false, message: '', error: error.message });
      });
    return () => {
      cancelled = true;
      setScriptTemplatesLoaded(false);
    };
  }, [activePanel, isOperator]);

  useEffect(() => {
    if (activePanel !== 'voice' || !isOperator) return;
    let cancelled = false;
    fetch(`${API_BASE_URL}/api/v1/clinic-recall/operator/voice-persona`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`Voice persona failed (${response.status})`)))
      .then((payload) => {
        if (!cancelled) setVoicePersona(payload);
      })
      .catch((error) => {
        if (!cancelled) setVoicePersonaStatus({ busy: false, message: '', error: error.message });
      });
    return () => { cancelled = true; };
  }, [activePanel, isOperator]);

  const runVoiceFallback = async () => {
    setVoiceStatus({ busy: true, message: '', error: '' });
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/voice/fallback/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || payload.error || `Voice fallback failed (${response.status})`);
      }
      const summary = payload.voice_fallback || {};
      setVoiceStatus({
        busy: false,
        message: `Voice fallback queued ${summary.calls_initiated || 0} call(s).`,
        error: '',
      });
    } catch (error) {
      setVoiceStatus({ busy: false, message: '', error: error.message });
    }
  };

  const saveScripts = async (event) => {
    event.preventDefault();
    setScriptStatus({ busy: true, message: '', error: '' });
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/operator/script-templates`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ templates: scriptTemplates }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || payload.error || `Scripts save failed (${response.status})`);
      }
      setScriptTemplates(payload.templates || {});
      setScriptStatus({ busy: false, message: 'Script templates saved.', error: '' });
    } catch (error) {
      setScriptStatus({ busy: false, message: '', error: error.message });
    }
  };

  const saveVoicePersona = async (event) => {
    event.preventDefault();
    setVoicePersonaStatus({ busy: true, message: '', error: '' });
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/operator/voice-persona`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(voicePersona),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || payload.error || `Voice persona save failed (${response.status})`);
      }
      setVoicePersona(payload);
      setVoicePersonaStatus({ busy: false, message: 'Voice persona saved.', error: '' });
    } catch (error) {
      setVoicePersonaStatus({ busy: false, message: '', error: error.message });
    }
  };

  const generatePromptProposal = async (event) => {
    event.preventDefault();
    setProposalStatus({ busy: true, diff: '', message: '', error: '' });
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/operator/prompt-proposals`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ proposed_prompt: promptProposal }),
      });
      const payload = await readJson(response);
      setProposalStatus({ busy: false, diff: payload.diff || 'No changes detected.', message: 'Prompt proposal submitted for AgentOps-gated review.', error: '' });
      setPromptProposals((current) => [payload, ...current.filter((proposal) => proposal.id !== payload.id)]);
    } catch (error) {
      setProposalStatus({ busy: false, diff: '', message: '', error: error.message });
    }
  };

  if (!isOperator && ['pilot', 'scripts', 'voice', 'agent', 'system'].includes(activePanel)) {
    return <LockedPanel activePanel={activePanel} />;
  }

  if (activePanel === 'system') {
    return (
      <section className="shell-panel" aria-labelledby="system-title">
        <p className="shell-eyebrow">Operator</p>
        <h2 id="system-title">System health</h2>
        <div className="shell-embedded-health">
          <BackendIndicator url={API_BASE_URL} compact />
        </div>
      </section>
    );
  }
  const content = PANEL_COPY[activePanel] || PANEL_COPY.monitor;
  return (
    <section className="shell-panel" aria-labelledby={`${activePanel}-title`}>
      <p className="shell-eyebrow">Operator</p>
      <h2 id={`${activePanel}-title`}>{content.title}</h2>
      <p>{content.body}</p>
      {activePanel === 'monitor' ? (
        <div className="shell-action-stack">
          {monitorStatus.busy ? <p>Loading clinic status...</p> : null}
          {monitorStatus.data ? (
            <div className="setup-steps" aria-label="Clinic status metrics">
              <article className="setup-step"><strong>{monitorStatus.data.open_queue_count}</strong><div><h3>Open queue</h3><p>Escalations and pending booking actions.</p></div></article>
              <article className="setup-step"><strong>{monitorStatus.data.queued_outbox_count}</strong><div><h3>Queued outbox</h3><p>Outreach waiting for cadence or approval.</p></div></article>
              <article className="setup-step"><strong>{monitorStatus.data.active_campaigns}</strong><div><h3>Active campaigns</h3><p>Campaigns currently eligible for send cadence.</p></div></article>
              <article className="setup-step"><strong>{monitorStatus.data.recent_interactions_count}</strong><div><h3>Recent interactions</h3><p>Aggregate activity in the last 24 hours.</p></div></article>
            </div>
          ) : null}
          {monitorStatus.data?.voice_fallback_summary ? (
            <div className="shell-action-card">
              <strong>Voice fallback</strong>
              <p>{Object.entries(monitorStatus.data.voice_fallback_summary.call_jobs_by_state || {}).map(([state, count]) => `${state}: ${count}`).join(' · ') || 'No call jobs yet'}</p>
            </div>
          ) : null}
          {monitorStatus.error ? <div className="shell-inline-error" role="alert">{monitorStatus.error}</div> : null}
        </div>
      ) : null}
      {activePanel === 'pilot' ? (
        <div className="shell-action-stack">
          <form className="shell-action-card shell-settings-form" onSubmit={createPilot}>
            <label>Programme ID<input value={pilotDraft.programme_id} onChange={(event) => setPilotDraft((current) => ({ ...current, programme_id: event.target.value }))} required /></label>
            <label>Environment<input value={pilotDraft.environment} onChange={(event) => setPilotDraft((current) => ({ ...current, environment: event.target.value }))} required /></label>
            <label>Release identity<input value={pilotDraft.release_identity} onChange={(event) => setPilotDraft((current) => ({ ...current, release_identity: event.target.value }))} required /></label>
            <button type="submit" className="shell-primary-button" disabled={pilotStatus.busy}>Create programme</button>
          </form>
          {pilotStatus.programmes.map((programme) => (
            <article className="shell-action-card shell-settings-form" key={programme.id}>
              <strong>{programme.id} · {programme.state}</strong>
              <p>{programme.released_count}/{programme.participant_count} released · cumulative limit {programme.active_cumulative_limit}</p>
              <label>Release evidence SHA-256<input value={pilotDraft.evidence_hash} onChange={(event) => setPilotDraft((current) => ({ ...current, evidence_hash: event.target.value }))} /></label>
              <div className="shell-action-row">
                <button type="button" className="shell-primary-button" onClick={() => releasePilot(programme)} disabled={pilotStatus.busy || !hasValidPilotEvidence || !canReleasePilotWave(programme)}>Release next wave</button>
                <button type="button" className="shell-secondary-button" onClick={() => darkPilot(programme)} disabled={pilotStatus.busy || !hasValidPilotEvidence || programme.state !== 'draft'}>Enter dark</button>
                <button type="button" className="shell-secondary-button" onClick={() => pausePilot(programme)} disabled={pilotStatus.busy || programme.state === 'paused'}>Pause</button>
                <button type="button" className="shell-secondary-button" onClick={() => closePilot(programme)} disabled={pilotStatus.busy || programme.state !== 'paused'}>Close</button>
              </div>
            </article>
          ))}
          {pilotStatus.busy ? <p>Loading pilot controls...</p> : null}
          {pilotStatus.message ? <output className="shell-inline-success">{pilotStatus.message}</output> : null}
          {pilotStatus.error ? <div className="shell-inline-error" role="alert">{pilotStatus.error}</div> : null}
        </div>
      ) : null}
      {activePanel === 'voice' ? (
        <div className="shell-action-stack">
        <form className="shell-action-card shell-settings-form" onSubmit={saveVoicePersona}>
          <label>
            Display name
            <input
              value={voicePersona.display_name || ''}
              onChange={(event) => setVoicePersona((current) => ({ ...current, display_name: event.target.value }))}
              required
            />
          </label>
          <label>
            Tone
            <input
              value={voicePersona.tone || ''}
              onChange={(event) => setVoicePersona((current) => ({ ...current, tone: event.target.value }))}
              required
            />
          </label>
          <label>
            Voice name
            <input
              value={voicePersona.voice_name || ''}
              onChange={(event) => setVoicePersona((current) => ({ ...current, voice_name: event.target.value }))}
            />
          </label>
          <button type="submit" className="shell-primary-button" disabled={voicePersonaStatus.busy}>
            {voicePersonaStatus.busy ? 'Saving voice persona...' : 'Save voice persona'}
          </button>
          {voicePersonaStatus.message ? <output className="shell-inline-success">{voicePersonaStatus.message}</output> : null}
          {voicePersonaStatus.error ? <div className="shell-inline-error" role="alert">{voicePersonaStatus.error}</div> : null}
        </form>
        <div className="shell-action-card">
          <button type="button" className="shell-primary-button" onClick={runVoiceFallback} disabled={voiceStatus.busy}>
            {voiceStatus.busy ? 'Starting voice fallback...' : 'Run voice fallback'}
          </button>
          {voiceStatus.message ? <output className="shell-inline-success">{voiceStatus.message}</output> : null}
          {voiceStatus.error ? <div className="shell-inline-error" role="alert">{voiceStatus.error}</div> : null}
        </div>
        </div>
      ) : null}
      {activePanel === 'scripts' ? (
        <form className="shell-action-card shell-proposal-form" onSubmit={saveScripts}>
          {['missed', 'overdue', 'feedback'].map((templateKey) => (
            <label key={templateKey}>
              {templateKey} script
              <textarea
                value={scriptTemplates[templateKey] || ''}
                onChange={(event) => setScriptTemplates((current) => ({ ...current, [templateKey]: event.target.value }))}
                disabled={!scriptTemplatesLoaded || scriptStatus.busy}
                required
                minLength={5}
              />
            </label>
          ))}
          <button type="submit" className="shell-primary-button" disabled={!scriptTemplatesLoaded || scriptStatus.busy}>
            {scriptStatus.busy && !scriptTemplatesLoaded ? 'Loading scripts...' : scriptStatus.busy ? 'Saving scripts...' : 'Save script templates'}
          </button>
          {scriptStatus.message ? <output className="shell-inline-success">{scriptStatus.message}</output> : null}
          {scriptStatus.error ? <div className="shell-inline-error" role="alert">{scriptStatus.error}</div> : null}
        </form>
      ) : null}
      {activePanel === 'agent' ? (
        <form className="shell-action-card shell-proposal-form" onSubmit={generatePromptProposal}>
          <label>
            Proposed Recall Agent prompt
            <textarea
              value={promptProposal}
              onChange={(event) => setPromptProposal(event.target.value)}
              placeholder="Paste the full revised prompt here. The app will generate a diff only; AgentOps review and gates remain required."
              required
              minLength={20}
            />
          </label>
          <button type="submit" className="shell-primary-button" disabled={proposalStatus.busy}>
            {proposalStatus.busy ? 'Submitting proposal...' : 'Generate gated diff'}
          </button>
          <p>AgentOps eval gate required before this ships.</p>
          {proposalStatus.message ? <output className="shell-inline-success">{proposalStatus.message}</output> : null}
          {proposalStatus.diff ? <pre className="shell-diff-output" aria-label="Prompt proposal diff">{proposalStatus.diff}</pre> : null}
          {proposalStatus.error ? <div className="shell-inline-error" role="alert">{proposalStatus.error}</div> : null}
          {promptProposals.length ? (
            <div className="shell-action-stack" aria-label="Submitted prompt proposals">
              {promptProposals.map((proposal) => (
                <article key={proposal.id} className="shell-action-card">
                  <strong>{proposal.status} · {proposal.actor}</strong>
                  <pre className="shell-diff-output">{proposal.diff}</pre>
                </article>
              ))}
            </div>
          ) : null}
        </form>
      ) : null}
    </section>
  );
}

export default function ProductShell({ children }) {
  const identity = useShellIdentity();
  const [activeView, setActiveView] = useState('dashboard');
  const isOperator = useMemo(
    () => Array.from(identity.roles).some((role) => OPERATOR_ROLES.has(role)),
    [identity.roles],
  );

  return (
    <div className="app-shell">
      <header className="shell-topbar">
        <div>
          <span className="shell-product">Clinic Recall</span>
          <strong>Wulo-X control room</strong>
        </div>
        <RoleBadge identity={identity} />
      </header>

      <aside className="shell-rail" aria-label="Clinic Recall app navigation">
        {RAIL_ITEMS.map((item) => {
          const Icon = item.icon;
          const locked = item.gated && !isOperator;
          return (
            <button
              key={item.id}
              type="button"
              className={`${activeView === item.id ? 'active' : ''} ${locked ? 'shell-rail-locked' : ''}`}
              onClick={() => setActiveView(item.id)}
              data-testid={item.gated ? `operator-tool-${item.id === 'system' ? 'system' : item.id}` : undefined}
              aria-label={locked ? `${item.label} - operator required` : item.label}
            >
              <Icon fontSize="small" />
              <span>{item.label}</span>
              {locked ? <small>Locked</small> : null}
            </button>
          );
        })}
        <div className="shell-help"><HelpButton /></div>
      </aside>

      <main className="shell-main">
        {activeView === 'dashboard' ? children : null}
        {activeView === 'setup' ? <SetupPanel isOperator={isOperator} /> : null}
        {['monitor', 'pilot', 'scripts', 'voice', 'agent', 'system'].includes(activeView) ? (
          <OperatorPanel activePanel={activeView} isOperator={isOperator} />
        ) : null}
      </main>
    </div>
  );
}
