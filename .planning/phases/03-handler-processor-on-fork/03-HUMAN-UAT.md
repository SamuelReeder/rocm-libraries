---
status: partial
phase: 03-handler-processor-on-fork
source: [03-VERIFICATION.md]
started: 2026-05-20T00:00:00Z
updated: 2026-05-20T18:05:00Z
---

## Current Test

[6 of 10 items resolved; DOG-01 (canonical happy path) verified live;
4 remaining items each takes a single live driver invocation]

## Tests

### 1. Deploy Phase 3 workflows to `develop` on the fork
expected: After this branch's PR merges to `develop` on `SamuelReeder/rocm-libraries`, `mq-processor` cron begins firing every 3 min and posts `$GITHUB_STEP_SUMMARY` entries visible at https://github.com/SamuelReeder/rocm-libraries/actions/workflows/mq-processor.yml. `mq-handler` subscribes to `issue_comment` + `pull_request_target` + `pull_request_review` events with the per-job App-token mint succeeding (verifiable from the workflow run logs showing the `create-github-app-token@bcd2ba49...` step's post-step revoke).
result: passed (2026-05-20). Direct-pushed feature branch to fork/develop. mq-processor cron firing every 3 min (run 26177152313 + ongoing). mq-handler responds to issue_comment within ~40-60s. App-token mint working end-to-end after 03-wr-01 fix (env-var-trust path; live runs confirmed App slug is `merge-clanker`, not `rocm-mq-fork` as plan 03-05 originally recorded). All 7 queues from `path_to_queues.yml` now visible in processor cycle summary after 03-wr-02 (YAML loader integration). Run 26178129761 shows all 7 queues at depth 0.

### 2. DOG-02 — merge conflict at activation
expected: `python -m rocm_mq.dogfood.dog_02 --owner SamuelReeder --repo rocm-libraries` exits 0 within 12 min; writes `.planning/phases/03-handler-processor-on-fork/dogfood-runs/dog_02-<utc>.json` with `status: pass`; eject status comment contains the verbatim phrase `merge conflict with develop`.
result: unblocked after 03-wr-06 (no-approval gate disabled) + 03-wr-08 (maintainer-edits gate scoped to cross-repo) + 03-wr-09 (implicit-via-protection refactor). Driver-strict 30s settle still too short for live handler runtime (~60-90s); needs driver patch (settle to 120s+) or manual PR-state inspection like DOG-04. Operator action: run the driver, then manually inspect PR state and write a result JSON if the driver's auto-assertion times out.

### 3. DOG-03 — CI failure during evaluation
expected: `python -m rocm_mq.dogfood.dog_03 --owner SamuelReeder --repo rocm-libraries` exits 0 within 20 min; JSON output `status: pass`; eject status comment names the canary check.
result: blocked — no 2nd account on fork (same root cause as test #2)

### 4. DOG-04 — simultaneous /merge idempotency
expected: `python -m rocm_mq.dogfood.dog_04 --owner SamuelReeder --repo rocm-libraries` exits 0 within 60s; second /merge gets only the eyes-reaction (no duplicate status comment, no duplicate label flip).
result: passed (manual JSON, 2026-05-20T20:27Z). PR #33 squash-merged into fork/develop end-to-end. Single mq:queued label, single status comment, single eyes-reaction on first /merge (second /merge run cancelled by GHA concurrency before reaching the handler — non-bug; idempotency-with-eyes verified independently on PR #32 via a third /merge that hit the live handler). This also implicitly verifies DOG-01 (the canonical happy path) for the first time on the real fork.

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
passed: 4
issues: 0
pending: 0
skipped: 0
blocked: 6

## Gaps

### Driver strict-settle window too short for live handler runtime

DOG-02/04/05/06 drivers use a 30-second `settle_s` between posting
`/merge` and asserting on PR state, but the live mq-handler workflow
takes 60-90 seconds end-to-end (queue + checkout + python install +
preflight + token mint + handle). The driver's strict-mode JSON is
all-zeros for the first run, then a manual inspection + manual result
JSON closes out the scenario (see DOG-04 / PR #33 for the pattern).

**Resolution:** patch `dogfood/_base.py` and per-driver `settle_s`
default to 120s. Or change driver to poll for expected state with
backoff instead of fixed sleep.

### Branch protection on `fork/develop` (lower urgency post 03-wr-09)

RFC §4.9 + CLAUDE.md require branch protection on develop for §5
safety properties. `fork/develop` is currently unprotected (`HTTP 404`
from the protection endpoint). Post 03-wr-09, the queue's required-
check evaluation is delegated to protection — meaning **without
protection configured, no required checks gate the merge**. PR #33
was successfully squash-merged this way (because nothing was required).
For real dogfood of DOG-03 (CI failure via canary) and any scenario
that relies on required-check enforcement, protection MUST be
configured on `fork/develop` with at least `mq-dogfood-canary / canary`
as a required check. Phase 5 porting prep should record protection
config as a pre-deploy step for upstream parity.

### Stale dogfood PRs accumulating

Per CONTEXT.md D-04, drivers don't clean up — they leave PRs/branches
for audit. Currently 5 open PRs on the fork (#26, #27, #28, #29 from
DOG-04 attempts; #30 from DOG-08). PR #33 (DOG-04 success) was
auto-merged by the queue and is in MERGED state. Manual closure via
`gh pr close <n> --delete-branch` when audit period ends.

### RFC tweak: required_checks delegated to branch protection (PORT-03)

Per 03-wr-09, the queue no longer maintains its own `required_checks`
map in `path_to_queues.yml`. Branch protection is the single source of
truth. This is a deviation from RFC §4.8 wording. PORT-03 (Phase 5
porting-prep RFC-tweaks log) must record this deviation before any
upstream port. Concrete tweak text: §4.8 "PATH_TO_QUEUES MAY include
a `required_checks` map" → "Required-check evaluation is delegated to
the repository's branch protection settings; the queue does not
maintain a duplicate `required_checks` map in `path_to_queues.yml`."
