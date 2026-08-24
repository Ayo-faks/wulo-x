import ArrowForwardRoundedIcon from '@mui/icons-material/ArrowForwardRounded';
import EventRepeatRoundedIcon from '@mui/icons-material/EventRepeatRounded';
import HealthAndSafetyRoundedIcon from '@mui/icons-material/HealthAndSafetyRounded';
import PhoneMissedRoundedIcon from '@mui/icons-material/PhoneMissedRounded';
import PlaylistAddCheckRoundedIcon from '@mui/icons-material/PlaylistAddCheckRounded';
import ShieldRoundedIcon from '@mui/icons-material/ShieldRounded';
import TrendingUpRoundedIcon from '@mui/icons-material/TrendingUpRounded';
import SupportAgentRoundedIcon from '@mui/icons-material/SupportAgentRounded';
import { useMemo, useState } from 'react';
import { API_BASE_URL, isGoogleLoginEnabled } from '../config/constants.js';
import DemoVoiceWidget from './DemoVoiceWidget.jsx';

const UTM_KEYS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'utm_id'];

/** Parse utm_* params once so paid-social attribution survives into signup. */
function readAttribution() {
  const params = new URLSearchParams(window.location.search);
  const attribution = {};
  for (const key of UTM_KEYS) {
    const value = params.get(key);
    if (value) attribution[key] = value.slice(0, 128);
  }
  return Object.keys(attribution).length ? attribution : null;
}

const USE_CASES = [
  {
    icon: EventRepeatRoundedIcon,
    title: 'Recall & reactivation',
    copy: 'Finds overdue and lapsed patients, contacts them safely, and brings them back onto the books.',
  },
  {
    icon: PhoneMissedRoundedIcon,
    title: 'No-show recovery',
    copy: 'Follows up missed appointments the same day with SMS first and an AI voice call fallback.',
  },
  {
    icon: PlaylistAddCheckRoundedIcon,
    title: 'Waitlist fill',
    copy: 'Offers real, tool-verified slots - never invented availability - to fill late cancellations.',
  },
  {
    icon: SupportAgentRoundedIcon,
    title: 'Follow-up reminders',
    copy: 'Keeps recurring treatment plans on track without adding front-desk workload.',
  },
];

const HOW_IT_WORKS = [
  { step: '1', title: 'Detect', copy: 'Deterministic rules spot patients who need follow-up. No guesswork.' },
  { step: '2', title: 'Contact', copy: 'Safe SMS outreach first; a governed AI voice agent follows up when needed.' },
  { step: '3', title: 'Rebook', copy: 'Real slots only. Bookings are confirmed by code, never claimed by the AI.' },
  { step: '4', title: 'Escalate', copy: 'Anything clinical, urgent, or unclear stops automation and goes to your staff.' },
];

const TRUST_POINTS = [
  { icon: HealthAndSafetyRoundedIcon, title: 'Fail-closed clinical safety', copy: 'The agent never gives medical advice. Clinical or urgent signals always route to humans.' },
  { icon: ShieldRoundedIcon, title: 'Per-clinic data isolation', copy: 'Row-level security keeps every clinic\u2019s data separate, enforced in the database.' },
  { icon: SupportAgentRoundedIcon, title: 'Anonymous incident reporting', copy: 'Staff and patients can file anonymous governance reports - just-culture by design.' },
  { icon: TrendingUpRoundedIcon, title: 'Evaluated before release', copy: 'Every agent version passes hosted safety evaluations, red-teaming, and release gates.' },
];

export default function LandingPage() {
  const signupEnabled = import.meta.env.VITE_ENABLE_SELF_SERVE_SIGNUP !== 'false';
  const isCampaignVariant = window.location.pathname.startsWith('/go/');
  const attribution = useMemo(readAttribution, []);
  const [form, setForm] = useState({ clinic_name: '', contact_email: '' });
  const [status, setStatus] = useState({ busy: false, message: '', error: '' });

  const updateField = (field, value) => {
    setForm((current) => ({ ...current, [field]: value }));
  };

  const submitSignup = async (event) => {
    event.preventDefault();
    setStatus({ busy: true, message: '', error: '' });
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/clinic-recall/signup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...form, ...(attribution ? { attribution } : {}) }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || payload.error || `Signup failed (${response.status})`);
      }
      setStatus({
        busy: false,
        message: `Created ${payload.clinic_id}. Status: ${payload.status}; sandbox setup starts at ${payload.onboarding_next}.`,
        error: '',
      });
      setForm({ clinic_name: '', contact_email: '' });
    } catch (error) {
      setStatus({ busy: false, message: '', error: error.message });
    }
  };

  return (
    <main className="landing-page" aria-labelledby="landing-title">
      <section className="landing-hero">
        <div className="landing-copy">
          <p className="landing-kicker">Clinic Recall</p>
          <h1 id="landing-title">
            {isCampaignVariant
              ? 'Recover missed appointments and fill your diary - without hiring front-desk staff.'
              : 'Recover missed appointments without adding front-desk work.'}
          </h1>
          <p className="landing-summary">
            Contacts overdue patients instantly. SMS first, governed AI voice fallback.
            Books real slots straight into your diary - and hands anything clinical to your team.
          </p>
          <div className="landing-actions">
            <a className="landing-primary" href="/app">
              {isCampaignVariant ? 'Get a free recall call demo' : 'Open Clinic Recall'}
              <ArrowForwardRoundedIcon fontSize="small" />
            </a>
            <a className="landing-secondary" href="/.auth/login/aad?post_login_redirect_uri=/app">
              Sign in with Microsoft
            </a>
            {isGoogleLoginEnabled() ? (
              <a className="landing-secondary" href="/.auth/login/google?post_login_redirect_uri=/app">
                Sign in with Google
              </a>
            ) : null}
          </div>
          {signupEnabled ? (
            <form className="landing-signup" onSubmit={submitSignup} aria-label="Create clinic workspace">
              <label>
                Clinic name
                <input
                  type="text"
                  value={form.clinic_name}
                  onChange={(event) => updateField('clinic_name', event.target.value)}
                  minLength={2}
                  maxLength={120}
                  required
                />
              </label>
              <label>
                Work email
                <input
                  type="email"
                  value={form.contact_email}
                  onChange={(event) => updateField('contact_email', event.target.value)}
                  required
                />
              </label>
              <button type="submit" disabled={status.busy}>
                {status.busy ? 'Creating...' : (isCampaignVariant ? 'Create free demo clinic' : 'Create sandbox clinic')}
              </button>
              {status.message ? <output className="landing-form-success">{status.message}</output> : null}
              {status.error ? <div className="landing-form-error" role="alert">{status.error}</div> : null}
            </form>
          ) : null}
        </div>
        <div className="landing-panel" aria-label="Wulo-X workflow summary">
          <div className="landing-metric">
            <span>Queue</span>
            <strong>0</strong>
            <small>open staff actions</small>
          </div>
          <div className="landing-flow">
            <div>
              <SupportAgentRoundedIcon fontSize="small" />
              SMS first
            </div>
            <div>
              <ShieldRoundedIcon fontSize="small" />
              fail closed
            </div>
            <div>
              <TrendingUpRoundedIcon fontSize="small" />
              ROI tracked
            </div>
          </div>
        </div>
      </section>

      <DemoVoiceWidget />

      <section className="landing-section" aria-labelledby="usecases-title">
        <h2 id="usecases-title">Driving results across the clinic</h2>
        <div className="landing-card-grid">
          {USE_CASES.map((useCase) => {
            const Icon = useCase.icon;
            return (
              <article key={useCase.title} className="landing-card">
                <Icon fontSize="small" />
                <h3>{useCase.title}</h3>
                <p>{useCase.copy}</p>
              </article>
            );
          })}
        </div>
      </section>

      <section className="landing-section" aria-labelledby="how-title">
        <h2 id="how-title">How it works</h2>
        <div className="landing-card-grid landing-steps">
          {HOW_IT_WORKS.map((item) => (
            <article key={item.step} className="landing-card">
              <strong className="landing-step-number">{item.step}</strong>
              <h3>{item.title}</h3>
              <p>{item.copy}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="landing-section" aria-labelledby="trust-title">
        <h2 id="trust-title">Built for clinics, governed for safety</h2>
        <div className="landing-card-grid">
          {TRUST_POINTS.map((point) => {
            const Icon = point.icon;
            return (
              <article key={point.title} className="landing-card">
                <Icon fontSize="small" />
                <h3>{point.title}</h3>
                <p>{point.copy}</p>
              </article>
            );
          })}
        </div>
        <p className="landing-footnote">
          Appointment logistics only - Clinic Recall never diagnoses, advises, or handles payments.
          GDPR-aligned data handling with per-clinic isolation.
        </p>
      </section>
    </main>
  );
}
