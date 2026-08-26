# Wulo-X Roadmap

Wulo-X is a pre-1.0 engineering preview. The MVP loop is built and tested end to end:
data sync → candidate detection → consent-aware SMS outreach → governed voice
fallback → deterministic booking → staff control room. This roadmap describes where
the project goes next and where contributions have the most impact.

Direction is maintainer-curated: items move between horizons based on pilot feedback
and contributor interest. Anything that touches the
[deterministic safety boundary](docs/deterministic-safety-boundary.md) requires an
issue discussion before implementation.

## Now (0.1.x — hardening the preview)

- Public documentation: quickstart walkthrough, troubleshooting, integration notes.
- Grow the synthetic evaluation and red-team corpus for the agent safety gates.
- Control-room polish: accessibility, exports, and small UX gaps that block pilots.
- GDPR lifecycle completion (erasure, retention, recording consent) under test.

## Next (0.2 — extensibility)

- A documented practice-management adapter interface with contract tests, so new
  PMS/calendar integrations don't touch core logic.
- A second reference adapter (generic CSV/iCal) implemented against that interface.
- First-pass internationalisation of the control room.
- Load and soak testing for the campaign orchestrator and durable-effects worker.

## Later (towards 1.0)

- Multi-clinic operator tooling at scale (cohorts, release waves, monitoring).
- Additional outreach channels behind the same consent and cap gates.
- Formal compliance artefact generation (audit exports, disclosure reports).

## Contribution areas

These three areas are deliberately scoped so external contributors can work without
credentials, cloud resources, or patient data. All of them run fully offline.

### 1. Integrations & adapters

New ways to get appointment data in and bookings out — behind the deterministic
booking interface, never inside the model.

- Start at [src/clinic_recall/](src/clinic_recall/) (`availability.py`, `booking.py`,
  `detection.py`) and the integration plan in
  [docs/clinic-recall-mvp-integration-plan.md](docs/clinic-recall-mvp-integration-plan.md).
- Contributions: adapter interface feedback, CSV/iCal import edge cases, new adapter
  prototypes with contract tests.
- Ground rules: adapters are deterministic code with tests; no live-provider calls in
  CI; synthetic fixtures only.

### 2. Safety & evaluation corpus

The eval gate is only as good as its dataset. Growing the synthetic corpus is one of
the highest-leverage contributions in the project.

- Start at [assert/](assert/) (`test_set.jsonl`, `taxonomy.json`, `eval_config.yaml`)
  and [.agentops/data/](.agentops/data/).
- Contributions: new personas and edge cases (ambiguous replies, mixed intents,
  non-English messages, opt-out phrasings, clinical mentions that must escalate),
  taxonomy refinements, rubric improvements.
- Ground rules: synthetic data only; every case states its expected safe behaviour;
  cases that weaken the clinical boundary are rejected.

### 3. Control room UX & accessibility

The staff dashboard is where humans make the decisions the AI is not allowed to make —
it should be excellent.

- Start at
  [apps/artagent/frontend/src/components/ClinicRecallSurfaces.jsx](apps/artagent/frontend/src/components/ClinicRecallSurfaces.jsx)
  with Playwright tests in [apps/artagent/frontend/e2e/](apps/artagent/frontend/e2e/).
- Contributions: WCAG 2.2 AA fixes, i18n string extraction, CSV exports, e2e test
  coverage, responsive layout improvements.
- Ground rules: UI runs against deterministic route mocks (no backend needed); new
  behaviour ships with Playwright coverage.

## Out of scope

Wulo-X will not add: medical triage or advice, model-authorised booking/payment,
contact that bypasses consent/quiet-hour/cap gates, or removal of the human approval
loop. See [docs/deterministic-safety-boundary.md](docs/deterministic-safety-boundary.md).

## How to pick something up

Check the [good first issue](https://github.com/Ayo-faks/wulo-x/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
label, comment on the issue to claim it, and read
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
