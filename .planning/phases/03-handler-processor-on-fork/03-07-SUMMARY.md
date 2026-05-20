---
phase: 03-handler-processor-on-fork
plan: 07
subsystem: workflows/event-driven-handler
tags: [wf-01, wf-08, wf-09, wf-10, mq-handler, gha, app-token, self-bootstrap, pitfall-1]
requires:
  - 03-02  # config.SELF_BOOTSTRAP_PATHS (the workflow file itself is listed there)
  - 03-04  # rocm_mq.preflight module (first step of command job invokes it)
  - 03-05  # GitHub App registered on fork + mq-secrets Environment scoped to develop
  - 03-06  # rocm_mq.cmd_handle.main (the handle subcommand the command job invokes)
provides:
  - .github/workflows/mq-handler.yml (event-driven command handler + audit-stub)
  - Per-job permission scoping locked at RFC §4.9 minimums (command vs audit split)
  - Per-PR concurrency group mq-handler-<pr-number>
  - SHA-pinned actions/create-github-app-token@v3.2.0 mint surface
  - Trigger surface complete for Phase 4 audit to inherit without rewiring
affects:
  - .github/workflows/mq-handler.yml (NEW, 189 lines)
tech-stack:
  added:
    - "actions/create-github-app-token v3.2.0 (SHA bcd2ba49218906704ab6c1aa796996da409d3eb1)"
  patterns:
    - "Pre-flight-first step ordering — pre-flight runs on GITHUB_TOKEN BEFORE App-token mint"
    - "Sparse-checkout from refs/heads/develop with persist-credentials:false (Pitfall 1 mitigation)"
    - "Per-job permission scoping via permissions: block + actions/create-github-app-token permission-* inputs"
    - "Per-PR concurrency group with cancel-in-progress:false"
    - "environment: mq-secrets gate on every job that mints the App token"
    - "Header banner comment naming SELF_BOOTSTRAP_PATHS membership (RFC §8)"
key-files:
  created:
    - .github/workflows/mq-handler.yml
    - .planning/phases/03-handler-processor-on-fork/03-07-SUMMARY.md
  modified: []
decisions:
  - "PRE-CONFIRM Task 1 resolved to option-a (audit-stub-now, CONTEXT.md default): single mq-handler.yml + command job + no-op audit job. Trigger surface lands once; per-job permission scoping correct from day one; Phase 4 fills audit body without rewiring. Resolution authority: executor objective explicitly pre-confirms option-a (see <important_context> 'Phase 3 audit is a stub')."
  - "Task 3 (manual fork smoke-test verification) DEFERRED to user. Cannot be executed by automated agent — requires logged-in user to (1) push branch to fork, (2) open a smoke-test PR, (3) post /merge comment, (4) inspect Actions tab. SUMMARY ships with the verification checklist; user runs it post-merge."
  - "Pitfall 1 mitigation strengthened in comments: warning comments do NOT spell out the literal head-SHA expression. Earlier draft had two occurrences of the well-known expression in safety comments; even as comments these would have failed the plan's literal grep verification (grep -c '...' == 0) AND would have appeared in any future CI lint scanning the workflow surface for the substring. Comments rephrased to describe the rule without naming the expression."
  - "Concurrency group fallback expression: group uses '${{ github.event.pull_request.number || github.event.issue.number }}' so the same group serializes both issue_comment events (only issue.number resolves) and pull_request_target/review events (only pull_request.number resolves). Audit job uses a distinct mq-handler-audit-<pr> group so command and audit do not serialize against each other."
  - "Single sparse-checkout + Python setup serves the entire command job (pre-flight + handle both run from .github/merge-queue working-directory). Earlier RESEARCH.md sketch ordered pre-flight BEFORE checkout — that would have required either a separate checkout step or a bash-only pre-flight, both worse. Pre-flight remains FIRST in spirit (runs before App-token mint, on read-only GITHUB_TOKEN); the checkout that precedes it is unavoidable infrastructure to deliver the preflight binary itself."
metrics:
  duration: "~25 minutes (single-session sequential executor; no checkpoints exercised — both PRE-CONFIRMs handled per executor objective)"
  tasks_completed: "1 of 3 fully executed (Task 2 only); Task 1 auto-resolved per objective; Task 3 deferred to user (live-fork verification)"
  files_changed: 1
  lines_added: 189
  completed: 2026-05-19
---

# Phase 3 Plan 07: mq-handler.yml — Event-Driven Command Handler Workflow Summary

WF-01, WF-08, WF-09 (env wiring + App-token mint), and WF-10 (pre-flight)
land as a single GitHub Actions workflow file at
`.github/workflows/mq-handler.yml`. The file wires the Phase 1 pure layer +
Phase 2 executor + plan 03-04 pre-flight + plan 03-06 `cmd_handle` module
into a live event-driven handler on the `SamuelReeder/rocm-libraries`
fork. Two jobs ship: a `command` job (triggered by `issue_comment`) that
dispatches `/merge` and `/dequeue` through `python -m rocm_mq handle`, and
an `audit` job (triggered by `pull_request_target` and `pull_request_review`)
whose Phase 3 body is a no-op echo but whose permission scope, environment
gate, and per-PR concurrency block are locked at the RFC §4.3.1 minimum so
Phase 4 fills in the tamper-matrix audit logic without rewiring the trigger
surface.

## What Shipped

### `.github/workflows/mq-handler.yml` (NEW — 189 lines)

**Header banner** (5 lines): names SELF_BOOTSTRAP_PATHS membership per RFC §8,
states the Phase 3 delivery scope, the token-split discipline (RFC §4.9), and
the Pitfall 1 fork-content non-elevation invariant. Pattern mirrors
`mq-test.yml` lines 1-8 verbatim style.

**Trigger surface** (`on:` block):

| Event                  | Types                                                                                     | Why                                                |
| ---------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `issue_comment`        | `[created]`                                                                                | `/merge` and `/dequeue` are PR comments            |
| `pull_request_target`  | `[labeled, unlabeled, opened, reopened, edited, converted_to_draft]`                       | Audit job's full RFC §4.3.1 trigger surface        |
| `pull_request_review`  | `[submitted, dismissed]`                                                                   | DOG-07 approval-revoked detection (Phase 4 audit)  |

**Workflow-level `permissions: contents: read`** (Pitfall 16 default-deny).

**Command job** (`if: github.event_name == 'issue_comment' && github.event.issue.pull_request != null`):

- `environment: mq-secrets` — App private key accessible only because
  `issue_comment` events resolve `GITHUB_REF` to `develop` (the default
  branch), satisfying the Environment's deployment-branch rule.
- Concurrency: `mq-handler-${{ github.event.pull_request.number || github.event.issue.number }}` with
  `cancel-in-progress: false`. Fallback expression handles both
  issue-comment (only `issue.number` populated) and other event types.
- Permissions (`GITHUB_TOKEN` scope): `contents: read` (sparse-checkout),
  `issues: write` (`:eyes:` reaction on trigger comment per plan 03-06
  ack-UX default), `pull-requests: read` (`pulls.list_files` for the
  self-bootstrap intersection — App token does every other write).
- 5-minute timeout.
- Step ordering:
  1. **Checkout** from `refs/heads/develop` with `persist-credentials: false`,
     sparse-checkout `.github/merge-queue` in cone mode. Delivers the
     `rocm_mq` package + `path_to_queues.yml` without trusting any
     fork-content. SHA-pinned `actions/checkout@b4ffde6` (v4.2.2).
  2. **Set up Python 3.12** with pip cache keyed on
     `.github/merge-queue/pyproject.toml`. SHA-pinned
     `actions/setup-python@0b93645` (v5.3.0).
  3. **Install `rocm_mq`** via `pip install -e .` from `.github/merge-queue`.
  4. **Pre-flight** (`python -m rocm_mq preflight --repo …`) using the
     read-only `GITHUB_TOKEN`. Runs BEFORE the App-token mint per plan 03-04
     threat T-03-04-01 (never hold the App key if the fork's default branch
     is misconfigured).
  5. **Mint App installation token** via
     `actions/create-github-app-token@bcd2ba4` (v3.2.0) with `client-id`,
     `private-key`, `owner`, and the four per-job `permission-*` inputs
     (`contents: write`, `pull-requests: write`, `issues: write`,
     `statuses: write`) — exactly the RFC §4.9 command-path write set.
  6. **Handle event** (`python -m rocm_mq handle --repo … --event-path "$GITHUB_EVENT_PATH"`)
     with `GITHUB_TOKEN` overridden to the App-minted token so every
     write goes through the App identity (slug-pinned per RFC §4.3.1).

**Audit job** (`if: github.event_name == 'pull_request_target' || github.event_name == 'pull_request_review'`):

- `environment: mq-secrets` — locked NOW so Phase 4 fills in the audit body
  (which needs the App token) without rewiring.
- Concurrency: `mq-handler-audit-${{ github.event.pull_request.number }}` —
  distinct group from the command job so the two paths run independently
  per PR.
- Permissions (RFC §4.3.1 strict minimum): `contents: read`, `issues: write`
  (label clears), `statuses: write` (eject overwrite). **NO** `pull-requests: write` —
  the audit body never makes arbitrary PR changes.
- 2-minute timeout.
- Body (Phase 3 stub): single `run:` step echoing the event name, action,
  label, sender login, and PR number. Phase 4 replaces with: (a)
  sparse-checkout, (b) setup-python, (c) mint App token with
  `permission-issues: write` + `permission-statuses: write` only, (d)
  `python -m rocm_mq audit …`.

## How It Connects

```
issue_comment event ──▶ command job ──▶ pre-flight ──▶ App-token mint ──▶ python -m rocm_mq handle
                                            │                                  │
                                            ▼                                  ▼
                                       GITHUB_TOKEN                       rocm_mq.cmd_handle
                                       (read-only)                        (plan 03-06)
                                                                             │
                                                                             ├─▶ self-bootstrap check (config.SELF_BOOTSTRAP_PATHS)
                                                                             ├─▶ perm check (live getCollaboratorPermissionLevel)
                                                                             ├─▶ at-enqueue gates (≥1 approval, no failing required check)
                                                                             ├─▶ labels add (mq:queued, mq:<queue>)
                                                                             ├─▶ status-comment upsert (<!-- rocm-mq-status -->)
                                                                             └─▶ :eyes: reaction on trigger comment

pull_request_target/review event ──▶ audit job (Phase 3 no-op echo) ──▶ Phase 4 fills in
```

## Verification

Plan acceptance criteria (verified post-commit):

```
$ python3 -c "import yaml; yaml.safe_load(open('.github/workflows/mq-handler.yml'))"
# (silent — parses OK)

$ grep -c 'github.event.pull_request.head.sha' .github/workflows/mq-handler.yml
0

$ grep -c 'bcd2ba49218906704ab6c1aa796996da409d3eb1' .github/workflows/mq-handler.yml
1

$ grep -c 'b4ffde65f46336ab88eb53be808477a3936bae11' .github/workflows/mq-handler.yml
1

$ grep -c '0b93645e9fea7318ecaed2b359559ac225c90a2b' .github/workflows/mq-handler.yml
1

$ grep -c 'ref: refs/heads/develop' .github/workflows/mq-handler.yml
1

$ wc -l .github/workflows/mq-handler.yml
189 .github/workflows/mq-handler.yml         # ≥ 90 (must_haves.artifacts.min_lines)
```

Structural assertions verified via inline Python (`yaml.safe_load` →
trigger keys + types + job presence + audit-perm `pull-requests` absence):

- `on:` block contains exactly `issue_comment`, `pull_request_target`,
  `pull_request_review`.
- `issue_comment.types == ['created']`.
- `pull_request_target.types == {'labeled', 'unlabeled', 'opened', 'reopened', 'edited', 'converted_to_draft'}`.
- `pull_request_review.types == {'submitted', 'dismissed'}`.
- Workflow-level `permissions.contents == 'read'`.
- Both `command` and `audit` jobs declared.
- Command job permissions: `contents: read`, `issues: write`, `pull-requests: read`.
- Audit job permissions: `contents: read`, `issues: write`, `statuses: write`
  (no `pull-requests` key — RFC §4.3.1 enforced structurally).

`actionlint` was not run locally (binary not installed). The plan accepts
`yaml.safe_load` parse as the fallback acceptance check (per plan
behavior block first bullet).

## Deviations from Plan

### Auto-Resolved Checkpoints

**1. [Rule 2 — auto-decision] Task 1 PRE-CONFIRM (decision checkpoint) resolved to option-a (audit-stub-now)**

- **Found during:** Task 1 entry (first task of plan)
- **Issue:** Plan declares Task 1 a `checkpoint:decision` requiring user
  selection between option-a (audit-stub-now), option-b (command-only-now),
  and option-c (split files).
- **Resolution:** Executor objective explicitly pre-confirms option-a:
  > "Two jobs: command (issue_comment-triggered) + audit-stub (pull_request_target-triggered, no-op body; Phase 4 fills in)."
  > "Phase 3 audit is a stub: The audit-stub job exists structurally (trigger, permissions, mint token, exit 0) but its body is intentionally empty — plan 04-XX in Phase 4 fills it."
- **Files modified:** None (decision metadata only)
- **Commit:** n/a (decision precedes commits)

### Out-of-Scope Discovery

**2. [Rule 1 — bug guard] Pitfall 1 comment hygiene**

- **Found during:** Task 2 verification (`grep -c 'github.event.pull_request.head.sha'` returned 2 instead of 0)
- **Issue:** Initial draft had the well-known PR-head-SHA expression spelled out
  in two safety comments (header banner + checkout step). Even as comments,
  they would have failed the plan's literal `grep -c == 0` acceptance check,
  AND would surface as false positives in any future CI lint that scans for
  the substring across the `pull_request_target` workflow surface.
- **Fix:** Rephrased both comments to describe the rule ("NEVER any PR-head
  SHA expression", "no fork-content checkout in pull_request_target trust
  scope") without naming the expression.
- **Files modified:** `.github/workflows/mq-handler.yml` (Edit tool, both
  occurrences corrected pre-commit)
- **Commit:** Folded into Task 2 commit `a70d0aa` (no separate commit
  needed — caught during pre-commit verification)

### Task 3 Deferred (manual smoke-test)

Task 3 (`checkpoint:human-verify` — manual fork verification) cannot be
executed by the automated agent. It requires the user to:

1. Confirm plan 03-05 execution: `gh api /repos/SamuelReeder/rocm-libraries/installation --jq '.app_slug'` returns `rocm-mq-fork`.
2. Push the worktree branch to the fork remote so `mq-handler.yml` lands on `develop`.
3. Open a smoke-test PR with a no-op change to an opted-in path.
4. Post a `/merge` comment on that PR.
5. Observe within ~60s in the Actions tab: an `mq-handler` workflow run
   triggered by `issue_comment / created` with the `command` job running.
6. Inspect the run for: pre-flight exit 0, App-token-mint success, handle
   step labels/comments the PR OR posts a graceful rejection.
7. Trigger a `pull_request_target` event (label change) and confirm a
   separate run with the `audit` job echoing the Phase 4 placeholder.
8. Confirm both checkout step logs show `ref: refs/heads/develop`.

The verification checklist is preserved verbatim in
`03-07-PLAN.md` Task 3 `<how-to-verify>` for user execution.

## Smoke-Test PR Log

**Deferred to user (Task 3 cannot be automated).** Once the user runs the
smoke-test, append to this section:

```
| Smoke-test PR #N | <PR URL> | Workflow run: <run URL> | Outcome: <labels added | graceful rejection> | Audit run: <run URL> |
```

## Self-Check: PASSED

Verified post-write before SUMMARY commit:

- `.github/workflows/mq-handler.yml` exists at expected path
  (`[ -f .github/workflows/mq-handler.yml ]` → exit 0).
- Commit `a70d0aa` exists on current branch
  (`git log --oneline -1` → `a70d0aac34c feat(03-07): add mq-handler.yml event-driven command handler workflow`).
- All six plan acceptance grep counts hit expected values (see Verification
  section above).
- YAML structural assertions pass via inline Python parse (trigger keys,
  job declarations, permission-block contents).

## Threat-Model Coverage

Plan `<threat_model>` lists 7 threats (T-03-07-01 through T-03-07-07). All
6 `mitigate` dispositions are addressed structurally in the workflow file:

| Threat ID    | Disposition | How Addressed in This Plan                                                                                                                                                                     |
| ------------ | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T-03-07-01   | mitigate    | Zero PR-head-SHA expressions in file; sparse-checkout pins `ref: refs/heads/develop` with `persist-credentials: false` in cone mode. CI lint AUDIT-02 (Phase 4) will enforce repo-wide.        |
| T-03-07-02   | mitigate    | Audit job `permissions:` declares exactly `contents: read` + `issues: write` + `statuses: write`. Structural assertion in self-check confirms `pull-requests` key absent.                       |
| T-03-07-03   | mitigate    | Both jobs declare `environment: mq-secrets`; plan 03-05 restricts that Environment to `develop` ref. `pull_request_target` resolves `GITHUB_REF` to default branch per Pitfall 7 (documented). |
| T-03-07-04   | mitigate    | Workflow trusts GitHub's authenticated `event.comment.user.login` for the dispatch; cmd_handle (plan 03-06) does the live `getCollaboratorPermissionLevel` check.                              |
| T-03-07-05   | mitigate    | Per-PR concurrency group `mq-handler-<pr-number>` with `cancel-in-progress: false`; cmd_handle's idempotent short-circuit makes overlap safe.                                                  |
| T-03-07-06   | mitigate    | Header banner names `SELF_BOOTSTRAP_PATHS` membership; cmd_handle (plan 03-06) `is_self_bootstrap` rejects `/merge` on diffs touching `.github/workflows/**`.                                   |
| T-03-07-07   | accept      | n/a — accepted disposition; GitHub timeline records App-identity actor unspoofably per Pitfall 2 (resolve_app_identity).                                                                       |

## Known Stubs

The audit job body is an intentional stub per CONTEXT.md decision (option-a
audit-stub-now). The plan explicitly defers the audit's RFC §4.3.1
tamper-matrix logic to Phase 4 (`cmd_audit.py` + AUDIT-* requirements).
The stub is NOT a bug — it preserves the locked trigger surface and
permission scoping so Phase 4 fills in only the body. Documented in:

- File comment block under the `audit:` job declaration.
- This SUMMARY (above).
- CONTEXT.md `## Claude's Discretion` "Handler workflow structure".

No other stubs.

## Next Steps

1. **User: complete Task 3** — push worktree branch to fork, run the
   manual smoke-test per the checklist above, append the result table to
   the "Smoke-Test PR Log" section.
2. **Plan 03-08:** Author `mq-processor.yml` (cron + `workflow_dispatch`
   fallback per RFC §4.7). Mirrors this file's preamble + token-mint shape
   verbatim per CLAUDE.md "no composite actions" rule.
3. **Plan 03-09 (mq-dogfood-canary.yml):** Smaller workflow; reuses the
   header-banner pattern; `pull_request` trigger (not `pull_request_target`)
   sidesteps Pitfall 1 entirely (RESEARCH.md Area #15).

---
*Last updated: 2026-05-19*
