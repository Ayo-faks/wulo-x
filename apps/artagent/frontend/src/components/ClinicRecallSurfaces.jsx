import React, { useCallback, useEffect, useMemo, useState } from 'react';
import ApprovalRoundedIcon from '@mui/icons-material/ApprovalRounded';
import AssessmentRoundedIcon from '@mui/icons-material/AssessmentRounded';
import BlockRoundedIcon from '@mui/icons-material/BlockRounded';
import CampaignRoundedIcon from '@mui/icons-material/CampaignRounded';
import CloseRoundedIcon from '@mui/icons-material/CloseRounded';
import DownloadRoundedIcon from '@mui/icons-material/DownloadRounded';
import DoneRoundedIcon from '@mui/icons-material/DoneRounded';
import InboxRoundedIcon from '@mui/icons-material/InboxRounded';
import LocalPhoneRoundedIcon from '@mui/icons-material/LocalPhoneRounded';
import OutboxRoundedIcon from '@mui/icons-material/OutboxRounded';
import PauseCircleRoundedIcon from '@mui/icons-material/PauseCircleRounded';
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded';
import ReportProblemRoundedIcon from '@mui/icons-material/ReportProblemRounded';
import RocketLaunchRoundedIcon from '@mui/icons-material/RocketLaunchRounded';
import SendRoundedIcon from '@mui/icons-material/SendRounded';
import SettingsRoundedIcon from '@mui/icons-material/SettingsRounded';
import TaskAltRoundedIcon from '@mui/icons-material/TaskAltRounded';
import { API_BASE_URL } from '../config/constants.js';
import './ClinicRecallSurfaces.css';

const CONTROL_TABS = [
  { id: 'inbox', label: 'Inbox', icon: InboxRoundedIcon },
  { id: 'phone', label: 'Phone', icon: LocalPhoneRoundedIcon },
  { id: 'outbox', label: 'Outbox', icon: OutboxRoundedIcon },
  { id: 'campaigns', label: 'Campaigns', icon: CampaignRoundedIcon },
  { id: 'sent', label: 'Sent', icon: SendRoundedIcon },
  { id: 'incidents', label: 'Incidents', icon: ReportProblemRoundedIcon },
  { id: 'roi', label: 'ROI', icon: AssessmentRoundedIcon },
  { id: 'settings', label: 'Settings', icon: SettingsRoundedIcon },
];

const INCIDENT_CATEGORIES = [
  { value: 'patient_safety', label: 'Patient safety' },
  { value: 'near_miss', label: 'Near miss' },
  { value: 'communication_failure', label: 'Communication failure' },
  { value: 'wrong_patient_contacted', label: 'Wrong patient contacted' },
  { value: 'data_privacy_concern', label: 'Data / privacy concern' },
  { value: 'agent_behaviour', label: 'Agent behaviour' },
  { value: 'other', label: 'Other' },
];

const INCIDENT_SEVERITIES = [
  { value: 'no_harm', label: 'No harm' },
  { value: 'low', label: 'Low' },
  { value: 'moderate', label: 'Moderate' },
  { value: 'severe', label: 'Severe' },
];

const INCIDENT_NEXT_STATUS = {
  new: [{ value: 'under_review', label: 'Start review' }, { value: 'closed', label: 'Close' }],
  under_review: [{ value: 'actioned', label: 'Mark actioned' }, { value: 'closed', label: 'Close' }],
  actioned: [{ value: 'closed', label: 'Close' }],
  closed: [],
};

const monthStart = () => {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString().slice(0, 10);
};

const monthEnd = () => {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)).toISOString().slice(0, 10);
};

const toPeriodInstant = (value) => `${value}T00:00:00Z`;

const money = (value) => `£${Number(value || 0).toLocaleString('en-GB', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})}`;

const percent = (value) => `${Math.round(Number(value || 0) * 100)}%`;

/** Turn raw enum values like `under_review` into display text (`under review`). */
const formatEnumLabel = (value) => String(value ?? '').replaceAll('_', ' ');

const slotLabel = (value) => (value ? new Date(value).toLocaleString() : 'staff follow-up');

const dateTimeLabel = (value) => (value ? new Date(value).toLocaleString() : 'not scheduled');

const campaignStatusTone = (status) => {
  if (status === 'active') return 'active';
  if (status === 'paused') return 'paused';
  if (status === 'draft') return 'draft';
  return 'default';
};

async function readJson(response) {
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `Request failed with ${response.status}`);
  }
  return payload;
}

function MetricTile({ label, value, detail, tone = 'default' }) {
  return (
    <div className={`cr-metric-tile cr-metric-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </div>
  );
}

function ControlTab({ tab, active, count, onSelect }) {
  const Icon = tab.icon;
  return (
    <button
      type="button"
      className={`cr-tab ${active ? 'cr-tab-active' : ''}`}
      onClick={() => onSelect(tab.id)}
      aria-pressed={active}
    >
      <Icon fontSize="small" />
      <span>{tab.label}</span>
      {typeof count === 'number' ? <strong className="cr-tab-count">{count}</strong> : null}
    </button>
  );
}

function QueueItem({ item, onAcknowledge, onResolve, busy }) {
  const canApprove = item.kind === 'booking_action';
  const itemType = canApprove ? 'approval' : item.kind === 'external_effect_handoff' ? 'operational effect' : 'escalation';
  return (
    <article className={`cr-queue-item cr-priority-${item.priority} ${item.overdue ? 'cr-queue-overdue' : ''}`} data-testid="clinic-queue-item">
      <div className="cr-priority-rail" aria-hidden="true">
        <span />
      </div>
      <div className="cr-queue-main">
        <div className="cr-queue-heading">
          <div>
            <h3>{item.patient_name}</h3>
            <p>{item.context_summary}</p>
          </div>
          <div className="cr-chip-row" aria-label="Queue item status">
            <span className={`cr-status-pill cr-priority-pill cr-priority-pill-${item.severity || item.priority}`}>{formatEnumLabel(item.severity || item.priority)}</span>
            <span className="cr-status-pill">{formatEnumLabel(item.delivery_state)}</span>
            <span className="cr-status-pill">{itemType}</span>
            {item.overdue ? <span className="cr-status-pill cr-status-overdue">Overdue</span> : null}
            {item.alternate_requested ? <span className="cr-status-pill cr-status-blocked">Alternate requested</span> : null}
          </div>
        </div>
        <dl className="cr-queue-meta">
          <div>
            <dt>Reason</dt>
            <dd>{formatEnumLabel(item.reason)}</dd>
          </div>
          <div>
            <dt>Owner state</dt>
            <dd>{formatEnumLabel(item.status)}</dd>
          </div>
          <div>
            <dt>Due</dt>
            <dd>{dateTimeLabel(item.due_at)}</dd>
          </div>
          <div>
            <dt>Acknowledged</dt>
            <dd>{item.acknowledged_at ? `${item.acknowledged_by || 'staff'} · ${dateTimeLabel(item.acknowledged_at)}` : 'Awaiting owner'}</dd>
          </div>
          <div>
            <dt>{canApprove ? 'Slot' : 'Resolution'}</dt>
            <dd>{canApprove ? slotLabel(item.slot_start) : item.owner_resolved ? 'Resolved' : 'Open'}</dd>
          </div>
        </dl>
      </div>
      <div className="cr-queue-actions">
        {!item.acknowledged_at ? (
          <button type="button" className="cr-action cr-action-ack" onClick={() => onAcknowledge(item.item_id)} disabled={busy}>
            <DoneRoundedIcon fontSize="small" />
            Acknowledge
          </button>
        ) : null}
        {canApprove ? (
          <button type="button" className="cr-action cr-action-approve" onClick={() => onResolve(item.item_id, 'approve')} disabled={busy}>
            <ApprovalRoundedIcon fontSize="small" />
            Approve
          </button>
        ) : null}
        <button type="button" className={`cr-action ${canApprove ? 'cr-action-reject' : 'cr-action-resolve'}`} onClick={() => onResolve(item.item_id, canApprove ? 'reject' : 'resolve')} disabled={busy}>
          {canApprove ? <BlockRoundedIcon fontSize="small" /> : <TaskAltRoundedIcon fontSize="small" />}
          {canApprove ? 'Reject' : 'Resolve'}
        </button>
      </div>
    </article>
  );
}

function OutboxItem({ item }) {
  return (
    <article className={`cr-outbox-item ${item.eligible_now ? 'cr-outbox-ready' : 'cr-outbox-blocked'}`}>
      <div className="cr-outbox-head">
        <div>
          <h3>{item.patient_name}</h3>
          <p>{formatEnumLabel(item.reason_code) || 'recall'} · {item.channel}</p>
        </div>
        <span className={`cr-status-pill cr-status-${item.eligible_now ? 'ready' : 'blocked'}`}>
          {item.eligible_now ? 'eligible' : formatEnumLabel(item.skip_reason) || 'blocked'}
        </span>
      </div>
      <p className="cr-message-preview">{item.message_preview || 'Template preview unavailable.'}</p>
      <dl className="cr-outbox-meta">
        <div>
          <dt>Template</dt>
          <dd>{item.template_id || 'none'}</dd>
        </div>
        <div>
          <dt>Campaign</dt>
          <dd>{item.campaign_status}</dd>
        </div>
        <div>
          <dt>Scheduled</dt>
          <dd>{dateTimeLabel(item.scheduled_for)}</dd>
        </div>
      </dl>
    </article>
  );
}

function SentItem({ item }) {
  return (
    <article className="cr-sent-item">
      <div className="cr-sent-head">
        <div>
          <h3>{item.channel} · {item.direction}</h3>
          <p>{dateTimeLabel(item.occurred_at)}</p>
        </div>
        <span className="cr-status-pill">{formatEnumLabel(item.outcome || item.intent) || 'logged'}</span>
      </div>
      <dl className="cr-sent-meta">
        <div>
          <dt>Job</dt>
          <dd>{item.outreach_job_id}</dd>
        </div>
        <div>
          <dt>Template</dt>
          <dd>{item.template_id || 'hidden'}</dd>
        </div>
      </dl>
      {item.content_preview ? <p className="cr-message-preview">{item.content_preview}</p> : null}
    </article>
  );
}

function PhoneNumberItem({ item }) {
  return (
    <article className="cr-phone-number-item">
      <div>
        <h3>{item.phone_number}</h3>
        <p>{item.provider} · {item.purpose}</p>
      </div>
      <div className="cr-chip-row">
        <span className={`cr-status-pill cr-status-${item.status === 'active' ? 'ready' : 'blocked'}`}>{formatEnumLabel(item.status)}</span>
        <span className="cr-status-pill">{formatEnumLabel(item.test_status) || 'not tested'}</span>
      </div>
      <small>{item.webhook_url || 'provider webhook pending'}</small>
    </article>
  );
}

function InboundTaskItem({ item, onAcknowledge, onResolve, busy }) {
  const sourceLabel = item.source === 'sms' || item.inbound_message_id ? 'Text' : 'Call';
  const sourceId = item.inbound_message_id || item.inbound_call_id || 'unlinked';
  return (
    <article className={`cr-inbound-task cr-priority-${item.priority}`}>
      <div className="cr-queue-heading">
        <div>
          <h3>{item.kind.replaceAll('_', ' ')}</h3>
          <p>{item.summary || item.reason || 'Staff follow-up required.'}</p>
        </div>
        <div className="cr-chip-row">
          <span className={`cr-status-pill cr-priority-pill cr-priority-pill-${item.severity || item.priority}`}>{formatEnumLabel(item.severity || item.priority)}</span>
          <span className="cr-status-pill">{formatEnumLabel(item.delivery_state)}</span>
          <span className="cr-status-pill">{formatEnumLabel(item.status)}</span>
          {item.overdue ? <span className="cr-status-pill cr-status-overdue">Overdue</span> : null}
        </div>
      </div>
      <dl className="cr-sent-meta">
        <div>
          <dt>Reason</dt>
          <dd>{formatEnumLabel(item.reason) || 'callback'}</dd>
        </div>
        <div>
          <dt>{sourceLabel}</dt>
          <dd>{sourceId}</dd>
        </div>
        <div>
          <dt>Due</dt>
          <dd>{dateTimeLabel(item.due_at)}</dd>
        </div>
        <div>
          <dt>Acknowledged</dt>
          <dd>{item.acknowledged_at ? `${item.acknowledged_by || 'staff'} · ${dateTimeLabel(item.acknowledged_at)}` : 'Awaiting owner'}</dd>
        </div>
      </dl>
      {item.status !== 'resolved' ? (
        <div className="cr-queue-actions cr-inbound-task-actions">
          {!item.acknowledged_at ? (
            <button type="button" className="cr-action cr-action-ack" onClick={() => onAcknowledge(item.id)} disabled={busy}>
              <DoneRoundedIcon fontSize="small" /> Acknowledge
            </button>
          ) : null}
          <button type="button" className="cr-action cr-action-resolve" onClick={() => onResolve(item.id)} disabled={busy}>
            <TaskAltRoundedIcon fontSize="small" /> Resolve
          </button>
        </div>
      ) : null}
    </article>
  );
}

function InboundMessageItem({ item }) {
  return (
    <article className="cr-sent-item">
      <div className="cr-sent-head">
        <div>
          <h3>{item.provider} · {formatEnumLabel(item.intent) || 'inbound text'}</h3>
          <p>{dateTimeLabel(item.created_at)}</p>
        </div>
        <span className="cr-status-pill">{formatEnumLabel(item.status)}</span>
      </div>
      <dl className="cr-sent-meta">
        <div>
          <dt>Sender</dt>
          <dd>{item.from_number_redacted}</dd>
        </div>
        <div>
          <dt>Summary</dt>
          <dd>{item.summary || 'Staff review pending'}</dd>
        </div>
      </dl>
    </article>
  );
}

function CampaignRow({ campaign, onReview, onPause, busy }) {
  return (
    <div className="cr-campaign-row">
      <div>
        <span>{campaign.type}</span>
        <strong>{campaign.id}</strong>
      </div>
      <strong className={`cr-campaign-status cr-campaign-status-${campaignStatusTone(campaign.status)}`}>
        {campaign.status}
      </strong>
      <small>{campaign.jobs} jobs</small>
      <div className="cr-campaign-row-actions">
        {campaign.is_approvable ? (
          <button type="button" className="cr-action cr-action-approve" onClick={() => onReview(campaign.id)} disabled={busy}>
            <ApprovalRoundedIcon fontSize="small" /> Review
          </button>
        ) : null}
        {campaign.is_pausable ? (
          <button type="button" className="cr-action" onClick={() => onPause(campaign.id)} disabled={busy}>
            <PauseCircleRoundedIcon fontSize="small" /> Pause
          </button>
        ) : null}
      </div>
    </div>
  );
}

function CampaignReviewModal({ campaign, items, onApprove, onClose, busy }) {
  if (!campaign) return null;
  const eligible = items.filter((item) => item.eligible_now).length;
  const blocked = items.length - eligible;
  return (
    <div className="cr-modal-backdrop" role="presentation">
      <dialog className="cr-review-modal" aria-labelledby="cr-review-title" open>
        <header className="cr-panel-title cr-panel-title-split">
          <div>
            <span>Outbox review</span>
            <h2 id="cr-review-title">Approve {campaign.type} campaign</h2>
          </div>
          <button type="button" className="cr-icon-button cr-icon-button-muted" onClick={onClose} aria-label="Close campaign review">
            <CloseRoundedIcon fontSize="small" />
          </button>
        </header>
        <div className="cr-review-summary">
          <MetricTile label="Queued" value={items.length} />
          <MetricTile label="Eligible now" value={eligible} tone="money" />
          <MetricTile label="Blocked" value={blocked} tone={blocked ? 'blocked' : 'default'} />
        </div>
        <div className="cr-review-list">
          {items.length ? items.slice(0, 4).map((item) => <OutboxItem key={item.item_id} item={item} />) : <div className="cr-empty">No queued messages for this campaign.</div>}
        </div>
        <footer className="cr-modal-actions">
          <button type="button" className="cr-action" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="button" className="cr-action cr-action-approve" onClick={() => onApprove(campaign.id)} disabled={busy || !items.length}>
            <ApprovalRoundedIcon fontSize="small" /> Approve campaign
          </button>
        </footer>
      </dialog>
    </div>
  );
}

export default function ClinicRecallSurfaces() {
  const [activeTab, setActiveTab] = useState('inbox');
  const [queue, setQueue] = useState([]);
  const [outbox, setOutbox] = useState([]);
  const [sent, setSent] = useState([]);
  const [metrics, setMetrics] = useState(null);
  const [settings, setSettings] = useState(null);
  const [phoneNumbers, setPhoneNumbers] = useState([]);
  const [inboundCalls, setInboundCalls] = useState([]);
  const [inboundMessages, setInboundMessages] = useState([]);
  const [inboundTasks, setInboundTasks] = useState([]);
  const [inboundMetrics, setInboundMetrics] = useState(null);
  const [inboundConfig, setInboundConfig] = useState(null);
  const [campaigns, setCampaigns] = useState([]);
  const [incidents, setIncidents] = useState([]);
  const [incidentDraft, setIncidentDraft] = useState({ category: 'other', severity: 'no_harm', description: '' });
  const [launchSummary, setLaunchSummary] = useState(null);
  const [reviewCampaignId, setReviewCampaignId] = useState('');
  const [start, setStart] = useState(monthStart);
  const [end, setEnd] = useState(monthEnd);
  const [loading, setLoading] = useState(false);
  const [busyItem, setBusyItem] = useState(null);
  const [busyAction, setBusyAction] = useState(null);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  const csvUrl = useMemo(() => {
    const params = new URLSearchParams({ start: toPeriodInstant(start), end: toPeriodInstant(end) });
    return `${API_BASE_URL}/api/v1/clinic-recall/roi.csv?${params.toString()}`;
  }, [end, start]);

  const outboxEligible = outbox.filter((item) => item.eligible_now).length;
  const outboxBlocked = outbox.length - outboxEligible;
  const reviewCampaign = campaigns.find((campaign) => campaign.id === reviewCampaignId) || null;
  const reviewItems = reviewCampaign ? outbox.filter((item) => item.campaign_id === reviewCampaign.id) : [];
  const tabCounts = {
    inbox: queue.length,
    phone: inboundTasks.length,
    outbox: outbox.length,
    campaigns: campaigns.length,
    sent: sent.length,
    incidents: incidents.filter((item) => item.status === 'new' || item.status === 'under_review').length,
  };

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const period = new URLSearchParams({ start: toPeriodInstant(start), end: toPeriodInstant(end) });
      const outboxPeriod = new URLSearchParams({ now: new Date().toISOString() });
      const [queuePayload, roiPayload, settingsPayload, campaignsPayload, outboxPayload, sentPayload, phonePayload, inboundCallsPayload, inboundMessagesPayload, inboundTasksPayload, inboundMetricsPayload, inboundConfigPayload, incidentsPayload] = await Promise.all([
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/inbox`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/roi?${period.toString()}`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/campaign/settings`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/campaigns`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/outbox?${outboxPeriod.toString()}`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/interactions`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/phone-numbers`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/inbound-calls`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/inbound-messages`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/inbound-tasks`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/inbound-metrics`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/inbound-config`).then(readJson),
        fetch(`${API_BASE_URL}/api/v1/clinic-recall/incidents`).then(readJson),
      ]);
      setQueue(queuePayload.items || []);
      setMetrics(roiPayload);
      setSettings(settingsPayload);
      setCampaigns(campaignsPayload.campaigns || []);
      setOutbox(outboxPayload.items || []);
      setSent(sentPayload.items || []);
      setPhoneNumbers(phonePayload.items || []);
      setInboundCalls(inboundCallsPayload.items || []);
      setInboundMessages(inboundMessagesPayload.items || []);
      setInboundTasks(inboundTasksPayload.items || []);
      setInboundMetrics(inboundMetricsPayload);
      setInboundConfig(inboundConfigPayload);
      setIncidents(incidentsPayload.items || []);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [end, start]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const acknowledgeInboundTask = useCallback(async (taskId) => {
    setBusyItem(taskId);
    setError('');
    setNotice('');
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/inbound-tasks/${encodeURIComponent(taskId)}/acknowledge`, {
        method: 'POST',
      });
      await readJson(response);
      setNotice(`Acknowledged inbound task ${taskId}.`);
      await loadAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyItem(null);
    }
  }, [loadAll]);

  const resolveInboundTask = useCallback(async (taskId) => {
    setBusyItem(taskId);
    setError('');
    setNotice('');
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/inbound-tasks/${encodeURIComponent(taskId)}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'resolved', reason: 'Resolved from Phone surface.' }),
      });
      await readJson(response);
      setNotice(`Resolved inbound task ${taskId}.`);
      await loadAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyItem(null);
    }
  }, [loadAll]);

  const submitIncident = useCallback(async () => {
    if (!incidentDraft.description.trim()) {
      setError('Describe what happened before submitting the incident report.');
      return;
    }
    setBusyAction('incident-submit');
    setError('');
    setNotice('');
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/incidents`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          category: incidentDraft.category,
          severity: incidentDraft.severity,
          description: incidentDraft.description.trim(),
        }),
      });
      await readJson(response);
      setIncidentDraft({ category: 'other', severity: 'no_harm', description: '' });
      setNotice('Anonymous incident report recorded for governance review.');
      await loadAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyAction(null);
    }
  }, [incidentDraft, loadAll]);

  const advanceIncident = useCallback(async (incidentId, status) => {
    setBusyItem(incidentId);
    setError('');
    setNotice('');
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/incidents/${encodeURIComponent(incidentId)}/status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      });
      await readJson(response);
      setNotice(`Incident moved to ${formatEnumLabel(status)}.`);
      await loadAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyItem(null);
    }
  }, [loadAll]);

  const saveInboundConfig = useCallback(async () => {
    if (!inboundConfig) return;
    setLoading(true);
    setError('');
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/inbound-config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          greeting: inboundConfig.greeting,
          callback_sla_hours: Number(inboundConfig.callback_sla_hours || 4),
          escalation_destination: inboundConfig.escalation_destination || null,
          recording_enabled: Boolean(inboundConfig.recording_enabled),
        }),
      });
      setInboundConfig(await readJson(response));
      setNotice('Inbound phone settings saved.');
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [inboundConfig]);

  const acknowledgeItem = useCallback(async (itemId) => {
    setBusyItem(itemId);
    setError('');
    setNotice('');
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/v1/clinic-recall/inbox/${encodeURIComponent(itemId)}/acknowledge`,
        { method: 'POST' },
      );
      await readJson(response);
      setNotice(`Acknowledged ${itemId}.`);
      await loadAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyItem(null);
    }
  }, [loadAll]);

  const resolveItem = useCallback(async (itemId, decision) => {
    setBusyItem(itemId);
    setError('');
    setNotice('');
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/v1/clinic-recall/queue/${encodeURIComponent(itemId)}/resolve`,
        {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, reason: decision === 'reject' ? 'Resolved by staff from dashboard.' : undefined }),
        },
      );
      const payload = await readJson(response);
      setNotice(`${decision === 'approve' ? 'Approved' : 'Resolved'} ${itemId}: ${payload.booking_status || payload.escalation_status || payload.external_effect_handoff_status || 'done'}`);
      await loadAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyItem(null);
    }
  }, [loadAll]);

  const saveSettings = useCallback(async () => {
    if (!settings) return;
    setLoading(true);
    setError('');
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/campaign/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          daily_caps: Number(settings.daily_caps || 1),
          contact_hours: settings.contact_hours || {},
          branding: settings.branding || {},
        }),
      });
      setSettings(await readJson(response));
      setNotice('Campaign settings saved.');
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [settings]);

  const launchCampaign = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/campaigns/launch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel: 'sms' }),
      });
      const payload = await readJson(response);
      setLaunchSummary(payload.candidate_queue);
      setActiveTab('outbox');
      setNotice('Recovery campaign refreshed for review.');
      await loadAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [loadAll]);

  const openCampaignReview = useCallback((campaignId) => {
    const fallback = campaigns.find((campaign) => campaign.is_approvable)?.id;
    const nextCampaignId = campaignId || fallback || '';
    if (nextCampaignId) {
      setReviewCampaignId(nextCampaignId);
      setActiveTab('outbox');
    }
  }, [campaigns]);

  const approveCampaign = useCallback(async (campaignId) => {
    setBusyAction(`approve:${campaignId}`);
    setError('');
    setNotice('');
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/campaigns/${encodeURIComponent(campaignId)}/approve`, { method: 'POST' });
      const payload = await readJson(response);
      setNotice(`Campaign ${payload.id} approved.`);
      setReviewCampaignId('');
      await loadAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyAction(null);
    }
  }, [loadAll]);

  const pauseCampaign = useCallback(async (campaignId) => {
    setBusyAction(`pause:${campaignId}`);
    setError('');
    setNotice('');
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/campaigns/${encodeURIComponent(campaignId)}/pause`, { method: 'POST' });
      const payload = await readJson(response);
      setNotice(`Campaign ${payload.id} paused.`);
      await loadAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyAction(null);
    }
  }, [loadAll]);

  const updateSetting = (field, value) => {
    setSettings((current) => ({ ...current, [field]: value }));
  };

  const updateContactHour = (field, value) => {
    setSettings((current) => ({
      ...current,
      contact_hours: { ...(current?.contact_hours || {}), [field]: Number(value) },
    }));
  };

  const updateBranding = (field, value) => {
    setSettings((current) => ({
      ...current,
      branding: { ...(current?.branding || {}), [field]: value },
    }));
  };

  const updateInboundConfig = (field, value) => {
    setInboundConfig((current) => ({ ...current, [field]: value }));
  };

  return (
    <section className="cr-surface" aria-label="Clinic Recall staff surfaces">
      <header className="cr-header">
        <div className="cr-title-block">
          <p>Clinic Recall</p>
        </div>
        <div className="cr-header-actions">
          <span className="cr-live-count" aria-label={`${queue.length + outbox.length} pending review items`}>
            <strong>{queue.length + outbox.length}</strong>
            review
          </span>
          <button type="button" className="cr-icon-button" onClick={loadAll} disabled={loading} aria-label="Refresh Clinic Recall surfaces">
            <RefreshRoundedIcon fontSize="small" />
          </button>
        </div>
      </header>

      <nav className="cr-tabs" aria-label="Clinic Recall control room">
        {CONTROL_TABS.map((tab) => (
          <ControlTab key={tab.id} tab={tab} active={activeTab === tab.id} count={tabCounts[tab.id]} onSelect={setActiveTab} />
        ))}
      </nav>

      {error ? <div className="cr-alert cr-alert-error" role="alert">{error}</div> : null}
      {notice ? <div className="cr-alert cr-alert-success" aria-live="polite">{notice}</div> : null}

      <div className="cr-tab-panels">
        {activeTab === 'inbox' ? (
        <section className="cr-panel cr-panel-large cr-panel-full">
          <header className="cr-panel-title">
            <div>
              <span>Inbox</span>
              <h2>Escalation and pending booking queue</h2>
            </div>
            <strong className="cr-count-badge">{queue.length}</strong>
          </header>
          <div className="cr-queue-list">
            {queue.length ? queue.map((item) => (
              <QueueItem key={item.item_id} item={item} onAcknowledge={acknowledgeItem} onResolve={resolveItem} busy={busyItem === item.item_id} />
            )) : <div className="cr-empty">No pending staff actions.</div>}
          </div>
        </section>
        ) : null}

        {activeTab === 'phone' ? (
        <section className="cr-panel cr-panel-full" aria-labelledby="phone-title">
          <header className="cr-panel-title cr-panel-title-split">
            <div>
              <span>Phone</span>
              <h2 id="phone-title">Inbound assistant</h2>
            </div>
            <strong className="cr-count-badge">{inboundTasks.length}</strong>
          </header>
          <div className="cr-summary-grid">
            <MetricTile label="Inbound calls" value={inboundMetrics?.calls_total ?? '—'} />
            <MetricTile label="Inbound texts" value={inboundMetrics?.texts_total ?? '—'} />
            <MetricTile label="Open tasks" value={inboundMetrics?.open_tasks ?? '—'} tone={inboundMetrics?.open_tasks ? 'blocked' : 'default'} />
          </div>
          <div className="cr-phone-grid">
            <section className="cr-phone-section">
              <h3>Assigned numbers</h3>
              <div className="cr-phone-list">
                {phoneNumbers.length ? phoneNumbers.map((item) => <PhoneNumberItem key={item.id} item={item} />) : <div className="cr-empty">No inbound numbers assigned.</div>}
              </div>
            </section>
            <section className="cr-phone-section">
              <h3>Recent inbound calls</h3>
              <div className="cr-phone-list">
                {inboundCalls.length ? inboundCalls.slice(0, 4).map((item) => (
                  <article key={item.id} className="cr-sent-item">
                    <div className="cr-sent-head">
                      <div>
                        <h3>{item.provider} · {item.called_number}</h3>
                        <p>{dateTimeLabel(item.created_at)}</p>
                      </div>
                      <span className="cr-status-pill">{item.status}</span>
                    </div>
                    <dl className="cr-sent-meta">
                      <div>
                        <dt>Caller</dt>
                        <dd>{item.caller_number_redacted}</dd>
                      </div>
                      <div>
                        <dt>Outcome</dt>
                        <dd>{item.outcome || 'pending'}</dd>
                      </div>
                    </dl>
                  </article>
                )) : <div className="cr-empty">No inbound calls yet.</div>}
              </div>
            </section>
          </div>
          <section className="cr-phone-section cr-phone-section-wide">
            <h3>Recent inbound texts</h3>
            <div className="cr-phone-list">
              {inboundMessages.length ? inboundMessages.slice(0, 4).map((item) => <InboundMessageItem key={item.id} item={item} />) : <div className="cr-empty">No inbound texts yet.</div>}
            </div>
          </section>
          <section className="cr-phone-section cr-phone-section-wide">
            <h3>Open inbound tasks</h3>
            <div className="cr-inbound-task-list">
              {inboundTasks.length ? inboundTasks.map((item) => (
                <InboundTaskItem key={item.id} item={item} onAcknowledge={acknowledgeInboundTask} onResolve={resolveInboundTask} busy={busyItem === item.id} />
              )) : <div className="cr-empty">No inbound tasks.</div>}
            </div>
          </section>
          <section className="cr-phone-section cr-phone-section-wide">
            <h3>Inbound rules</h3>
            <div className="cr-settings-grid">
              <label>
                Greeting
                <input type="text" value={inboundConfig?.greeting ?? ''} onChange={(event) => updateInboundConfig('greeting', event.target.value)} />
              </label>
              <label>
                Callback SLA hours
                <input type="number" min="1" max="168" value={inboundConfig?.callback_sla_hours ?? 4} onChange={(event) => updateInboundConfig('callback_sla_hours', event.target.value)} />
              </label>
              <label>
                Escalation destination
                <input type="text" value={inboundConfig?.escalation_destination ?? ''} onChange={(event) => updateInboundConfig('escalation_destination', event.target.value)} />
              </label>
              <label>
                Recording enabled
                <input type="text" value={inboundConfig?.recording_enabled ? 'true' : 'false'} onChange={(event) => updateInboundConfig('recording_enabled', event.target.value === 'true')} />
              </label>
            </div>
            <div className="cr-campaign-actions">
              <button type="button" className="cr-action" onClick={saveInboundConfig} disabled={loading}>Save inbound rules</button>
            </div>
          </section>
        </section>
        ) : null}

        {activeTab === 'outbox' ? (
        <section className="cr-panel cr-panel-full">
          <header className="cr-panel-title cr-panel-title-split">
            <div>
              <span>Outbox</span>
              <h2>Queued outbound review</h2>
            </div>
            <button type="button" className="cr-action cr-action-approve" onClick={() => openCampaignReview()} disabled={loading || !campaigns.some((campaign) => campaign.is_approvable)}>
              <ApprovalRoundedIcon fontSize="small" /> Review and approve launch
            </button>
          </header>
          <div className="cr-summary-grid">
            <MetricTile label="Queued" value={outbox.length} />
            <MetricTile label="Eligible now" value={outboxEligible} tone="money" />
            <MetricTile label="Blocked" value={outboxBlocked} tone={outboxBlocked ? 'blocked' : 'default'} />
          </div>
          <div className="cr-outbox-list">
            {outbox.length ? outbox.map((item) => <OutboxItem key={item.item_id} item={item} />) : <div className="cr-empty">No queued outbound messages.</div>}
          </div>
        </section>
        ) : null}

        {activeTab === 'campaigns' ? (
        <section className="cr-panel cr-panel-full">
          <header className="cr-panel-title">
            <div>
              <span>Campaigns</span>
              <h2>Launch review and cadence controls</h2>
            </div>
            <button type="button" className="cr-action cr-action-launch" onClick={launchCampaign} disabled={loading}>
              <RocketLaunchRoundedIcon fontSize="small" /> Launch
            </button>
          </header>
          {launchSummary ? (
            <div className="cr-launch-summary">
              <strong>{launchSummary.queued}</strong> queued · <strong>{launchSummary.detected_total}</strong> detected · <strong>{launchSummary.already_queued}</strong> already queued
            </div>
          ) : null}
          <div className="cr-campaign-list">
            {campaigns.length ? campaigns.map((campaign) => (
              <CampaignRow
                key={campaign.id}
                campaign={campaign}
                onReview={openCampaignReview}
                onPause={pauseCampaign}
                busy={busyAction === `approve:${campaign.id}` || busyAction === `pause:${campaign.id}`}
              />
            )) : <div className="cr-empty cr-empty-compact">No campaigns.</div>}
          </div>
        </section>
        ) : null}

        {activeTab === 'sent' ? (
        <section className="cr-panel cr-panel-full">
          <header className="cr-panel-title">
            <div>
              <span>Sent</span>
              <h2>Interaction timeline</h2>
            </div>
            <strong className="cr-count-badge">{sent.length}</strong>
          </header>
          <div className="cr-sent-list">
            {sent.length ? sent.map((item) => <SentItem key={item.item_id} item={item} />) : <div className="cr-empty">No sent or inbound interactions yet.</div>}
          </div>
        </section>
        ) : null}

        {activeTab === 'incidents' ? (
        <section className="cr-panel cr-panel-full" aria-labelledby="incidents-title">
          <header className="cr-panel-title">
            <div>
              <span>Incidents</span>
              <h2 id="incidents-title">Anonymous incident reporting</h2>
            </div>
            <strong className="cr-count-badge">{incidents.length}</strong>
          </header>
          <div className="cr-settings-grid" data-testid="incident-form">
            <p className="cr-form-note">
              Reports are anonymous by design: no name, patient, phone number, or login is stored
              with the report, and the time is recorded to the nearest hour. Do not include
              patient-identifying details in the description. Patients can also file reports by
              texting REPORT followed by a description to the clinic number.
            </p>
            <label>
              Category
              <select
                value={incidentDraft.category}
                onChange={(event) => setIncidentDraft((draft) => ({ ...draft, category: event.target.value }))}
              >
                {INCIDENT_CATEGORIES.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            <label>
              Severity
              <select
                value={incidentDraft.severity}
                onChange={(event) => setIncidentDraft((draft) => ({ ...draft, severity: event.target.value }))}
              >
                {INCIDENT_SEVERITIES.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            <label className="cr-span-full">
              What happened?
              <textarea
                rows={4}
                maxLength={4000}
                value={incidentDraft.description}
                placeholder="Describe the incident without naming the patient or yourself."
                onChange={(event) => setIncidentDraft((draft) => ({ ...draft, description: event.target.value }))}
              />
            </label>
            <button
              type="button"
              className="cr-primary-button"
              onClick={submitIncident}
              disabled={busyAction === 'incident-submit' || !incidentDraft.description.trim()}
            >
              Submit anonymous report
            </button>
          </div>
          <div className="cr-queue-list">
            {incidents.length ? incidents.map((item) => (
              <article key={item.id} className={`cr-queue-item cr-priority-${item.severity === 'severe' ? 'high' : 'normal'}`} data-testid="incident-item">
                <div className="cr-priority-rail" aria-hidden="true">
                  <span />
                </div>
                <div className="cr-queue-main">
                  <div className="cr-queue-heading">
                    <div>
                      <h3>{INCIDENT_CATEGORIES.find((option) => option.value === item.category)?.label || formatEnumLabel(item.category)}</h3>
                      <p>{item.description}</p>
                    </div>
                    <div className="cr-chip-row" aria-label="Incident status">
                      <span className={`cr-status-pill ${item.severity === 'severe' ? 'cr-priority-pill cr-priority-pill-high' : ''}`}>{formatEnumLabel(item.severity)}</span>
                      <span className="cr-status-pill">{formatEnumLabel(item.status)}</span>
                      <span className="cr-status-pill">{formatEnumLabel(item.source)}</span>
                    </div>
                  </div>
                  <small>Occurred around {new Date(item.occurred_hour).toLocaleString()}</small>
                </div>
                <div className="cr-queue-actions">
                  {(INCIDENT_NEXT_STATUS[item.status] || []).map((action) => (
                    <button
                      key={action.value}
                      type="button"
                      className="cr-action"
                      onClick={() => advanceIncident(item.id, action.value)}
                      disabled={busyItem === item.id}
                    >
                      {action.label}
                    </button>
                  ))}
                </div>
              </article>
            )) : <div className="cr-empty">No incident reports yet. Submit the form above to file the first anonymous report.</div>}
          </div>
        </section>
        ) : null}

        {activeTab === 'roi' ? (
        <section className="cr-panel cr-panel-full">
          <header className="cr-panel-title cr-panel-title-split">
            <div>
              <span>ROI</span>
              <h2>Month read model</h2>
            </div>
            <a className="cr-download" href={csvUrl}>
              <DownloadRoundedIcon fontSize="small" /> CSV
            </a>
          </header>
          <div className="cr-period-row">
            <label>
              Start
              <input type="date" value={start} onChange={(event) => setStart(event.target.value)} />
            </label>
            <label>
              End
              <input type="date" value={end} onChange={(event) => setEnd(event.target.value)} />
            </label>
          </div>
          <div className="cr-metric-grid">
            <MetricTile label="Contacted" value={metrics?.contacted ?? '—'} />
            <MetricTile label="Rebooked" value={metrics?.rebooked ?? '—'} detail={percent(metrics?.conversion_rate)} />
            <MetricTile label="Recovered revenue" value={money(metrics?.recovered_revenue)} tone="money" />
            <MetricTile label="Net value" value={money(metrics?.monthly_net)} tone="money" />
            <MetricTile label="Opt-out rate" value={percent(metrics?.opt_out_rate)} />
            <MetricTile label="No-show delta" value={percent(metrics?.no_show_delta)} />
          </div>
        </section>
        ) : null}

        {activeTab === 'settings' ? (
        <section className="cr-panel cr-panel-campaign cr-panel-full">
          <header className="cr-panel-title">
            <div>
              <span>Settings</span>
              <h2>Campaign limits and clinic branding</h2>
            </div>
            <SettingsRoundedIcon className="cr-muted-icon" />
          </header>
          <div className="cr-settings-grid">
            <label>
              Daily cap
              <input type="number" min="1" value={settings?.daily_caps ?? 1} onChange={(event) => updateSetting('daily_caps', event.target.value)} />
            </label>
            <label>
              Start hour
              <input type="number" min="0" max="23" value={settings?.contact_hours?.start_hour ?? 9} onChange={(event) => updateContactHour('start_hour', event.target.value)} />
            </label>
            <label>
              End hour
              <input type="number" min="0" max="23" value={settings?.contact_hours?.end_hour ?? 17} onChange={(event) => updateContactHour('end_hour', event.target.value)} />
            </label>
            <label>
              SMS sender
              <input type="text" value={settings?.branding?.sms_sender ?? ''} onChange={(event) => updateBranding('sms_sender', event.target.value)} />
            </label>
          </div>
          <div className="cr-campaign-actions">
            <button type="button" className="cr-action" onClick={saveSettings} disabled={loading}>Save</button>
          </div>
        </section>
        ) : null}
      </div>
      <CampaignReviewModal
        campaign={reviewCampaign}
        items={reviewItems}
        onApprove={approveCampaign}
        onClose={() => setReviewCampaignId('')}
        busy={Boolean(busyAction)}
      />
    </section>
  );
}
