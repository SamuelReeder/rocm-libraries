---
status: partial
phase: 03-handler-processor-on-fork
source: [03-VERIFICATION.md]
started: 2026-05-20T00:00:00Z
updated: 2026-05-21T04:50:00Z
---

## Current Test

[9 of 10 items resolved; only DOG-07 (approval revoked) remains blocked
on a second collaborator account on the fork.]

## Tests

### 1. Deploy Phase 3 workflows to `develop` on the fork
expected: After this branch's PR merges to `develop` on `SamuelReeder/rocm-libraries`, `mq-processor` cron begins firing every 3 min and posts `$GITHUB_STEP_SUMMARY` entries visible at https://github.com/SamuelReeder/rocm-libraries/actions/workflows/mq-processor.yml. `mq-handler` subscribes to `issue_comment` + `pull_request_target` + `pull_request_review` events with the per-job App-token mint succeeding (verifiable from the workflow run logs showing the `create-github-app-token@bcd2ba49...` step's post-step revoke).
result: passed (2026-05-20). Direct-pushed feature branch to fork/develop. mq-processor cron firing (cron is heavily throttled on personal repos — actual interval ~30-60 min, so we dispatch manually via `gh workflow run` for dogfood). mq-handler responds to issue_comment within ~40-60s. App-token mint working end-to-end after 03-wr-01 fix; live App slug is `merge-clanker`. All 7 queues from `path_to_queues.yml` visible in processor cycle summary after 03-wr-02.

### 2. DOG-02 — merge conflict at activation
expected: `python -m rocm_mq.dogfood.dog_02 --owner SamuelReeder --repo rocm-libraries` exits 0 within 12 min; writes `.planning/phases/03-handler-processor-on-fork/dogfood-runs/dog_02-<utc>.json` with `status: pass`; eject status comment contains the verbatim phrase `merge conflict with develop`.
result: passed (2026-05-21T04:31Z). PR #38 ejected after manual processor cycle saw the 422 from develop→PR merge attempt. Required fixes en route: (03-wr-11) `_handle_eject` now upserts a user-visible status comment, (03-wr-11c) `_handle_activate` 409 ejects with the documented reason instead of leaving the PR stuck in mq:queued, (03-wr-11d) drivers handle existing seed-file paths on re-runs. JSON: `dogfood-runs/2026-05-21T04-30-46.580255+00-00-dog_02.json`.

### 3. DOG-03 — CI failure during evaluation
expected: `python -m rocm_mq.dogfood.dog_03 --owner SamuelReeder --repo rocm-libraries` exits 0 within 20 min; JSON output `status: pass`; eject status comment names the canary check.
result: passed (2026-05-21T04:19Z). PR #37 ejected when activation cycle attempted squash → repo ruleset blocked with `Required status check "mq-dogfood-canary" is failing` → executor's 405/422 translation ejected with that message as the reason. JSON: `dogfood-runs/2026-05-21T04-14-13.356284+00-00-dog_03.json`. Prerequisite: repo ruleset on `refs/heads/develop` requiring `mq-dogfood-canary` (added by operator 2026-05-21 via the GitHub Rulesets UI; admin RepositoryRole given bypass via the same API).

### 4. DOG-04 — simultaneous /merge idempotency
expected: `python -m rocm_mq.dogfood.dog_04 --owner SamuelReeder --repo rocm-libraries` exits 0 within 60s; second /merge gets only the eyes-reaction (no duplicate status comment, no duplicate label flip).
result: passed (manual JSON, 2026-05-20T20:27Z). PR #33 squash-merged into fork/develop end-to-end. Single mq:queued label, single status comment, single eyes-reaction on first /merge (second /merge run cancelled by GHA concurrency before reaching the handler — non-bug; idempotency-with-eyes verified independently on PR #32 via a third /merge that hit the live handler).

### 5. DOG-05 — author push between activation and squash
expected: `python -m rocm_mq.dogfood.dog_05 --owner SamuelReeder --repo rocm-libraries` exits 0 within 15 min; eject status comment contains the verbatim RFC §6 phrase `activation invalid (branch updated or label tampered)`.
result: passed (2026-05-21T04:36Z). PR #39 activated on cycle 1; driver's Contents-API push wrote a second commit to the PR branch; cycle 2 saw the head-SHA mismatch and ejected via the decision-layer is_validly_active branch with the documented reason. JSON: `dogfood-runs/2026-05-21T04-34-17.664340+00-00-dog_05.json`.

### 6. DOG-06 — RFC §4.2 5-PR worked example (marquee)
expected: `python -m rocm_mq.dogfood.dog_06 --owner SamuelReeder --repo rocm-libraries` exits 0 within ~60 min; PR squash-merge order on fork develop matches A → B,C (parallel) → D → E.
result: passed (2026-05-21T04:42Z). PRs #40 (A) → #41 (B) + #42 (C) parallel → #43 (D) → #44 (E) all squash-merged with `tree_diff_status=ahead` on every squash. Both invariants held (ordering_invariant_violations: [], tree_diff_invariant_violations: []). Required fix (03-wr-11e): merged-state renderer now emits `tree_diff_status=ahead` so DOG-06's per-PR observation walker can extract it from the squash status comment. JSON: `dogfood-runs/2026-05-21T04-42-39.643364+00-00-dog_06.json`.

### 7. DOG-07 — approval revoked between enqueue and squash
expected: `APPROVER_TOKEN=<second-account-PAT> python -m rocm_mq.dogfood.dog_07 --owner SamuelReeder --repo rocm-libraries` exits 0 within 15 min; eject status comment contains the verbatim RFC §6 phrase `approval revoked`. **Requires a second collaborator account on the fork** (plan 03-15 PRE-CONFIRM deferred provisioning to runtime).
result: blocked — no 2nd account on fork. The no-approval gate is also currently disabled for dogfood (commented out per the user's 2026-05-20 instruction; PORT-02 marker in `cmd_handle.py`), so this scenario cannot exercise its full eject path even with an approver until the gate is re-enabled.

### 8. DOG-08 — /merge on PR touching no opted-in path
expected: `python -m rocm_mq.dogfood.dog_08 --owner SamuelReeder --repo rocm-libraries` exits 0 within 60s; handler posts explanatory rejection comment (no `mq:queued` label, no active status comment).
result: passed (2026-05-20T18:02Z, run 53s). PR #30 on fork; handler rejected with "no opted-in path" comment; zero `mq:*` labels applied; eyes-reaction from `merge-clanker[bot]` on the /merge comment (WF-12 fix verified live). JSON: `dogfood-runs/2026-05-20T18-01-20.568530+00-00-dog_08.json`. DOG-08 doesn't need an approver because the path-rejection branch fires BEFORE the at-enqueue gates.

### 9. Aggregator refresh of DOGFOOD-RESULTS.md
expected: `cd .github/merge-queue && python -m rocm_mq.dogfood.aggregator` reads the 7 dogfood-runs/*.json files from tests 2–8 above and replaces the 7 "not yet run" sections in `DOGFOOD-RESULTS.md` with the most-recent passing JSON per scenario.
result: passed (6/7 sections populated). Aggregator runs cleanly; DOGFOOD-RESULTS.md now shows DOG-02/03/04/05/06/08 with status=passed + live PR links + observed_outcome. Only DOG-07 stays "not yet run" until a second collaborator account is provisioned on the fork.

### 10. SC#5 — full RFC §4.2 worked example replay (proof-of-correctness statement)
expected: Identical to test 6 above (DOG-06 driver). Listed separately because Phase 3 ROADMAP SC#5 names it directly and Phase 5 porting-prep will cite it by SC number. Squash-merge timestamps + processor `$GITHUB_STEP_SUMMARY` entries are the canonical evidence.
result: passed (2026-05-21T04:42Z, same DOG-06 invocation). Driver-emitted per-PR sub-outcomes recorded against PRs #40–#44 on fork develop; `tree_diff_status=ahead` confirmed on every squash (SC#3 Phase B compare_commits invariant). Phase 5 porting-prep can cite `dogfood-runs/2026-05-21T04-42-39.643364+00-00-dog_06.json` as the canonical SC#5 evidence pack.

## Summary

total: 10
passed: 9
issues: 0
pending: 0
skipped: 0
blocked: 1

## Gaps

### DOG-07 needs a second collaborator account on the fork

Plan 03-15 deferred provisioning to runtime. The no-approval gate is
also commented out in `cmd_handle.py` (DOGFOOD-ONLY marker, PORT-02
revert) because the operator did not want to test what is essentially
GitHub's own branch-protection enforcement. Both need to land before
DOG-07 can fully exercise the "approval revoked between enqueue and
squash" eject path against a live PR.

### GHA cron is throttled on personal repos (operational note)

The 3-min schedule in `mq-processor.yml` actually fires every ~30–60
min on `SamuelReeder/rocm-libraries`, not every 3 min. Every Phase 3
dogfood drive against the live fork uses `gh workflow run
mq-processor.yml --ref develop` to dispatch cycles synchronously.
Upstream `ROCm/rocm-libraries` is an organization repo with much higher
runner capacity and cron is expected to fire on the documented cadence
there. Phase 5 porting-prep should record this expectation (and call
out that org-repo cron honors the schedule) as a pre-deploy note.

### RFC tweak: required_checks delegated to branch protection (PORT-03)

Per 03-wr-09, the queue no longer maintains its own `required_checks`
map in `path_to_queues.yml`. Branch protection is the single source of
truth. This is a deviation from RFC §4.8 wording. PORT-03 (Phase 5
porting-prep RFC-tweaks log) must record this deviation before any
upstream port. Concrete tweak text: §4.8 "PATH_TO_QUEUES MAY include
a `required_checks` map" → "Required-check evaluation is delegated to
the repository's branch protection settings; the queue does not
maintain a duplicate `required_checks` map in `path_to_queues.yml`."

### RFC tweak: canary workflow runs on every PR (PORT-04)

Per 03-wr-11b, `mq-dogfood-canary.yml` no longer scopes itself to
`dogfood/**` paths. With branch protection requiring the canary on
develop, a paths filter would block every non-dogfood PR because the
check would never report. Phase 5 porting-prep should record this
deviation (the upstream branch-protection setup either uses a different
required-check workflow or accepts the always-runs cost of the canary)
and decide whether the dogfood canary even ships upstream or stays as
fork-only test scaffolding.

### Stale dogfood PRs accumulating

Per CONTEXT.md D-04, drivers don't clean up — they leave PRs/branches
for audit. The current run added PRs #35–#44 (canary registration,
DOG-02/03/05/06 PRs); DOG-06's 5 PRs (#40–#44) were auto-merged by the
queue. Manual closure via `gh pr close <n> --delete-branch` when audit
period ends.
