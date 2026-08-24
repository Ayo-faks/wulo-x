/**
 * DemoVoiceWidget — Motics-style "try the phone agent" widget for the landing page.
 *
 * Two modes, both gated server-side (Turnstile + rate limits + signed tokens):
 *  - Browser demo: mic session against the demo persona, hard-capped server-side.
 *  - Phone demo:   real outbound call to a UK number, capped server-side.
 *
 * Availability is runtime configuration, not a build input: the widget asks
 * GET /api/v1/demo/capabilities for the active experience, enabled transports,
 * demo duration, and the PUBLIC Turnstile site key. It renders nothing
 * actionable when the demo reports itself off or the contract is unreachable
 * (fail closed); the backend independently fails closed without a valid
 * Turnstile token. window.TURNSTILE_SITE_KEY remains a test-only override,
 * mirroring the ENABLE_GOOGLE_LOGIN convention in config/constants.js.
 */
import MicRoundedIcon from '@mui/icons-material/MicRounded';
import PhoneForwardedRoundedIcon from '@mui/icons-material/PhoneForwardedRounded';
import { useCallback, useEffect, useRef, useState } from 'react';
import { API_BASE_URL, WS_URL } from '../config/constants.js';

const TURNSTILE_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
const CAPABILITIES_PATH = '/api/v1/demo/capabilities';
const DEFAULT_DEMO_SECONDS = 60;

const OFF_CAPABILITIES = Object.freeze({
  experience: 'off',
  browser_enabled: false,
  phone_enabled: false,
  max_demo_seconds: DEFAULT_DEMO_SECONDS,
  turnstile_site_key: '',
});

/** Coerce the server contract defensively; anything malformed fails closed. */
function normalizeCapabilities(body) {
  if (!body || typeof body !== 'object') return OFF_CAPABILITIES;
  const siteKey = typeof body.turnstile_site_key === 'string' ? body.turnstile_site_key : '';
  const seconds = Number.parseInt(body.max_demo_seconds, 10);
  const normalized = {
    experience: typeof body.experience === 'string' ? body.experience : 'off',
    browser_enabled: body.browser_enabled === true,
    phone_enabled: body.phone_enabled === true,
    max_demo_seconds: Number.isFinite(seconds) && seconds > 0 ? seconds : DEFAULT_DEMO_SECONDS,
    turnstile_site_key: siteKey,
  };
  if (
    normalized.experience === 'off'
    || !siteKey
    || (!normalized.browser_enabled && !normalized.phone_enabled)
  ) {
    return { ...OFF_CAPABILITIES, max_demo_seconds: normalized.max_demo_seconds };
  }
  return normalized;
}

/** Test-only override: a window site key short-circuits the capabilities fetch. */
function windowCapabilitiesOverride() {
  if (typeof window === 'undefined' || !window.TURNSTILE_SITE_KEY) return null;
  return {
    experience: 'legacy',
    browser_enabled: true,
    phone_enabled: true,
    max_demo_seconds: DEFAULT_DEMO_SECONDS,
    turnstile_site_key: window.TURNSTILE_SITE_KEY,
  };
}

// AudioWorklet PCM sink (jitter-buffered), same approach as useRealTimeVoiceApp.
const workletSource = `
  class PcmSink extends AudioWorkletProcessor {
    constructor() {
      super();
      this.queue = [];
      this.readIndex = 0;
      this.PREBUFFER_SAMPLES = Math.floor(sampleRate * 0.10);
      this.MAX_WAIT_FRAMES = Math.floor((sampleRate * 0.16) / 128);
      this.queuedSamples = 0;
      this.playing = false;
      this.sawData = false;
      this.framesWaited = 0;
      this.port.onmessage = (e) => {
        if (e.data?.type === 'push') {
          this.queue.push(e.data.payload);
          this.queuedSamples += e.data.payload.length;
        } else if (e.data?.type === 'clear') {
          this.queue = [];
          this.readIndex = 0;
          this.queuedSamples = 0;
          this.playing = false;
          this.sawData = false;
          this.framesWaited = 0;
        }
      };
    }
    process(inputs, outputs) {
      const out = outputs[0][0];
      if (!this.playing) {
        if (this.queuedSamples > 0) this.sawData = true;
        if (this.sawData) this.framesWaited++;
        if (this.queuedSamples >= this.PREBUFFER_SAMPLES || this.framesWaited >= this.MAX_WAIT_FRAMES) {
          this.playing = true;
        } else {
          out.fill(0);
          return true;
        }
      }
      let i = 0;
      while (i < out.length) {
        if (this.queue.length === 0) {
          for (; i < out.length; i++) out[i] = 0;
          break;
        }
        const chunk = this.queue[0];
        const remain = chunk.length - this.readIndex;
        const toCopy = Math.min(remain, out.length - i);
        out.set(chunk.subarray(this.readIndex, this.readIndex + toCopy), i);
        i += toCopy;
        this.readIndex += toCopy;
        this.queuedSamples -= toCopy;
        if (this.readIndex >= chunk.length) {
          this.queue.shift();
          this.readIndex = 0;
        }
      }
      return true;
    }
  }
  registerProcessor('pcm-sink', PcmSink);
`;

let turnstileScriptPromise = null;
function loadTurnstile() {
  if (turnstileScriptPromise) return turnstileScriptPromise;
  turnstileScriptPromise = new Promise((resolve, reject) => {
    if (window.turnstile) return resolve(window.turnstile);
    const script = document.createElement('script');
    script.src = TURNSTILE_SRC;
    script.async = true;
    script.onload = () => resolve(window.turnstile);
    script.onerror = () => reject(new Error('captcha_load_failed'));
    document.head.appendChild(script);
  });
  return turnstileScriptPromise;
}

async function postJson(path, body) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `request_failed_${response.status}`);
  }
  return payload;
}

const GATE_ERRORS = {
  invalid_email: 'Please use a valid work email address.',
  invalid_clinic_name: 'Please enter your clinic name.',
  invalid_uk_phone: 'Please enter a valid UK phone number (e.g. 07700 900123).',
  blocked_phone_range: 'That number range is not supported for demo calls.',
  captcha_failed: 'The security check failed - please try again.',
  captcha_required: 'Please complete the security check first.',
  too_many_requests: 'Too many demo requests from your network - try again later.',
  phone_daily_limit: 'That number has already had a demo call today.',
  demo_capacity_reached: 'Demo capacity is full for today - please try tomorrow.',
  demo_disabled: 'The live demo is not available right now.',
  demo_call_failed: 'We could not place the call - please try again shortly.',
  rate_limiter_unavailable: 'The demo is temporarily unavailable - please try again shortly.',
};

function friendlyError(message) {
  return GATE_ERRORS[message] || 'Something went wrong - please try again.';
}

export default function DemoVoiceWidget() {
  const [capabilities, setCapabilities] = useState(windowCapabilitiesOverride);
  const [mode, setMode] = useState('browser'); // 'browser' | 'phone'
  const [form, setForm] = useState({ work_email: '', clinic_name: '', phone_number: '' });
  const [phase, setPhase] = useState('idle'); // idle | connecting | live | calling | done | error
  const [error, setError] = useState('');
  const [secondsLeft, setSecondsLeft] = useState(DEFAULT_DEMO_SECONDS);
  const [transcript, setTranscript] = useState([]);
  const [captchaToken, setCaptchaToken] = useState('');

  const captchaRef = useRef(null);
  const captchaWidgetId = useRef(null);
  const socketRef = useRef(null);
  const micContextRef = useRef(null);
  const micStreamRef = useRef(null);
  const processorRef = useRef(null);
  const playbackContextRef = useRef(null);
  const pcmSinkRef = useRef(null);
  const countdownRef = useRef(null);

  const siteKey = capabilities?.turnstile_site_key || '';
  const browserEnabled = Boolean(capabilities?.browser_enabled);
  const phoneEnabled = Boolean(capabilities?.phone_enabled);
  const maxSeconds = capabilities?.max_demo_seconds || DEFAULT_DEMO_SECONDS;
  const enabled = Boolean(
    capabilities && capabilities.experience !== 'off' && siteKey && (browserEnabled || phoneEnabled),
  );

  // Resolve runtime availability once (unless a test override already did).
  useEffect(() => {
    if (capabilities) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch(`${API_BASE_URL}${CAPABILITIES_PATH}`);
        const body = response.ok ? await response.json().catch(() => null) : null;
        if (!cancelled) setCapabilities(normalizeCapabilities(body));
      } catch {
        if (!cancelled) setCapabilities(OFF_CAPABILITIES); // fail closed
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [capabilities]);

  // Keep the selected mode within the transports the server actually offers.
  useEffect(() => {
    if (!capabilities) return;
    if (mode === 'phone' && !phoneEnabled && browserEnabled) setMode('browser');
    if (mode === 'browser' && !browserEnabled && phoneEnabled) setMode('phone');
  }, [capabilities, mode, browserEnabled, phoneEnabled]);

  // Render the Turnstile widget once its container exists.
  useEffect(() => {
    if (!enabled || !siteKey || !captchaRef.current || captchaWidgetId.current !== null) return undefined;
    let cancelled = false;
    loadTurnstile()
      .then((turnstile) => {
        if (cancelled || !captchaRef.current) return;
        captchaWidgetId.current = turnstile.render(captchaRef.current, {
          sitekey: siteKey,
          callback: (token) => setCaptchaToken(token),
          'expired-callback': () => setCaptchaToken(''),
        });
      })
      .catch(() => setError(friendlyError('captcha_load_failed')));
    return () => {
      cancelled = true;
    };
  }, [enabled, siteKey]);

  const stopSession = useCallback((finalPhase = 'done') => {
    if (countdownRef.current) {
      clearInterval(countdownRef.current);
      countdownRef.current = null;
    }
    try { processorRef.current?.disconnect(); } catch { /* noop */ }
    processorRef.current = null;
    try { micContextRef.current?.close(); } catch { /* noop */ }
    micContextRef.current = null;
    for (const track of micStreamRef.current?.getTracks?.() ?? []) {
      track.stop();
    }
    micStreamRef.current = null;
    try { playbackContextRef.current?.close(); } catch { /* noop */ }
    playbackContextRef.current = null;
    pcmSinkRef.current = null;
    try { socketRef.current?.close(); } catch { /* noop */ }
    socketRef.current = null;
    setPhase(finalPhase);
  }, []);

  useEffect(() => () => stopSession('idle'), [stopSession]);

  const handleSocketMessage = useCallback(async (event) => {
    if (typeof event.data !== 'string') return;
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    if (payload.type === 'audio_data' && payload.data && pcmSinkRef.current) {
      const bstr = atob(payload.data);
      const buf = new ArrayBuffer(bstr.length);
      const view = new Uint8Array(buf);
      for (let i = 0; i < bstr.length; i++) view[i] = bstr.charCodeAt(i);
      const int16 = new Int16Array(buf);
      const float32 = new Float32Array(int16.length);
      for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 0x8000;
      pcmSinkRef.current.port.postMessage({ type: 'push', payload: float32 });
      return;
    }
    if (payload.sender && payload.message) {
      payload.speaker = payload.sender;
      payload.content = payload.message;
    }
    const text = payload.content || payload.message || '';
    const type = (payload.type || '').toLowerCase();
    if (!text) return;
    if (type === 'user' || payload.speaker === 'User') {
      setTranscript((prev) => [...prev, { id: `${Date.now()}-${prev.length}`, speaker: 'You', text }]);
    } else if (type === 'assistant' || type === 'assistant_streaming' || payload.speaker === 'Assistant') {
      setTranscript((prev) => {
        const last = prev[prev.length - 1];
        if (last?.speaker === 'Agent' && (type === 'assistant_streaming' || last.text === text)) {
          return [...prev.slice(0, -1), { ...last, text }];
        }
        return [...prev, { id: `${Date.now()}-${prev.length}`, speaker: 'Agent', text }];
      });
    }
  }, []);

  const startBrowserDemo = useCallback(async (demoToken) => {
    setTranscript([]);
    setSecondsLeft(maxSeconds);

    // Playback context (48 kHz to match backend PCM stream).
    const playbackCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
    if (playbackCtx.state === 'suspended') await playbackCtx.resume();
    await playbackCtx.audioWorklet.addModule(
      URL.createObjectURL(new Blob([workletSource], { type: 'text/javascript' })),
    );
    const pcmSink = new AudioWorkletNode(playbackCtx, 'pcm-sink');
    pcmSink.connect(playbackCtx.destination);
    playbackContextRef.current = playbackCtx;
    pcmSinkRef.current = pcmSink;

    // Mic capture at 16 kHz PCM16.
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    micStreamRef.current = stream;
    const micCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    micContextRef.current = micCtx;
    const source = micCtx.createMediaStreamSource(stream);
    const processor = micCtx.createScriptProcessor(512, 1, 1);
    processorRef.current = processor;

    const sessionId = `demo_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const url = `${WS_URL}/api/v1/browser/conversation?session_id=${encodeURIComponent(sessionId)}&demo_token=${encodeURIComponent(demoToken)}`;
    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    socketRef.current = socket;

    socket.onopen = () => {
      setPhase('live');
      countdownRef.current = setInterval(() => {
        setSecondsLeft((current) => {
          if (current <= 1) {
            stopSession('done');
            return 0;
          }
          return current - 1;
        });
      }, 1000);
    };
    socket.onmessage = handleSocketMessage;
    socket.onerror = () => {
      setError(friendlyError('demo_disabled'));
      stopSession('error');
    };
    socket.onclose = () => {
      if (countdownRef.current) stopSession('done');
    };

    processor.onaudioprocess = (evt) => {
      const float32 = evt.inputBuffer.getChannelData(0);
      const int16 = new Int16Array(float32.length);
      for (let i = 0; i < float32.length; i++) {
        int16[i] = Math.max(-1, Math.min(1, float32[i])) * 0x7fff;
      }
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(int16.buffer);
      }
    };
    source.connect(processor);
    processor.connect(micCtx.destination);
  }, [handleSocketMessage, stopSession, maxSeconds]);

  const resetCaptcha = useCallback(() => {
    setCaptchaToken('');
    if (window.turnstile && captchaWidgetId.current !== null) {
      try { window.turnstile.reset(captchaWidgetId.current); } catch { /* noop */ }
    }
  }, []);

  const submit = async (event) => {
    event.preventDefault();
    setError('');
    setPhase('connecting');
    try {
      if (mode === 'browser') {
        const session = await postJson('/api/v1/demo/session', {
          work_email: form.work_email,
          clinic_name: form.clinic_name,
          turnstile_token: captchaToken,
        });
        await startBrowserDemo(session.demo_token);
      } else {
        await postJson('/api/v1/demo/call', {
          work_email: form.work_email,
          clinic_name: form.clinic_name,
          phone_number: form.phone_number,
          turnstile_token: captchaToken,
        });
        setPhase('calling');
      }
    } catch (err) {
      setError(friendlyError(err.message));
      setPhase('error');
    } finally {
      resetCaptcha();
    }
  };

  if (!enabled) return null;

  const busy = phase === 'connecting';
  const inSession = phase === 'live';

  return (
    <section className="demo-widget" aria-labelledby="demo-widget-title">
      <p className="landing-kicker">Try it now</p>
      <h2 id="demo-widget-title">Take a 60-second live demo call</h2>
      <p className="demo-widget-summary">
        Speak to the Clinic Recall agent for a fictional demo clinic - in your browser,
        or with a real call to your UK phone. Free, nothing is booked, nothing is recorded.
      </p>

      <div className="demo-widget-modes" role="tablist" aria-label="Demo type">
        {browserEnabled ? (
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'browser'}
            className={mode === 'browser' ? 'active' : ''}
            onClick={() => setMode('browser')}
            disabled={busy || inSession}
          >
            <MicRoundedIcon fontSize="small" /> In your browser
          </button>
        ) : null}
        {phoneEnabled ? (
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'phone'}
            className={mode === 'phone' ? 'active' : ''}
            onClick={() => setMode('phone')}
            disabled={busy || inSession}
          >
            <PhoneForwardedRoundedIcon fontSize="small" /> Call my phone
          </button>
        ) : null}
      </div>

      {inSession ? (
        <div className="demo-widget-session" aria-live="polite">
          <div className="demo-widget-countdown">
            <span>Live demo</span>
            <strong>{secondsLeft}s</strong>
          </div>
          <ul className="demo-widget-transcript">
            {transcript.map((entry) => (
              <li key={entry.id} className={entry.speaker === 'You' ? 'you' : 'agent'}>
                <strong>{entry.speaker}:</strong> {entry.text}
              </li>
            ))}
          </ul>
          <button type="button" className="landing-secondary" onClick={() => stopSession('done')}>
            End demo
          </button>
        </div>
      ) : (
        <form className="demo-widget-form" onSubmit={submit} aria-label="Start demo">
          <label>
            Work email
            <input
              type="email"
              required
              maxLength={254}
              value={form.work_email}
              onChange={(e) => setForm((f) => ({ ...f, work_email: e.target.value }))}
              placeholder="you@yourclinic.co.uk"
            />
          </label>
          <label>
            Clinic name
            <input
              type="text"
              required
              minLength={2}
              maxLength={120}
              value={form.clinic_name}
              onChange={(e) => setForm((f) => ({ ...f, clinic_name: e.target.value }))}
              placeholder="Riverside Physiotherapy"
            />
          </label>
          {mode === 'phone' ? (
            <label>
              UK phone number
              <input
                type="tel"
                required
                maxLength={32}
                value={form.phone_number}
                onChange={(e) => setForm((f) => ({ ...f, phone_number: e.target.value }))}
                placeholder="07700 900123"
              />
            </label>
          ) : null}
          <div className="demo-widget-captcha" ref={captchaRef} />
          <button type="submit" className="landing-primary" disabled={busy || !captchaToken}>
            {busy
              ? 'Starting...'
              : mode === 'browser'
                ? 'Start voice demo'
                : 'Call me now'}
          </button>
          <p className="demo-widget-smallprint">
            {mode === 'browser'
              ? `You'll speak with the agent for ${maxSeconds} seconds - we'll ask for mic access first.`
              : `We'll ring your UK number and the agent will chat for up to ${maxSeconds} seconds.`}
          </p>
        </form>
      )}

      {phase === 'calling' ? (
        <output className="demo-widget-status">
          📞 Calling you now - answer to meet the agent.
        </output>
      ) : null}
      {phase === 'done' ? (
        <output className="demo-widget-status">
          That&apos;s the demo! Sign up above to set this up for your own clinic.
        </output>
      ) : null}
      {error ? (
        <p className="demo-widget-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
