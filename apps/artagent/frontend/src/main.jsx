import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import CssBaseline from '@mui/material/CssBaseline'
import { ThemeProvider } from '@mui/material/styles'
import './index.css'
import './styles/auth.css'
import App from './components/App.jsx'
import ClinicRecallSurfaces from './components/ClinicRecallSurfaces.jsx'
import LandingPage from './components/LandingPage.jsx'
import ProductShell from './components/ProductShell.jsx'
import abstractBg from './assets/abstract.jpg'
import { vscodeTheme } from './styles/theme.js'
import logger, { configureLogLevel } from './utils/logger.js'
import { FRONTEND_RUNTIME_REVISION, isGoogleLoginEnabled } from './config/constants.js'

const enableArtDemo = import.meta.env.DEV || import.meta.env.VITE_ENABLE_ART_DEMO === 'true'
const currentPath = window.location.pathname
const isAppRoute = currentPath === '/app' || currentPath.startsWith('/app/')
const isArtRoute = enableArtDemo && (currentPath === '/art' || currentPath.startsWith('/art/'))
const surface = isArtRoute ? 'art' : isAppRoute ? 'recall' : 'landing'

document.body.classList.toggle('art-mode', surface === 'art')
document.body.classList.toggle('clinic-recall-mode', surface === 'recall')
document.body.classList.toggle('landing-mode', surface === 'landing')
document.body.style.backgroundImage = surface === 'art'
  ? `radial-gradient(1100px 620px at 12% -10%, rgba(var(--vsc-accent-rgb), 0.18), transparent 60%), radial-gradient(900px 560px at 105% 5%, rgba(var(--vsc-accent-rgb), 0.10), transparent 55%), url(${abstractBg})`
  : `radial-gradient(1100px 620px at 12% -10%, rgba(var(--vsc-accent-rgb), 0.16), transparent 60%), radial-gradient(900px 560px at 105% 5%, rgba(var(--vsc-accent-rgb), 0.09), transparent 55%), url(${abstractBg})`

configureLogLevel(import.meta.env?.VITE_APP_LOG_LEVEL ?? import.meta.env?.VITE_LOG_LEVEL)
logger.info(`[ARTAgent] Frontend bootstrapping (${FRONTEND_RUNTIME_REVISION})`)

function ProductSwitch({ active }) {
  return (
    <>
      <div className="product-brand" aria-label="Wulo-X">Wulo-X</div>
      <nav className={`product-switch product-switch-${active}`} aria-label="Wulo-X workspaces">
        <a href="/" aria-current={active === 'landing' ? 'page' : undefined}>
          <span>Home</span>
          <small>Start</small>
        </a>
        <a href="/app" aria-current={active === 'recall' ? 'page' : undefined}>
          <span>Clinical</span>
          <small>Recall</small>
        </a>
        {enableArtDemo ? (
          <a href="/art" aria-current={active === 'art' ? 'page' : undefined}>
            <span>Phone</span>
            <small>Assistant</small>
          </a>
        ) : null}
      </nav>
    </>
  )
}

function CurrentSurface() {
  if (surface === 'art') {
    return <App />
  }
  if (surface === 'recall') {
    return (
      <RequireAuth>
        <ProductShell>
          <ClinicRecallSurfaces />
        </ProductShell>
      </RequireAuth>
    )
  }
  return <LandingPage />
}

function RequireAuth({ children }) {
  // window.ENABLE_AUTH_CHECK forces the real auth flow in dev builds (tests).
  const devBypass = import.meta.env.DEV && !(typeof window !== 'undefined' && window.ENABLE_AUTH_CHECK === true)
  const [state, setState] = useState(devBypass ? 'ready' : 'checking')

  useEffect(() => {
    if (devBypass) {
      return
    }
    let cancelled = false
    fetch('/.auth/me', { credentials: 'include' })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        if (cancelled) return
        const principal = Array.isArray(payload) ? payload[0] : payload
        const hasIdentity = Boolean(principal?.user_id || principal?.userDetails || principal?.user_claims?.length || principal?.claims?.length)
        setState(hasIdentity ? 'ready' : 'signin')
      })
      .catch(() => {
        if (!cancelled) {
          setState('signin')
        }
      })
    return () => {
      cancelled = true
    }
  }, [devBypass])

  if (state === 'signin') {
    // Provider choice stays on our page: Microsoft's hosted login can never
    // show a Google option, so we offer both providers here instead of
    // hard-redirecting to /.auth/login/aad.
    return (
      <main className="auth-signin" aria-labelledby="signin-title">
        <div className="auth-card">
          <div className="auth-brand-mark" aria-hidden="true">W</div>
          <span className="auth-kicker">Welcome back</span>
          <h1 id="signin-title">Sign in to Wulo-X</h1>
          <p>Recover missed appointments and follow up patients — with a governed AI agent.</p>
          <div className="auth-providers">
            {isGoogleLoginEnabled() ? (
              <a className="auth-provider auth-provider-google" href="/.auth/login/google?post_login_redirect_uri=/app">
                <svg aria-hidden="true" width="18" height="18" viewBox="0 0 48 48">
                  <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
                  <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
                  <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
                  <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
                </svg>
                Continue with Google
              </a>
            ) : null}
            <a className="auth-provider auth-provider-microsoft" href="/.auth/login/aad?post_login_redirect_uri=/app">
              <svg aria-hidden="true" width="18" height="18" viewBox="0 0 23 23">
                <rect x="1" y="1" width="10" height="10" fill="#f25022" />
                <rect x="12" y="1" width="10" height="10" fill="#7fba00" />
                <rect x="1" y="12" width="10" height="10" fill="#00a4ef" />
                <rect x="12" y="12" width="10" height="10" fill="#ffb900" />
              </svg>
              Continue with Microsoft
            </a>
          </div>
          <span className="auth-footnote">New here? Sign in to create your workspace.</span>
          <a className="auth-back" href="/">Back to home</a>
        </div>
      </main>
    )
  }
  if (state !== 'ready') {
    return <main className="auth-loading" aria-live="polite">Checking sign-in...</main>
  }
  return children
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ThemeProvider theme={vscodeTheme}>
      <CssBaseline />
      <ProductSwitch active={surface} />
      <CurrentSurface />
    </ThemeProvider>
  </StrictMode>,
)