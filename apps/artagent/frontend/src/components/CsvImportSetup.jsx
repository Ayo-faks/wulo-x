import React, { useCallback, useEffect, useRef, useState } from 'react';
import UploadFileRoundedIcon from '@mui/icons-material/UploadFileRounded';
import FactCheckRoundedIcon from '@mui/icons-material/FactCheckRounded';
import LinkRoundedIcon from '@mui/icons-material/LinkRounded';
import { API_BASE_URL } from '../config/constants.js';

const IMPORTS_URL = `${API_BASE_URL}/api/v1/clinic-recall/imports/csv`;
const MATCHES_URL = `${API_BASE_URL}/api/v1/clinic-recall/operator/import-matches`;
const UPLOAD_TIMEOUT_MS = 90_000;

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `Request failed (${response.status})`);
  }
  return payload;
}

function shortHash(value) {
  return value ? `${value.slice(0, 12)}…` : '';
}

function formatWhen(value) {
  if (!value) return '';
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

const BATCH_STATE_LABELS = {
  preview_valid: 'Preview ready',
  preview_invalid: 'Preview failed validation',
  superseded: 'Superseded',
  expired: 'Expired',
  completed: 'Imported',
};

/**
 * Staff CSV preview / approve workflow plus aggregate import history.
 *
 * The selected File object lives only in component memory: preview and
 * approval both upload it, nothing is placed in storage or URLs, and any
 * mismatch/expiry clears it so staff must reselect.
 */
export default function CsvImportSetup({ onImported }) {
  const fileInputRef = useRef(null);
  const uploadAbortRef = useRef(null);
  const uploadPendingRef = useRef(false);
  const mountedRef = useRef(true);
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState('');
  const [file, setFile] = useState(null);
  const [sourceSystem, setSourceSystem] = useState('csv');
  const [exportAt, setExportAt] = useState('');
  const [attested, setAttested] = useState(false);
  const [attestedChannels, setAttestedChannels] = useState([]);
  const [preview, setPreview] = useState(null);
  const [history, setHistory] = useState([]);
  const [status, setStatus] = useState({ busy: '', message: '', error: '' });

  const refreshHistory = useCallback(() => {
    fetch(IMPORTS_URL)
      .then(readJson)
      .then((payload) => setHistory(payload.batches || []))
      .catch(() => setHistory([]));
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    let cancelled = false;
    fetch(`${IMPORTS_URL}/config`)
      .then(readJson)
      .then((payload) => {
        if (!cancelled) setConfig(payload);
      })
      .catch((error) => {
        if (!cancelled) setConfigError(error.message);
      });
    refreshHistory();
    return () => {
      cancelled = true;
      mountedRef.current = false;
      uploadAbortRef.current?.abort();
      uploadAbortRef.current = null;
      uploadPendingRef.current = false;
    };
  }, [refreshHistory]);

  const beginUpload = () => {
    if (uploadPendingRef.current) return null;
    uploadPendingRef.current = true;
    const controller = new AbortController();
    uploadAbortRef.current = controller;
    const timeoutId = window.setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS);
    return { controller, timeoutId };
  };

  const finishUpload = (request) => {
    window.clearTimeout(request.timeoutId);
    if (uploadAbortRef.current === request.controller) uploadAbortRef.current = null;
    uploadPendingRef.current = false;
  };

  const uploadErrorMessage = (error) => (
    error?.name === 'AbortError'
      ? 'The upload timed out or was cancelled. Try again.'
      : error.message
  );

  const clearSelection = useCallback((error = '') => {
    setFile(null);
    setPreview(null);
    setAttested(false);
    setAttestedChannels([]);
    if (fileInputRef.current) fileInputRef.current.value = '';
    setStatus({ busy: '', message: '', error });
  }, []);

  const onFileChange = (event) => {
    const selected = event.target.files?.[0] || null;
    setFile(selected);
    setPreview(null); // a new selection always requires a new preview
    setAttested(false);
    setAttestedChannels([]);
    setStatus({ busy: '', message: '', error: '' });
  };

  const onMetadataChange = (setter) => (event) => {
    setter(event.target.value);
    setPreview(null); // changed source metadata invalidates the preview
    setAttested(false);
    setAttestedChannels([]);
  };

  const buildForm = (withAttestation) => {
    const form = new FormData();
    form.append('file', file);
    form.append('source_system', sourceSystem);
    form.append('export_at', new Date(exportAt).toISOString());
    if (withAttestation) {
      form.append('attestation_version', config?.attestation_version || '');
      form.append('attested_channels', attestedChannels.join(','));
      form.append('confirm_clinic_authority', attested ? 'true' : 'false');
    }
    return form;
  };

  const runPreview = async (event) => {
    event.preventDefault();
    if (!file || !exportAt) return;
    const request = beginUpload();
    if (!request) return;
    setStatus({ busy: 'preview', message: '', error: '' });
    try {
      const response = await fetch(`${IMPORTS_URL}/preview`, {
        method: 'POST',
        body: buildForm(false),
        signal: request.controller.signal,
      });
      const payload = await readJson(response);
      if (mountedRef.current) {
        setPreview(payload);
        setStatus({ busy: '', message: '', error: '' });
        refreshHistory();
      }
    } catch (error) {
      if (mountedRef.current) {
        setPreview(null);
        setStatus({ busy: '', message: '', error: uploadErrorMessage(error) });
      }
    } finally {
      finishUpload(request);
    }
  };

  const runApprove = async () => {
    if (!file || !preview?.importable || !attested) return;
    const request = beginUpload();
    if (!request) return;
    setStatus({ busy: 'approve', message: '', error: '' });
    try {
      const response = await fetch(`${IMPORTS_URL}/${preview.batch.id}/approve`, {
        method: 'POST',
        body: buildForm(true),
        signal: request.controller.signal,
      });
      const payload = await readJson(response);
      if (!mountedRef.current) return;
      clearSelection();
      setStatus({
        busy: '',
        message: `Imported ${payload.batch.patients_inserted + payload.batch.patients_updated} patient record(s) and ${payload.batch.appointments_inserted + payload.batch.appointments_updated} appointment(s).`,
        error: '',
      });
      setAttested(false);
      setAttestedChannels([]);
      refreshHistory();
      if (onImported) onImported();
    } catch (error) {
      if (!mountedRef.current) return;
      const detail = String(error.message || '');
      if (error?.name === 'AbortError') {
        setStatus({ busy: '', message: '', error: uploadErrorMessage(error) });
      } else if (detail.includes('file_hash_mismatch') || detail.includes('preview_expired')) {
        clearSelection('The preview no longer matches the selected file. Reselect the file and preview again.');
      } else {
        setStatus({ busy: '', message: '', error: detail });
      }
    } finally {
      finishUpload(request);
    }
  };

  const toggleChannel = (channel) => {
    setAttestedChannels((current) => (
      current.includes(channel)
        ? current.filter((value) => value !== channel)
        : [...current, channel]
    ));
  };

  if (configError) {
    return (
      <div className="shell-action-card" aria-label="CSV import unavailable">
        <strong>CSV import unavailable</strong>
        <div className="shell-inline-error" role="alert">{configError}</div>
      </div>
    );
  }
  if (!config) {
    return <p aria-live="polite">Loading CSV import…</p>;
  }
  if (!config.enabled) {
    return (
      <div className="shell-action-card" aria-label="CSV import disabled">
        <strong><UploadFileRoundedIcon fontSize="small" /> CSV import</strong>
        <p>CSV import is switched off for this environment. An operator enables it after the clinic&apos;s import policy is approved.</p>
      </div>
    );
  }

  const approveReady = Boolean(
    file && preview?.importable && attested && status.busy === '',
  );

  return (
    <div className="shell-action-stack csv-import" aria-label="Controlled CSV import">
      <form className="shell-action-card shell-settings-form" onSubmit={runPreview}>
        <strong><UploadFileRoundedIcon fontSize="small" /> Import patient CSV</strong>
        <p>
          Preview first: the file is validated and summarised without saving any
          patient rows. Importing needs a second explicit approval with the same file.
        </p>
        <label>
          CSV file
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,text/csv"
            onChange={onFileChange}
            aria-describedby="csv-import-limits"
          />
        </label>
        <small id="csv-import-limits">
          One UTF-8 .csv file, up to {Math.floor(config.max_bytes / (1024 * 1024))} MB / {config.max_rows.toLocaleString()} rows.
        </small>
        <label>
          Source system
          <select value={sourceSystem} onChange={onMetadataChange(setSourceSystem)}>
            {config.source_systems.map((system) => (
              <option key={system} value={system}>{system}</option>
            ))}
          </select>
        </label>
        <label>
          Export time (when the file was created)
          <input
            type="datetime-local"
            value={exportAt}
            onChange={onMetadataChange(setExportAt)}
            required
          />
        </label>
        <button
          type="submit"
          className="shell-secondary-button"
          disabled={!file || !exportAt || status.busy !== ''}
        >
          {status.busy === 'preview' ? 'Previewing…' : 'Preview'}
        </button>
      </form>

      {preview ? (
        <div className="shell-action-card" aria-label="CSV preview result">
          <strong><FactCheckRoundedIcon fontSize="small" /> {BATCH_STATE_LABELS[preview.batch.state] || preview.batch.state}</strong>
          <p>
            {preview.batch.total_rows} row(s) ({preview.batch.valid_row_count} valid, {preview.batch.invalid_row_count} invalid) · {preview.batch.patient_count} patient(s) · {preview.batch.appointment_count} appointment(s) · file {shortHash(preview.batch.file_sha256)}
          </p>
          {preview.importable ? (
            <p>Preview valid until {formatWhen(preview.batch.preview_expires_at)}.</p>
          ) : (
            <p role="alert" className="shell-inline-error">
              {preview.batch.state === 'completed'
                ? 'This exact file has already been imported.'
                : `${preview.errors.length} validation issue(s). Fix the file and preview again.`}
            </p>
          )}
          {preview.errors.length ? (
            <table className="csv-import-errors" aria-label="Validation issues">
              <thead>
                <tr><th scope="col">Row</th><th scope="col">Line</th><th scope="col">Field</th><th scope="col">Issue</th></tr>
              </thead>
              <tbody>
                {preview.errors.map((error, index) => (
                  <tr key={`${error.reason}-${index}`}>
                    <td>{error.record ?? '—'}</td>
                    <td>{error.line ?? '—'}</td>
                    <td>{error.field}</td>
                    <td>{error.reason.replaceAll('_', ' ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          {preview.importable ? (
            <fieldset className="csv-import-attestation">
              <legend>Consent and source attestation ({config.attestation_version})</legend>
              <label className="csv-import-checkbox">
                <input
                  type="checkbox"
                  checked={attested}
                  onChange={(event) => setAttested(event.target.checked)}
                />
                <span>{config.attestation_statement}</span>
              </label>
              <p>
                Channels with clinic-held positive consent evidence in this file
                {config.consent_authority_available ? '' : ' (no approved consent evidence policy is active, so imported consent stays unknown either way)'}:
              </p>
              <div className="csv-import-channels">
                {config.consent_channels.map((channel) => (
                  <label key={channel} className="csv-import-checkbox">
                    <input
                      type="checkbox"
                      checked={attestedChannels.includes(channel)}
                      onChange={() => toggleChannel(channel)}
                    />
                    <span>{channel}</span>
                  </label>
                ))}
              </div>
              <button
                type="button"
                className="shell-primary-button"
                onClick={runApprove}
                disabled={!approveReady}
              >
                {status.busy === 'approve' ? 'Importing…' : 'Approve & Import'}
              </button>
            </fieldset>
          ) : null}
        </div>
      ) : null}

      <div aria-live="polite">
        {status.message ? <output className="shell-inline-success">{status.message}</output> : null}
        {status.error ? <div className="shell-inline-error" role="alert">{status.error}</div> : null}
      </div>

      {history.length ? (
        <div className="shell-action-card" aria-label="Import history">
          <strong>Import history</strong>
          <table className="csv-import-history">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">State</th>
                <th scope="col">Rows</th>
                <th scope="col">Patients</th>
                <th scope="col">Appointments</th>
                <th scope="col">File</th>
              </tr>
            </thead>
            <tbody>
              {history.map((batch) => (
                <tr key={batch.id}>
                  <td>{formatWhen(batch.completed_at || batch.created_at)}</td>
                  <td>{BATCH_STATE_LABELS[batch.state] || batch.state}</td>
                  <td>{batch.total_rows}</td>
                  <td>{batch.state === 'completed' ? batch.patients_inserted + batch.patients_updated : batch.patient_count}</td>
                  <td>{batch.state === 'completed' ? batch.appointments_inserted + batch.appointments_updated : batch.appointment_count}</td>
                  <td><code>{shortHash(batch.file_sha256)}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

const REVIEW_STATE_LABELS = {
  not_run: 'Not run',
  pending: 'Pending',
  unmatched: 'Unmatched',
  ambiguous: 'Ambiguous',
  linked: 'Linked',
  dismissed: 'Dismissed',
  failed: 'Failed',
};

/** Operator-only review of provider source-match outcomes. */
export function OperatorImportMatches() {
  const [listing, setListing] = useState(null);
  const [status, setStatus] = useState({ busy: '', message: '', error: '' });
  const [candidateOptions, setCandidateOptions] = useState({});

  const refresh = useCallback(() => {
    fetch(MATCHES_URL)
      .then(readJson)
      .then(setListing)
      .catch((error) => setStatus((current) => ({ ...current, error: error.message })));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const refreshCandidates = async (review) => {
    setStatus({ busy: review.id, message: '', error: '' });
    try {
      const response = await fetch(`${MATCHES_URL}/${review.id}/refresh`, {
        method: 'POST',
      });
      const payload = await readJson(response);
      setCandidateOptions((current) => ({
        ...current,
        [review.id]: payload.candidates || [],
      }));
      setListing((current) => ({
        ...current,
        reviews: current.reviews.map((item) => (
          item.id === review.id ? payload.review : item
        )),
      }));
      setStatus({
        busy: '',
        message: payload.candidates?.length
          ? `Loaded ${payload.candidates.length} exact candidate(s).`
          : 'No exact candidates found.',
        error: '',
      });
    } catch (error) {
      setStatus({ busy: '', message: '', error: error.message });
    }
  };

  const resolve = async (review, action, candidateToken = null) => {
    setStatus({ busy: review.id, message: '', error: '' });
    try {
      const body = { action };
      if (action === 'link') {
        body.candidate_token = candidateToken;
      }
      const response = await fetch(`${MATCHES_URL}/${review.id}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      await readJson(response);
      setCandidateOptions((current) => ({ ...current, [review.id]: [] }));
      setStatus({ busy: '', message: `Review ${action === 'link' ? 'linked' : 'dismissed'}.`, error: '' });
      refresh();
    } catch (error) {
      setStatus({ busy: '', message: '', error: error.message });
    }
  };

  if (!listing) {
    return <p aria-live="polite">Loading source-match reviews…</p>;
  }
  return (
    <div className="csv-import" aria-label="Import source-match review">
      <strong><LinkRoundedIcon fontSize="small" /> Source-match review</strong>
      <p>
        {listing.unmatched_count} unmatched · {listing.ambiguous_count} ambiguous · {listing.pending_count} pending.
        Zero or multiple provider matches always land here; nothing links automatically.
      </p>
      <div aria-live="polite">
        {status.message ? <output className="shell-inline-success">{status.message}</output> : null}
        {status.error ? <div className="shell-inline-error" role="alert">{status.error}</div> : null}
      </div>
      {listing.reviews.length ? (
        <div className="shell-action-stack">
          {listing.reviews.map((review) => {
            const open = !['linked', 'dismissed'].includes(review.state);
            const options = candidateOptions[review.id] || [];
            return (
              <article key={review.id} className="shell-action-card csv-import-review">
                <strong>{REVIEW_STATE_LABELS[review.state] || review.state} · {review.provider}</strong>
                <p>
                  Import {shortHash(review.import_batch_id)} · {review.candidate_count} candidate(s)
                  {review.reason ? ` · ${review.reason.replaceAll('_', ' ')}` : ''}
                </p>
                {review.resolved_by ? (
                  <small>Resolved by {review.resolved_by} at {formatWhen(review.resolved_at)}</small>
                ) : null}
                {open ? (
                  <div className="csv-import-review-actions">
                    <div className="shell-action-row">
                      <button
                        type="button"
                        className="shell-secondary-button"
                        onClick={() => refreshCandidates(review)}
                        disabled={status.busy !== ''}
                      >
                        Refresh candidates
                      </button>
                      <button
                        type="button"
                        className="shell-secondary-button"
                        onClick={() => resolve(review, 'dismiss')}
                        disabled={status.busy !== ''}
                      >
                        Dismiss
                      </button>
                    </div>
                    {options.length ? (
                      <div className="csv-import-candidates" aria-label={`Candidates for review ${review.id}`}>
                        {options.map((option) => (
                          <button
                            key={option.token}
                            type="button"
                            className="shell-primary-button"
                            onClick={() => resolve(review, 'link', option.token)}
                            disabled={status.busy !== ''}
                            title="Link this exact, freshly reviewed provider candidate"
                          >
                            Link candidate {option.ordinal} ({option.active ? 'active' : 'archived'})
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : (
        <p>No source-match reviews yet.</p>
      )}
    </div>
  );
}
