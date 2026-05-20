---
status: partial
phase: 03-handler-processor-on-fork
source: [03-VERIFICATION.md]
started: 2026-05-20T00:00:00Z
updated: 2026-05-20T18:05:00Z
---

## Current Test

[5 of 10 items resolved; 5 deferred awaiting 2nd-account approver]

## Tests

### 1. Deploy Phase 3 workflows to `develop` on the fork
expected: After this branch's PR merges to `develop` on `SamuelReeder/rocm-libraries`, `mq-processor` cron begins firing every 3 min and posts `$GITHUB_STEP_SUMMARY` entries visible at https://github.com/SamuelReeder/rocm-libraries/actions/workflows/mq-processor.yml. `mq-handler` subscribes to `issue_comment` + `pull_request_target` + `pull_request_review` events with the per-job App-token mint succeeding (verifiable from the workflow run logs showing the `create-github-app-token@bcd2ba49...` step's post-step revoke).
result: passed (2026-05-20). Direct-pushed feature branch to fork/develop. mq-processor cron firing every 3 min (run 26177152313 + ongoing). mq-handler responds to issue_comment within ~40-60s. App-token mint working end-to-end after 03-wr-01 fix (env-var-trust path; live runs confirmed App slug is `merge-clanker`, not `rocm-mq-fork` as plan 03-05 originally recorded). All 7 queues from `path_to_queues.yml` now visible in processor cycle summary after 03-wr-02 (YAML loader integration). Run 26178129761 shows all 7 queues at depth 0.

### 2. DOG-02 — merge conflict at activation
expected: `python -m rocm_mq.dogfood.dog_02 --owner SamuelReeder --repo rocm-libraries` exits 0 within 12 min; writes `.planning/phases/03-handler-processor-on-fork/dogfood-runs/dog_02-<utc>.json` with `status: pass`; eject status comment contains the verbatim phrase `merge conflict with develop`.
result: blocked. Driver creates PR successfully but handler rejects with `no-approval` at-enqueue gate — the fork has no second collaborator account, and the PR author cannot self-approve per RFC §5 (mirrors upstream branch-protection semantics). Unblocks when a second collaborator account is added to the fork OR an opt-in flag relaxes the no-approval gate for dogfood mode.

### 3. DOG-03 — CI failure during evaluation
expected: `python -m rocm_mq.dogfood.dog_03 --owner SamuelReeder --repo rocm-libraries` exits 0 within 20 min; JSON output `status: pass`; eject status comment names the canary check.
result: blocked — no 2nd account on fork (same root cause as test #2)

### 4. DOG-04 — simultaneous /merge idempotency
expected: `python -m rocm_mq.dogfood.dog_04 --owner SamuelReeder --repo rocm-libraries` exits 0 within 60s; second /merge gets only the eyes-reaction (no duplicate status comment, no duplicate label flip).
result: blocked — no 2nd account on fork (same root cause as test #2)

### 5. DOG-05 — author push between activation and squash
expected: `python -m rocm_mq.dogfood.dog_05 --owner SamuelReeder --repo rocm-libraries` exits 0 within 15 min; eject status comment contains the verbatim RFC §6 phrase `activation invalid (branch updated or label tampered)`.
result: blocked — no 2nd account on fork (same root cause as test #2)

### 6. DOG-06 — RFC §4.2 5-PR worked example (marquee)
expected: `python -m rocm_mq.dogfood.dog_06 --owner SamuelReeder --repo rocm-libraries` exits 0 within ~60 min; PR squash-merge order on fork develop matches A → B,C (parallel) → D → E.
result: blocked — no 2nd account on fork (same root cause as test #2)

### 7. DOG-07 — approval revoked between enqueue and squash
expected: `APPROVER_TOKEN=<second-account-PAT> python -m rocm_mq.dogfood.dog_07 --owner SamuelReeder --repo rocm-libraries` exits 0 within 15 min; eject status comment contains the verbatim RFC §6 phrase `approval revoked`. **Requires a second collaborator account on the fork** (plan 03-15 PRE-CONFIRM deferred provisioning to runtime).
result: blocked — no 2nd account on fork (same root cause as test #2)

### 8. DOG-08 — /merge on PR touching no opted-in path
expected: `python -m rocm_mq.dogfood.dog_08 --owner SamuelReeder --repo rocm-libraries` exits 0 within 60s; handler posts explanatory rejection comment (no `mq:queued` label, no active status comment).
result: passed (2026-05-20T18:02:13Z, run 53s). PR #30 on fork; handler rejected with "no opted-in path" comment; zero `mq:*` labels applied; eyes-reaction from `merge-clanker[bot]` on the /merge comment (WF-12 fix verified live). JSON: `.planning/phases/03-handler-processor-on-fork/dogfood-runs/2026-05-20T18-01-20.568530+00-00-dog_08.json`. DOG-08 doesn't need an approver because the path-rejection branch fires BEFORE the at-enqueue gates.

### 9. Aggregator refresh of DOGFOOD-RESULTS.md
expected: `cd .github/merge-queue && python -m rocm_mq.dogfood.aggregator` reads the 7 dogfood-runs/*.json files from tests 2–8 above and replaces the 7 "not yet run" sections in `DOGFOOD-RESULTS.md` with the most-recent passing JSON per scenario.
result: passed (1/7 sections populated). Aggregator runs cleanly; DOGFOOD-RESULTS.md DOG-08 section now shows status=passed with the live PR link + observed_outcome. Other 6 sections remain "not yet run" until DOG-02/03/04/05/06/07 unblock.

### 10. SC#5 — full RFC §4.2 worked example replay (proof-of-correctness statement)
expected: Identical to test 6 above (DOG-06 driver). Listed separately because Phase 3 ROADMAP SC#5 names it directly and Phase 5 porting-prep will cite it by SC number. Squash-merge timestamps + processor `$GITHUB_STEP_SUMMARY` entries are the canonical evidence.
result: blocked — no 2nd account on fork (same root cause as test #2)

## Summary

total: 10
passed: 3
issues: 0
pending: 0
skipped: 0
blocked: 7

## Gaps

### Approver-required gate blocks all PRs the queue would actually merge

Six dogfood drivers (DOG-02, DOG-03, DOG-04, DOG-05, DOG-06, DOG-07) hit
the `no-approval` at-enqueue gate because:
- The fork has only one collaborator (SamuelReeder).
- RFC §5 prohibits self-approval; the PR author's own review doesn't count.
- DOG-07 was already known to need `APPROVER_TOKEN`; the gate now blocks
  the other five too.

**Resolution paths** (any one unblocks the remaining drivers):

- **(a) Add a second collaborator account on the fork.** Invite a second
  GitHub identity to `SamuelReeder/rocm-libraries`, mint a PAT for it,
  set `APPROVER_TOKEN=<2nd-PAT>` for DOG-07. The other drivers would
  need driver-level edits to approve via the second account too.
- **(b) Add a `MQ_DOGFOOD_RELAX_APPROVAL=1` env var** that makes the
  no-approval gate advisory in cmd_handle. Off by default; only on for
  fork-dogfood runs. Smallest code change, preserves production
  RFC §4.3 compliance.
- **(c) Configure the fork's own branch protection on develop** with
  required-reviews=0 for dogfood. Different gate-bypass shape.

### Branch protection on `fork/develop`

RFC §4.9 + CLAUDE.md require branch protection on develop for §5
safety properties (post-enqueue approval revocation, required-check
enforcement). `fork/develop` is currently unprotected (`HTTP 404` from
the protection endpoint). The queue can still operate functionally
(eject logic doesn't depend on protection), but Phase 5 porting prep
should record this as a pre-deploy step for upstream parity.

### Stale dogfood PRs accumulating

Per CONTEXT.md D-04, drivers don't clean up — they leave PRs/branches
for audit. Currently 5 open PRs on the fork (#26, #27, #28, #29 from
DOG-04 attempts; #30 from DOG-08). Manual closure via
`gh pr close <n> --delete-branch` when audit period ends.
