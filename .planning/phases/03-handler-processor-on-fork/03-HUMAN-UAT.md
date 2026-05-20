---
status: partial
phase: 03-handler-processor-on-fork
source: [03-VERIFICATION.md]
started: 2026-05-20T00:00:00Z
updated: 2026-05-20T00:00:00Z
---

## Current Test

[awaiting operator deployment + live-fork dogfood runs]

## Tests

### 1. Deploy Phase 3 workflows to `develop` on the fork
expected: After this branch's PR merges to `develop` on `SamuelReeder/rocm-libraries`, `mq-processor` cron begins firing every 3 min and posts `$GITHUB_STEP_SUMMARY` entries visible at https://github.com/SamuelReeder/rocm-libraries/actions/workflows/mq-processor.yml. `mq-handler` subscribes to `issue_comment` + `pull_request_target` + `pull_request_review` events with the per-job App-token mint succeeding (verifiable from the workflow run logs showing the `create-github-app-token@bcd2ba49...` step's post-step revoke).
result: [pending]

### 2. DOG-02 — merge conflict at activation
expected: `python -m rocm_mq.dogfood.dog_02 --owner SamuelReeder --repo rocm-libraries` exits 0 within 12 min; writes `.planning/phases/03-handler-processor-on-fork/dogfood-runs/dog_02-<utc>.json` with `status: pass`; eject status comment contains the verbatim phrase `merge conflict with develop`.
result: [pending]

### 3. DOG-03 — CI failure during evaluation
expected: `python -m rocm_mq.dogfood.dog_03 --owner SamuelReeder --repo rocm-libraries` exits 0 within 20 min; JSON output `status: pass`; eject status comment names the canary check.
result: [pending]

### 4. DOG-04 — simultaneous /merge idempotency
expected: `python -m rocm_mq.dogfood.dog_04 --owner SamuelReeder --repo rocm-libraries` exits 0 within 60s; second /merge gets only the eyes-reaction (no duplicate status comment, no duplicate label flip).
result: [pending]

### 5. DOG-05 — author push between activation and squash
expected: `python -m rocm_mq.dogfood.dog_05 --owner SamuelReeder --repo rocm-libraries` exits 0 within 15 min; eject status comment contains the verbatim RFC §6 phrase `activation invalid (branch updated or label tampered)`.
result: [pending]

### 6. DOG-06 — RFC §4.2 5-PR worked example (marquee)
expected: `python -m rocm_mq.dogfood.dog_06 --owner SamuelReeder --repo rocm-libraries` exits 0 within ~60 min; PR squash-merge order on fork develop matches A → B,C (parallel) → D → E.
result: [pending]

### 7. DOG-07 — approval revoked between enqueue and squash
expected: `APPROVER_TOKEN=<second-account-PAT> python -m rocm_mq.dogfood.dog_07 --owner SamuelReeder --repo rocm-libraries` exits 0 within 15 min; eject status comment contains the verbatim RFC §6 phrase `approval revoked`. **Requires a second collaborator account on the fork** (plan 03-15 PRE-CONFIRM deferred provisioning to runtime).
result: [pending]

### 8. DOG-08 — /merge on PR touching no opted-in path
expected: `python -m rocm_mq.dogfood.dog_08 --owner SamuelReeder --repo rocm-libraries` exits 0 within 60s; handler posts explanatory rejection comment (no `mq:queued` label, no active status comment).
result: [pending]

### 9. Aggregator refresh of DOGFOOD-RESULTS.md
expected: `cd .github/merge-queue && python -m rocm_mq.dogfood.aggregator` reads the 7 dogfood-runs/*.json files from tests 2–8 above and replaces the 7 "not yet run" sections in `DOGFOOD-RESULTS.md` with the most-recent passing JSON per scenario.
result: [pending]

### 10. SC#5 — full RFC §4.2 worked example replay (proof-of-correctness statement)
expected: Identical to test 6 above (DOG-06 driver). Listed separately because Phase 3 ROADMAP SC#5 names it directly and Phase 5 porting-prep will cite it by SC number. Squash-merge timestamps + processor `$GITHUB_STEP_SUMMARY` entries are the canonical evidence.
result: [pending]

## Summary

total: 10
passed: 0
issues: 0
pending: 10
skipped: 0
blocked: 0

## Gaps
