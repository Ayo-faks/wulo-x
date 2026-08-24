# Security Policy

## Supported Versions

Wulo-X is pre-1.0. Security fixes are applied to the latest release and `main`; older snapshots are not supported.

## Reporting a Vulnerability

Do not open a public issue for a suspected vulnerability. Use [GitHub private vulnerability reporting](https://github.com/Ayo-faks/wulo-x/security/advisories/new).

Include the affected revision, impact, prerequisites, and the smallest synthetic reproduction you can provide. Do not include patient information, credentials, call recordings, production resource identifiers, or unredacted logs. If a credential has been exposed, revoke or rotate it through the owning provider immediately rather than sending it to maintainers.

Maintainers will keep reports private while they investigate and coordinate a fix. Please allow time for remediation before public disclosure.

## Deployment Responsibility

Wulo-X is not a medical device and does not provide medical advice. Operators are responsible for their own privacy impact assessment, clinical-safety review, telecoms compliance, access controls, retention policy, incident response, and release approval before processing real patient data.

## Threat Model

| Risk | Where | Control |
|---|---|---|
| Prompt injection via tool output | PMS/calendar data, patient SMS replies | Treat all external text as untrusted; never let it change booking rules or escalate privileges. |
| Secret leakage | API keys (PMS, ACS, Azure) | Secrets only in Key Vault / env; never in code, prompts, logs, or git. |
| PII exposure | Patient name, phone, health context | Data minimisation; least-privilege access; encrypt in transit/at rest; per-clinic isolation. |
| Unsafe autonomous action | Booking, money, clinical advice | Deterministic gates; model cannot self-authorise; fail closed + escalate. |
| Over-broad tooling | Agent tools | Each tool does one thing and enforces its own rules; no general "do anything" tool. |
| Consent / opt-out failure | Outreach | Consent gating + immediate, permanent opt-out, always logged. |

## Security Checklist

- [ ] No secrets in diff (`sk-`, `ghp_`, `AKIA`, connection strings, keys).
- [ ] All external input (tool output, patient replies) treated as untrusted — does not control flow or rules.
- [ ] No new tool grants the agent broad/unbounded capability.
- [ ] Patient PII is minimised, access-scoped per clinic, and never logged in plaintext beyond audit needs.
- [ ] Booking / clinical / money actions remain deterministic and human-gated.
- [ ] Opt-out, consent, quiet-hours, and frequency caps still enforced.
- [ ] Audit trail still records contact, response, action, and escalation.
- [ ] OWASP Top 10 reviewed for any new endpoint or data path.

## Product-Agent Safety Gates

These run in CI via `agentops.yaml` (see the AgentOps tutorial steps 11–12):

- **ASSERT** — natural-language safety policies as executable tests (e.g. "must refuse to give medical advice", "must escalate urgent symptoms", "must honour opt-out").
- **AI Red Team** — adversarial prompts across risk categories; fail if attack-success-rate exceeds threshold.

A prompt change that weakens the `safe_clinical_boundary` rubric dimension must not merge.
