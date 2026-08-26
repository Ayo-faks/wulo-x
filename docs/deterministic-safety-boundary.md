# The Deterministic Safety Boundary

Wulo-X operates in a clinical-adjacent domain, so it draws one hard line through the
whole system: **the AI model generates language; deterministic, reviewed code makes
every decision that matters.** The model can propose, summarise, and phrase — it can
never authorise.

## What the model may do

- Understand a patient's reply (SMS or voice) and classify intent.
- Draft conversational wording within an approved, versioned prompt.
- Propose next steps (for example, "patient accepted the Thursday slot").

That is the entire mandate. A model proposal is input to code, never an action.

## What deterministic code owns

| Concern | Deterministic rule | The model can never… |
|---|---|---|
| Identity | Patients matched by strict identifiers, ambiguity fails closed ([src/clinic_recall/booking_identity.py](../src/clinic_recall/booking_identity.py)) | resolve an ambiguous identity |
| Consent & opt-out | Per-channel consent gate before any send; one-word STOP suppresses immediately and permanently | override consent or soften an opt-out |
| Contact rules | Quiet hours, frequency caps, and daily volume caps checked before every send/call ([src/clinic_recall/eligibility.py](../src/clinic_recall/eligibility.py)) | schedule contact outside the rules |
| Booking | Slots come from deterministic availability rules; auto-book only when unambiguous, otherwise a staff approval task ([src/clinic_recall/booking.py](../src/clinic_recall/booking.py)) | self-authorise a booking or invent a slot |
| Money & prices | No price, refund, or payment statement is generated — deterministic templates or staff only | quote or negotiate money |
| Clinical content | Urgent, clinical, safeguarding, complaint, or distress signals hard-stop the conversation and escalate to a human ([src/clinic_recall/escalation.py](../src/clinic_recall/escalation.py)) | triage, diagnose, or give medical advice |
| Call termination | A deterministic state machine ends calls; hard-stop signals cannot be talked over | hold a call open against a stop signal |
| External effects | Provider sends/writes go through durable state with reconciliation ([src/clinic_recall/durable/](../src/clinic_recall/durable/)) | retry blindly or write directly to a provider |
| Data isolation | Tenant scoping enforced in the application and with PostgreSQL row-level security | reach across clinic boundaries |

## Fail closed

When anything is uncertain — identity, intent, eligibility, clinical tone — the system
does not guess. It stops, records why, and routes the item to the clinic control room
where a human approves, rejects, or takes over. Every contact, decision, and
escalation is written to an audit trail.

## How the boundary is enforced in the repository

- **Prompt-as-code:** the agent's behaviour lives in
  [.agentops/prompts/recall-agent.prompt.md](../.agentops/prompts/recall-agent.prompt.md)
  and changes only through reviewed diffs.
- **Blocking evaluation gate:** [agentops.yaml](../agentops.yaml) scores every prompt
  change against a rubric where `safe_clinical_boundary` carries the strictest
  threshold; ASSERT safety policies and red-team suites run in the same gate
  ([assert/eval_config.yaml](../assert/eval_config.yaml)). No green gate, no ship.
- **Deterministic modules:** the decision logic in
  [src/clinic_recall/](../src/clinic_recall/) is ordinary, unit-tested Python — it can
  be reviewed, diffed, and reasoned about like any other business logic.
- **Human surfaces:** approvals, escalations, and campaign launch review are staff
  actions in the control room UI, not model outputs.

Contributions must preserve this boundary. If a change would let model output trigger
an irreversible action without a deterministic check or a human decision, it will not
be merged — see [CONTRIBUTING.md](../CONTRIBUTING.md).
