## Summary

Describe the problem and the observable behavior change.

## Validation

List the exact tests, linters, builds, or evaluation gates run.

## Risk

- [ ] Uses only synthetic or redacted data
- [ ] Preserves tenant isolation
- [ ] Preserves deterministic identity, consent, booking, and external-write controls
- [ ] Preserves clinical/urgent/complaint fail-closed behavior
- [ ] Includes migration and rollback notes when applicable
- [ ] Includes prompt evaluation evidence when `.agentops/prompts/` changed
- [ ] Contains no secrets, production identifiers, patient data, or call recordings

## Deployment

State whether the change needs configuration, infrastructure, migration, provider, or rollout work. Write `None` when it does not.
