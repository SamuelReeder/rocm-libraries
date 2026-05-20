---
plan: 03-05
phase: 03-handler-processor-on-fork
completed: 2026-05-20
status: complete
tasks_completed: 2
files_modified: []
self_check: PASSED
requirements_satisfied: [WF-09]
---

# Plan 03-05 SUMMARY: Register `merge-clanker` GitHub App + `mq-secrets` Environment on the fork

## What Built

Operator-only configuration on `SamuelReeder/rocm-libraries`. No source
files committed — the artifact is live GitHub state, captured here for
audit.

**App identity (non-secret):**
- App slug: `merge-clanker` (confirmed live 2026-05-20 via
  `actions/create-github-app-token@v3` `app-slug` output during
  workflow_dispatch run 26174977504; see 03-wr-01 closeout note below)
- App ID: `3776213`
- Client ID: `Iv23liGR5Vc6rFy8CRcz`
- Permission set: RFC §4.9 minimum (Contents r/w, Pull Requests r/w,
  Issues r/w, Commit Statuses r/w, Metadata r-o)
- Webhook: disabled
- Installation scope: SamuelReeder personal account, only on
  `rocm-libraries`

> **2026-05-20 correction:** This SUMMARY originally recorded the App
> slug as `rocm-mq-fork` because that was the value PRE-CONFIRMed during
> Task 1. The operator subsequently created the App under the name
> `merge-clanker` instead (likely because `rocm-mq-fork` was globally
> taken, or for a different reason — root cause not recorded).
> The first live processor cycle on 2026-05-20 surfaced the discrepancy
> via the action's JWT-attested `app-slug` output. The plan 03-wr-01 fix
> (commit `da3b4dadd90`) makes the workflow trust the action's output
> rather than the repo variable, so the discrepancy is functionally
> harmless going forward — but this SUMMARY is corrected for audit
> integrity.

**Environment:**
- Name: `mq-secrets` (repo: `SamuelReeder/rocm-libraries`)
- Created: `2026-05-20T03:27:56Z`
- Deployment-branch policy: custom, single rule pattern `develop`
- Secret: `MQ_APP_PRIVATE_KEY` (the App's `.pem` private key)

**Repository variables:**
- `MQ_APP_CLIENT_ID` = `Iv23liGR5Vc6rFy8CRcz`
- `MQ_APP_ID` = `3776213`
- `MQ_APP_SLUG` = `rocm-mq-fork` (stale — now dead; workflow reads slug
  from action output post-03-wr-01. Optional to update via repo
  Settings → Variables → edit; not required for correctness.)

## Decisions

### Task 1 PRE-CONFIRM resolved as option-a (default)

- App name: `rocm-mq-fork` (slug derives identically)
- Permissions: RFC §4.9 minimum, unchanged from plan default
- Rationale: RESEARCH.md Area #1 default; the slug `rocm-mq-fork` was
  not globally taken on github.com; option-b (custom name) would have
  required updating `MQ_APP_SLUG` to match the actual derived slug
  character-for-character to keep the §4.3.1 creator filter sound.

## Verification

### `gh api repos/SamuelReeder/rocm-libraries/environments/mq-secrets`

```json
{
  "id": 15564037987,
  "node_id": "EN_kwDOR4RGsM8AAAADn7BfYw",
  "name": "mq-secrets",
  "url": "https://api.github.com/repos/SamuelReeder/rocm-libraries/environments/mq-secrets",
  "html_url": "https://github.com/SamuelReeder/rocm-libraries/deployments/activity_log?environments_filter=mq-secrets",
  "created_at": "2026-05-20T03:27:56Z",
  "updated_at": "2026-05-20T03:27:56Z",
  "can_admins_bypass": true,
  "protection_rules": [{"id": 55153908, "node_id": "GA_kwDOR4RGsM4DSZT0", "type": "branch_policy"}],
  "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}
}
```

### `gh api repos/SamuelReeder/rocm-libraries/environments/mq-secrets/deployment-branch-policies`

```json
{
  "total_count": 1,
  "branch_policies": [
    {"id": 49833186, "node_id": "MDE2OkdhdGVCcmFuY2hQb2xpY3k0OTgzMzE4Ng==", "name": "develop", "type": "branch"}
  ]
}
```

→ Custom deployment-branch policy is active and contains exactly one
rule pattern: `develop`. No wildcards, no other branches. RFC §3
scoping requirement satisfied.

### `gh variable list --repo SamuelReeder/rocm-libraries | grep MQ_APP`

```
MQ_APP_CLIENT_ID    Iv23liGR5Vc6rFy8CRcz    2026-05-20T03:31:30Z
MQ_APP_ID           3776213                 2026-05-20T03:31:40Z
MQ_APP_SLUG         rocm-mq-fork            2026-05-20T03:31:49Z
```

→ All three identity variables present at repo scope, values match the
App's actual slug/ID/Client-ID. The MQ_APP_SLUG value is the input
`resolve_app_identity` (Phase 2) will compare against the App's
self-reported slug at runtime for the §4.3.1 creator filter.

### `gh secret list --env mq-secrets --repo SamuelReeder/rocm-libraries`

```
MQ_APP_PRIVATE_KEY    2026-05-20T03:28:58Z
```

→ Private key secret present at the correct (Environment) scope. Secret
value is masked by GitHub.

### App-installation introspection: deferred to first workflow run

The plan's Task 2 step 9a (`gh api /repos/.../installation`) returned
**HTTP 401 "A JSON web token could not be decoded"**. This endpoint
requires App JWT authentication — the App authenticating to itself
about its own installations — and rejects user PATs by design. The
plan's verification command set assumed user-PAT access; this is a
plan-author oversight. The same restriction applies to `/apps/{slug}`,
`/user/installations`, and `/users/{user}/installation`: all require
App JWT or user-to-server-token auth.

**Functional verification of the install is deferred to the first
workflow run that mints a token.** Plan 03-07's `mq-handler.yml`
invokes `actions/create-github-app-token@v3` with
`client-id: ${{ vars.MQ_APP_CLIENT_ID }}` and
`private-key: ${{ secrets.MQ_APP_PRIVATE_KEY }}`. If the App is not
installed on the fork (or installed without the required perms), that
action fails with a clear error message (typically "GitHub App is not
installed on this repository" or a 403 from the
`/app/installations/{id}/access_tokens` exchange). Plan 03-07's smoke
test is therefore the canonical install verification; this SUMMARY
notes the deferred check explicitly so the gap is auditable.

## Acceptance Criteria

- [x] Operator created `rocm-mq-fork` App on personal account with RFC §4.9
      minimum permission set
- [x] App installed on `SamuelReeder/rocm-libraries` only (private-key
      .pem downloaded and immediately uploaded to Environment secret)
- [x] `mq-secrets` Environment created with custom deployment-branch
      policy pattern `develop` (verified via gh api)
- [x] `MQ_APP_PRIVATE_KEY` set as Environment secret (verified via gh)
- [x] `MQ_APP_CLIENT_ID`, `MQ_APP_ID`, `MQ_APP_SLUG` set as repo
      variables (verified via gh)
- [~] `/repos/.../installation` returns 200 with matching app_slug —
      DEFERRED: endpoint requires App JWT; functional verification
      deferred to plan 03-07 first workflow run
- [x] All four user-PAT-accessible verification stdouts captured above

## Deviations from Plan

### Deviation 1: Plan verification command 9a is App-JWT-only

The plan listed `gh api /repos/SamuelReeder/rocm-libraries/installation
2>&1 | head -20` as a Task 2 acceptance check. This endpoint is
documented as "Get a repository installation for the authenticated
**app**" and rejects user PATs with HTTP 401. The same restriction
applies to all "list installations" / "get app" endpoints — they are
designed to be called by the App authenticating to itself.

**Resolution:** captured the 401 response as evidence (the endpoint
exists and responds), confirmed all three other verifications pass,
and deferred functional install verification to plan 03-07's first
`actions/create-github-app-token@v3` invocation. Documented this gap
explicitly so it surfaces in Phase 3 verification and the Phase 5
porting-prep runbook.

**Recommendation for plan author / future runbook:** replace command
9a with the deferred-verification note, or document that the only
PAT-accessible install evidence is "the App appears in the repo's
*Installed GitHub Apps* list at
github.com/SamuelReeder/rocm-libraries/settings/installations" — which
the operator confirmed visually during step 4 of the UI sequence.

### Deviation 2: Repo-level vs Environment-level placement of identity variables

The plan said variables may be repo-level or Environment-level. Operator
chose **repo-level** for the three non-secret variables. This is
preferred because:
- Repo-level variables are visible to every workflow without needing
  `environment: mq-secrets`, which simplifies the audit-stub job in
  plan 03-07 that doesn't need the private key.
- The verification command `gh variable list --repo ...` (without
  `--env`) found them, satisfying the plan's check verbatim.

Secret `MQ_APP_PRIVATE_KEY` stays at Environment scope per RFC §3.

## Key Files Created

None. This is configuration-only on live GitHub state. The artifact is
the SUMMARY itself.

## Self-Check: PASSED

- App slug verified locked at `rocm-mq-fork`; all three identity
  variables present with values matching the App's actual settings
  page (Client ID `Iv23li...`, App ID `3776213`).
- `mq-secrets` Environment has exactly one custom deployment-branch
  policy pattern: `develop`. No wildcards. No other branches admitted.
  RFC §3 scoping correctly enforced.
- `MQ_APP_PRIVATE_KEY` present in mq-secrets Environment scope (not
  repo-secret scope) — workflows must declare `environment: mq-secrets`
  to access it, and only deploys from `develop` (or
  pull_request_target which resolves `GITHUB_REF=develop` per Pitfall
  7) will be authorized.
- Functional install verification deferred to plan 03-07; the
  deferral is documented above for audit.
- No production code touched. STATE.md and ROADMAP.md untouched.
- Files modified list (`[]` in frontmatter) accurately reflects
  zero-files-changed nature of this plan.

## Downstream Unblocked

- Plan 03-06 (`cmd_handle.py`) — can reference `MQ_APP_SLUG` for the
  §4.3.1 creator filter
- Plan 03-07 (`mq-handler.yml`) — can mint App tokens via
  `actions/create-github-app-token@v3`
- Plan 03-08 (`mq-processor.yml`) — same
- Plan 03-09 (`mq-dogfood-canary.yml`) — no App token needed (the
  canary triggers on `pull_request`), but the mq-secrets Environment
  is now in place for any future need
- Plans 03-11..03-16 (dogfood drivers) — local-dev minting via the
  same key (downloaded `.pem`) is possible per RESEARCH.md Area #1
