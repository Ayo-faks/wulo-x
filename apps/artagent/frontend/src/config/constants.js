/**
 * Application Configuration Constants
 * 
 * Central configuration for API endpoints and environment variables
 */

// Simple placeholder that gets replaced at container startup, with fallback for local dev
const backendPlaceholder = '__BACKEND_URL__';
const wsPlaceholder = '__WS_URL__';

const fallbackApiBaseUrl = () => {
  if (import.meta.env.VITE_BACKEND_BASE_URL) {
    return import.meta.env.VITE_BACKEND_BASE_URL;
  }
  if (import.meta.env.DEV) {
    return 'http://localhost:8010';
  }
  return typeof window !== 'undefined' && window.location?.origin
    ? window.location.origin
    : '';
};

const toWsUrl = (value) => {
  if (!value || typeof value !== 'string') {
    return 'ws://localhost';
  }
  if (/^wss?:\/\//i.test(value)) {
    return value;
  }
  if (/^https:\/\//i.test(value)) {
    return value.replace(/^https:\/\//i, 'wss://');
  }
  if (/^http:\/\//i.test(value)) {
    return value.replace(/^http:\/\//i, 'ws://');
  }
  return value;
};

export const API_BASE_URL = backendPlaceholder.startsWith('__')
  ? fallbackApiBaseUrl()
  : backendPlaceholder;

const wsBaseCandidate = wsPlaceholder.startsWith('__')
  ? import.meta.env.VITE_WS_BASE_URL || API_BASE_URL
  : wsPlaceholder;

export const WS_URL = toWsUrl(wsBaseCandidate);
export { toWsUrl };

// Replaced at container startup by entrypoint.sh (ENABLE_GOOGLE_LOGIN env var).
// Falls back to the Vite env flag in local dev and a window override in tests.
const googleLoginPlaceholder = '__ENABLE_GOOGLE_LOGIN__';

export const isGoogleLoginEnabled = () => {
  if (!googleLoginPlaceholder.startsWith('__')) {
    return googleLoginPlaceholder === 'true';
  }
  if (typeof window !== 'undefined' && typeof window.ENABLE_GOOGLE_LOGIN === 'boolean') {
    return window.ENABLE_GOOGLE_LOGIN;
  }
  return import.meta.env.VITE_ENABLE_GOOGLE_LOGIN === 'true';
};

export const FRONTEND_RUNTIME_REVISION = '2026-06-29-public-origin-routing';

// Application metadata
export const APP_CONFIG = {
  name: "Real-Time Voice App",
  subtitle: "AI-powered voice interaction platform",
  version: "1.0.0"
};
