# Production Bring-Up Runbook

First-time production deployment of wulo-x (Clinic Recall), plus the gates that
must pass before and after. Production is a fully separate stack: its own
Terraform state (`prod.tfstate`), resource group, **Foundry project**, Entra app
registration, Google OAuth client, and azd environment.

## 0. Preconditions (all must be true before `terraform apply`)

- [ ] The release commit is on `main`; pytest, Playwright, and AgentOps development gates are green.
- [ ] Prod Terraform plan artifact reviewed and explicitly approved by the owner.
- [ ] DNS names such as `clinic.example.com`, `api-origin.example.com`, and `ui-origin.example.com` are ready to point at the production Application Gateway.
- [ ] A separate production Google OAuth client exists with `https://clinic.example.com/.auth/login/google/callback` as an authorized redirect URI. Store its secret only in the production Key Vault.
- [ ] The GitHub `prod` environment has required reviewers and variables (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_AI_FOUNDRY_PROJECT_ENDPOINT`, `AZURE_OPENAI_*`). Configure the federated credential for `repo:<owner>/wulo-x:environment:prod`.

## 1. azd environment

```bash
azd env new prod --no-prompt
azd env select prod
```

Required keys (`azd env set KEY <value>` — names only, values live in the env):

| Key | Purpose |
|---|---|
| `AZURE_ENV_NAME` | `prod` (set by `azd env new`) |
| `AZURE_LOCATION` | deployment region selected for your production environment |
| `AZURE_SUBSCRIPTION_ID` | prod subscription |
| `RS_RESOURCE_GROUP` / `RS_STORAGE_ACCOUNT` / `RS_CONTAINER_NAME` | Terraform remote state |
| `FOUNDRY_PROJECT_ENDPOINT` / `AZURE_AI_FOUNDRY_PROJECT_ENDPOINT` | **prod** Foundry project (created by provision; never staging's) |
| `AGENT_RECALL_AGENT_NAME` / `AGENT_RECALL_AGENT_VERSION` | pinned prod agent version (set by promote workflow) |
| `AGENT_INBOUND_ASSISTANT_NAME` / `AGENT_INBOUND_ASSISTANT_VERSION` | pinned prod inbound agent version |
| `GOOGLE_AUTH_CLIENT_ID` | prod Google OAuth client ID (enables the Google provider) |
| `GOOGLE_SECRET_KV_URI` | Key Vault secret URI for the Google client secret |
| `GOOGLE_SECRET_IDENTITY_ID` | resource ID of the Container App identity with Key Vault Secrets User |
| `ENABLE_GOOGLE_LOGIN` | `true` — renders the Google button (frontend container env) |

## 2. Provision + deploy order

```bash
azd provision          # terraform plan/apply via preprovision hook — REVIEW PLAN FIRST
azd deploy             # container apps + Foundry-hosted agents
./devops/scripts/azd/helpers/enable-easyauth.sh   # Entra + (env-gated) Google EasyAuth
```

Dry-run the auth config before the real run:

```bash
GOOGLE_AUTH_CLIENT_ID=<prod-client-id> ./devops/scripts/azd/helpers/enable-easyauth.sh --dry-run \
  -g <prod-rg> -a <frontend-app> -i <frontend-uai-client-id>
```

Then run database migrations against prod Postgres (private access — run from a
network-permitted host):

```bash
CLINIC_RECALL_DATABASE_URL=<prod-dsn> make db_upgrade   # includes 0011_identity_provider
```

## 3. Prod Foundry evaluation gate (before accepting traffic)

Production Foundry is separate from staging. After agents are deployed and
versions pinned in the `prod` azd env, run the non-destructive gate against the
prod Foundry target and archive evidence separately from staging:

```bash
azd env select prod
make agentops_hydrate_env
make recall_agent_gate            # eval + ASSERT + Red Team + Doctor
make inbound_assistant_gate       # if the inbound path shipped changes
```

Or trigger the **AgentOps Promote (PROD)** workflow (manual dispatch; `prod`
environment approval required). No green gate, no traffic.

## 4. Post-deploy smoke checklist (staging first, then prod)

- [ ] `https://clinic.example.com/` loads the landing page through the WAF.
- [ ] "Sign in with Microsoft" completes; `/app` loads with clinic context.
- [ ] "Sign in with Google" button visible (ENABLE_GOOGLE_LOGIN=true) and completes; WAF does not block `POST /.auth/login/google/callback`.
- [ ] Unmapped test user (Google account with no `clinic_identity_mapping` row) gets 403 "identity is not mapped" — fail closed, no clinic context.
- [ ] Google account whose email matches an Entra mapping does NOT inherit access (provider-scoped matching).
- [ ] One end-to-end recall dry-run against fake providers.
- [ ] Alerts wired: voice latency, gate failures, escalation queue depth.

## 5. Rollback

- **App:** `azd deploy` the previous image tag (Container Apps keeps prior revisions; activate the last-known-good revision).
- **Agents:** re-pin `AGENT_*_VERSION` to the previous gated version in the prod azd env; redeploy.
- **Auth config:** re-run `enable-easyauth.sh` without `GOOGLE_AUTH_CLIENT_ID` to drop the Google provider (Entra unaffected).
- **DB:** `alembic downgrade` only if no non-aad identity rows exist (see 0011 docstring); otherwise fix forward.
- **Infra:** `terraform plan` against the previous commit and review before any destructive apply. Never `azd down` in prod.

## 6. Secret rotation owners

| Secret | Store | Owner | Cadence |
|---|---|---|---|
| Google OAuth client secret (prod) | Prod Key Vault → Container App secret `google-provider-authentication-secret` | TBD — assign before go-live | 90 days |
| Entra EasyAuth | none (FIC, secretless) | n/a | n/a |
| Postgres admin password | Terraform random_password (state) | infra owner | rotate via `terraform apply` |

## 7. Pilot observability and operational rehearsal (PR-14)

Local rehearsal evidence proves the alert and telemetry **contracts** only.
It is never Azure operational evidence: do not claim real alert fire,
automatic resolution, action-group routing, or rollback behavior until an
authorized staging rehearsal proves them against deployed resources.

### 7.1 Dry-run alert fire/resolve rehearsal (local, offline)

The rehearsal probe is deterministic, dry-run only, and cannot send SMS,
place calls, write Cliniko, start recording, or contact Azure or any
provider. It builds synthetic fixture rows for every registered pilot
signal, evaluates the exact alert predicates from
`infra/terraform/monitoring.tf`, and proves that each violating fixture
fires while each healthy fixture does not (fire → resolve when the window
contains only healthy rows).

```bash
python devops/probes/pilot_observability_rehearsal.py            # dry-run (the only mode)
python -m pytest tests/test_clinic_recall_pilot_observability_rehearsal.py -q
```

The read-only aggregate snapshot collector is default-off. To run a bounded
local pass against a disposable database (never production without separate
authority):

```bash
CLINIC_RECALL_OPERATIONAL_SNAPSHOT_ENABLED=true \
  python -m src.clinic_recall.operational_snapshot --clinic-id <clinic> --lookback-hours 24
```

### 7.2 Configuration staleness diagnosis

`pilot.configuration.status` reports `fresh`,
`configuration_identity_missing`, `configuration_evidence_missing`, or
`configuration_stale` from the PR-13 snapshot
(`CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT` vs
`CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS`, capped at 1 hour). On a
staleness alert:

1. Read the deployed keys (names only) under `app/clinic-recall/pilot/*`
   with `az appconfig kv show`; compare `config-refreshed-at` and
   `config-max-age-seconds` against the alert window.
2. If the refresher (sync-appconfig) stopped, re-run it and restart the
   backend so the snapshot is re-read; outreach remains failed closed until
   the snapshot is fresh — this is expected policy behavior, not an
   invariant breach.
3. `pilot_release_mismatch` firing alongside staleness usually means the
   runtime release identity and the database programme disagree; do not
   flip switches to silence it — reconcile the release identity first.

### 7.3 Kill switches (dry-run first, then real under authority)

Two independent kill switch layers must both be rehearsed. The rehearsal
probe proves that pausing/killing never turns safety controls off — pause
only ever removes permission to act.

- **Database pause:** `pause_programme()` (PR-13) sets the programme
  `paused`; every patient/job gate then denies with a closed reason and
  undispatched effects are canceled deterministically.
- **App Configuration kill:** set `app/clinic-recall/pilot/outreach-enabled`
  (and voice/recording keys) to `false`; the snapshot fails closed on the
  next refresh even if the database pause were unavailable.

### 7.4 Control-first rollback order

Roll back controls before code. The order is mandatory:

1. **database pause** — pause the pilot programme (removes all outreach,
   voice, and recording permission at the deterministic gate);
2. **App Configuration off** — set the pilot operational switches false so a
   restarted or rolled-back image also starts closed;
3. **Jobs and recording stopped** — confirm Container Apps Jobs are not
   executing and no recording is active;
4. **code/image rollback** — only then roll back the container image/agent
   version per §5, with outreach still disabled.

Monitoring changes themselves are independently reversible: removing the
PR-14 alerts or the Pilot Operations Workbook page never changes runtime
behavior, and new alert notification paths stay inert without approved
receivers in `monitor_alert_email_receivers`.

## Gate summary (no task is "done" without evidence)

| Gate | Command / surface | Evidence |
|---|---|---|
| Backend auth tests | `uv run pytest tests/test_clinic_recall_surfaces_api.py -q` | CI run link |
| Migration round-trip | alembic upgrade → downgrade → upgrade | CI/terminal log |
| UI providers | `npx playwright test e2e/auth-login-providers.spec.js` | Playwright report |
| Infra | `terraform validate` + reviewed prod plan artifact | plan file in PR |
| Auth config | `enable-easyauth.sh --dry-run` (redacted) | log in PR |
| Agent safety | `make recall_agent_gate` vs prod Foundry | `.agentops/results/latest`, Doctor evidence pack |
| Smoke | §4 checklist in staging, then prod | ticked checklist in release notes |
