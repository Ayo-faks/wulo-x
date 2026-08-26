# Contributing to Wulo-X

Thank you for improving Wulo-X. Contributions should preserve its core boundary: models may help with language, but deterministic code controls identity, consent, safety, booking, money, and external writes.

## Before You Start

- Check the [roadmap](ROADMAP.md) for current priorities and the three scoped contribution areas; starter tasks are labelled `good first issue`.
- Search existing issues before opening a new one.
- Use synthetic data only. Never submit patient data, phone numbers, call recordings, credentials, access tokens, resource IDs, or production logs.
- Discuss large behavior or architecture changes in an issue before implementation.
- Report security vulnerabilities privately according to [SECURITY.md](SECURITY.md).

## Development Setup

```bash
git clone https://github.com/Ayo-faks/wulo-x.git
cd wulo-x
uv sync --extra dev
uv run pytest -q
```

For frontend work:

```bash
cd apps/artagent/frontend
npm ci
npm run build
npm run test:e2e
```

For the standalone voice transport package:

```bash
cd voicekit
uv sync --extra dev --extra contract
uv run pytest -q
python evals/run_evals.py --repeats 20
```

## Change Requirements

- Keep changes focused and consistent with existing modules.
- Add tests that fail before the fix and pass afterward.
- Treat provider payloads and model output as untrusted input.
- Preserve tenant scoping, auditability, consent, opt-out, and quiet-hour controls.
- Do not make an AI model authoritative for clinical, identity, booking, or financial decisions.
- Update `.agentops/prompts/` only with corresponding evaluation coverage and a green safety gate.
- Add dependencies only when their license and maintenance posture are suitable for redistribution.

## Pull Requests

A pull request should explain the problem, the behavior change, the tests run, and any security, privacy, deployment, or migration impact. Maintainers may ask for additional evidence when a change affects patient-facing behavior or external side effects.

By submitting a contribution, you agree that it may be distributed under this repository's MIT License.
