---
phase: 03-handler-processor-on-fork
plan: 11
subsystem: testing
tags: [dogfood, drivers, dog-02, dog-04, dog-08, fork-validation, two-mode]

# Dependency graph
requires:
  - phase: 03-handler-processor-on-fork (plan 10)
    provides: _base.py scaffolding (DogfoodResult, create_dogfood_pr, post_command, poll_pr_state, emit_result, download_cycle_summary_artifact)
  - phase: 03-handler-processor-on-fork (plan 06)
    provides: cmd_handle.py rejection-text for DOG-08 ('no opted-in queue paths') + idempotency short-circuit for DOG-04
  - phase: 03-handler-processor-on-fork (plan 03)
    provides: path_to_queues.yml (driver path-selection for DOG-08 must miss every opted-in entry)
provides:
  - dog_02 driver — merge-conflict-at-activation eject scenario
  - dog_04 driver — simultaneous /merge idempotency scenario
  - dog_08 driver — handler-level rejection on no opted-in path scenario
  - dogfood-runs/.gitkeep — directory placeholder for per-run JSON output
affects:
  - 03-12 (dog_03 driver — extends the two-mode driver pattern)
  - 03-14 (dog_05 / dog_07 drivers — same shape)
  - 03-15 (dog_06 5-PR worked example driver — same shape, longer timeline)
  - 03-16 (aggregator — reads emit_result JSONs from dogfood-runs/)
  - Phase 3 live-fork verification step (Task 5 deferred — operator runs after workflows are deployed)
  - Phase 5 porting-prep replay kit (per-run JSON schema stability)

# Tech tracking
tech-stack:
  added: []  # all behavior built on stdlib + plan-10 scaffolding
  patterns:
    - "Two-mode driver: run_scenario(client, ...) test seam + main() CLI entrypoint sharing the same orchestration body"
    - "Inject-on-poll seam (inject_eject_after / inject_after) for unit testing async eject/rejection comments without real GitHub"
    - "Local FakeGitHub extension per driver (subclass + monkey-attached sub-namespaces) — _DogfoodFake is per-test-file, NOT promoted to tests/gh_fake.py until a second consumer needs it (mirrors plan 10 pattern)"
    - "Reactions-log read via state.reactions_log in tests, with per-comment reactions.list_for_issue_comment fallback for live mode"
    - "Conflict-seed-on-develop pattern (DOG-02): one Contents API write to branch='develop' BEFORE create_dogfood_pr, with PR branch then modifying the same line — produces deterministic activation-time conflict"

key-files:
  created:
    - .github/merge-queue/src/rocm_mq/dogfood/dog_02.py
    - .github/merge-queue/src/rocm_mq/dogfood/dog_04.py
    - .github/merge-queue/src/rocm_mq/dogfood/dog_08.py
    - .github/merge-queue/tests/test_dogfood_dog_02.py
    - .github/merge-queue/tests/test_dogfood_dog_04.py
    - .github/merge-queue/tests/test_dogfood_dog_08.py
    - .planning/phases/03-handler-processor-on-fork/dogfood-runs/.gitkeep
  modified: []

key-decisions:
  - "PRE-CONFIRM Task 1 auto-selected option-a (defaults from RESEARCH.md Area #10 + no teardown). Rationale: documented in research, drivers aren't being live-run in this plan, and the user surfaced no tightening preference. TIMEOUT_S=720 for DOG-02 (12 min, ≥3 cron cycles + CI start); TIMEOUT_S=60 for DOG-04 and DOG-08 (handler-fast paths)."
  - "Two-mode pattern: every driver exposes run_scenario(client, ...) as the orchestration body and main() as the CLI wrapper. Unit tests call run_scenario directly with a FakeGitHub extension; live-fork mode calls main() which constructs a real GitHubClient from GITHUB_TOKEN."
  - "Inject-on-poll test seam (inject_eject_after / inject_after callbacks) lets unit tests synthesize the async eject / rejection comments WITHOUT needing a separate background thread or simulated webhook. The predicate calls the callback BEFORE scanning, so the simulated state lands deterministically on every poll."
  - "DOG-04 idempotency: drivers do NOT poll for absence-of-state (no good affirmative signal exists per RESEARCH.md Area #7). Instead a single bounded sleep lets the handler settle, then assertions are collected. Unit tests bypass the sleep via poll_interval_s=0."
  - "DOG-08 PR target path is DOCS/dogfood-no-opted-in.md (upper-case DOCS/, not docs/, to avoid future docs/ opt-in collisions). A unit test asserts the path does NOT match any path_to_queues.yml entry — guards against future YAML evolution silently breaking the driver."
  - "DOG-02 seeds develop with a conflict line BEFORE creating the PR branch. Both the seed commit (variant-A on develop) and the PR's file commit (variant-B on the dogfood/dog_02-* branch) touch the SAME line, so activation-time merge-into-PR-head conflicts deterministically. Threat T-03-11-01 (Tampering on develop) is accepted per the threat register; seed file lives at projects/hipdnn/dogfood-seed-conflict.txt."
  - "Task 5 (live-fork verification) deferred per the user's plan execution objective: 'Do NOT actually run the live driver against the fork as part of this plan execution.' Operator runs the three --live drivers after mq-handler.yml + mq-processor.yml are deployed to develop on the fork and the App installation is live."

patterns-established:
  - "Per-driver test file shape: module-constants tests → _DogfoodFake fake extension → run_scenario happy path test → run_scenario failure-mode test(s). Each driver has at least one failure mode unit test that demonstrates the assertion is real, not a no-op."
  - "Driver module ordering: docstring → constants → predicate factory (where applicable) → run_scenario → _parse_args → main → __all__ → __main__ guard"
  - "Lazy githubkit import: from rocm_mq.gh import GitHubClient lives INSIDE main(), so unit-test mode never pays the githubkit import cost"

requirements-completed: [DOG-02, DOG-04, DOG-08]

# Metrics
duration: ~45min
completed: 2026-05-20
---

# Phase 3 Plan 11: DOG-02 / DOG-04 / DOG-08 Driver Trio Summary

**Three short-scenario dogfood drivers built on plan 03-10's _base.py scaffolding (merge-conflict-at-activation eject, simultaneous /merge idempotency, handler-level rejection on no opted-in path) with a unit-test-mode FakeGitHub seam alongside live-fork mode CLI; per-run JSON output directory placeholder added.**

## Performance

- **Duration:** ~45 min
- **Started:** 2026-05-20
- **Completed:** 2026-05-20
- **Tasks:** 4 implemented + 1 deferred (Task 5 live-fork verification, intentionally deferred per user objective)
- **Files created:** 7
- **Files modified:** 0
- **Tests added:** 22 unit tests (4 + 6 + 9 + smoke from base = 22 new for this plan)
- **Total dogfood-suite tests:** 43 passing (21 from plan 10 + 22 from plan 11)
- **Full repo test suite:** 459 passing (no regressions)

## Accomplishments

- `rocm_mq.dogfood.dog_02` ships the merge-conflict-at-activation driver:
  - Seeds `projects/hipdnn/dogfood-seed-conflict.txt` on develop with `variant-A` BEFORE creating the PR branch
  - Creates a PR via `create_dogfood_pr` with `variant-B` on the same line, producing a deterministic merge conflict only at activation time
  - Posts `/merge`, polls (15s interval, 12-min budget) for a status comment carrying the literal `merge conflict with develop`
  - `inject_eject_after` callback seam lets unit tests synthesize the eject comment without a real processor
  - Emits the D-04 JSON to `.planning/.../dogfood-runs/`
- `rocm_mq.dogfood.dog_04` ships the simultaneous-/merge idempotency driver:
  - Creates a PR at `dnn-providers/miopen-provider/dogfood-idempotency.txt` (opted-in path → non-empty queue set)
  - Posts two `/merge` comments back-to-back, lets the handler settle (single bounded sleep in live mode; 0s in tests)
  - Asserts three RESEARCH.md Area #7 idempotency layers: `mq:queued` label count == 1, `<!-- rocm-mq-status -->` comment count == 1, eyes reactions on triggers == 2
  - Reads `FakeGitHub.state.reactions_log` in test mode; falls back to per-comment `reactions.list_for_issue_comment` in live mode
- `rocm_mq.dogfood.dog_08` ships the handler-rejection driver:
  - Creates a PR at `DOCS/dogfood-no-opted-in.md` (verified absent from path_to_queues.yml by a unit test)
  - Posts `/merge`, polls for any non-status-marker comment containing `no opted-in`
  - Asserts the three rejection invariants: zero `mq:*` labels, zero status-comment-marker comments, rejection comment found
  - Three unit-test failure modes: missing rejection (TimeoutError), buggy label apply (`passed=False`), reason text mismatch
- `dogfood-runs/.gitkeep` placeholder force-added to the gitignored planning tree so the directory exists before any per-run JSON lands

## Decisions Made

| Decision | Rationale |
|---|---|
| Auto-select option-a (RESEARCH.md defaults) for Task 1 checkpoint | Drivers aren't live-run in this plan; defaults are documented; no user-surfaced preference to tighten |
| Two-mode driver per scenario (`run_scenario` test seam + `main()` CLI) | Lets the same orchestration body be exercised by CI without real GitHub AND by operator without a separate test scaffold |
| Inject-on-poll callback in predicates (DOG-02, DOG-08) | Eliminates the need for background threads / fake webhooks in tests; synchronous + deterministic |
| DOG-04 uses a single bounded sleep, not a poll | RESEARCH.md Area #7: no affirmative "second /merge produced no work" signal exists; assertions are the signal |
| DOG-08 path = `DOCS/dogfood-no-opted-in.md` (upper-case) | Avoids future `docs/` opt-in collisions; unit test enforces the YAML mismatch property |
| Conflict-seed-on-develop for DOG-02 (write before PR branch) | Only way to produce deterministic activation-time conflict without a separate background mutation; accepted per threat T-03-11-01 |
| Lazy `from rocm_mq.gh import GitHubClient` inside `main()` | Unit-test mode never pays the githubkit import cost |
| Task 5 (live-fork verification) deferred | Per user objective: "Do NOT actually run the live driver against the fork as part of this plan execution" |

## Threat Flags

None introduced beyond the existing threat register (T-03-11-01..04 from the plan are unchanged):
- The DOG-02 conflict-seed commit on develop (T-03-11-01) remains an **accepted** disposition; the file path and content live in the driver source for audit.
- The driver code is purely additive to `rocm_mq/dogfood/` (I/O layer, NOT pure-layer per CONTEXT.md D-02); does not extend the App-token trust boundary.

## Deviations from Plan

**None.** All four task behavior blocks were implemented as specified. The TDD gates (RED → GREEN per task) were honored via the test-first commit ordering visible in the git log:

| Task | RED commit | GREEN commit |
|---|---|---|
| Task 2 (dog_02) | `861b7eacc0e` test(03-11): add failing tests for dog_02 | `78ef7856ba1` feat(03-11): implement dog_02 |
| Task 3 (dog_04) | `9eb692a151f` test(03-11): add failing tests for dog_04 | `2fd9bd2b757` feat(03-11): implement dog_04 |
| Task 4 (dog_08) | `ffe3a92b942` test(03-11): add failing tests for dog_08 | `cd3b7b31a09` feat(03-11): implement dog_08 + .gitkeep |

Task 1 (`checkpoint:decision`) auto-selected option-a (defaults) per the auto-mode pattern + the user's silence on timeout-tightening; rationale logged inline.

Task 5 (`checkpoint:human-verify`, live-fork run) intentionally deferred per the user's plan execution objective. Listed under "Deferred Issues" below for operator action.

## Deferred Issues

- **Task 5 (live-fork verification):** Operator runs `python -m rocm_mq.dogfood.dog_08`, `dog_04`, `dog_02` (in that order, shortest-budget-first) against `SamuelReeder/rocm-libraries` AFTER:
  1. `mq-handler.yml` is deployed to `develop` on the fork (plan 03-07)
  2. `mq-processor.yml` is deployed to `develop` on the fork (plan 03-08)
  3. The merge-queue App installation is live with the develop-scoped secrets (plan 03-05)
  4. `GITHUB_TOKEN` is exported with App-equivalent scopes (or an installation token minted via `gh api`)
  Each driver creates a PR on the fork, leaves it in place (no teardown per D-04), and emits a per-run JSON to `.planning/phases/03-handler-processor-on-fork/dogfood-runs/`. Live runs are scoped to Phase 3 verification or the optional Phase 3 final-validation pass — they are out of scope for CI.

- **Cycle-summary artifact capture:** None of the three drivers populates `processor_run_urls` or `step_summary_excerpt` from live runs. The processor's status-comment embeds the cycle URL; future enhancement can extract it from the matched comment body during the predicate callback. DOG-02's `notes` field documents this. Not blocking — the JSON contract is forward-compatible (additions are non-breaking).

## Live-Fork Run Recipe (for the deferred Task 5)

```bash
# 1. Ensure the fork has mq-handler.yml + mq-processor.yml on develop and the
#    App installation is live.
# 2. Export an App-equivalent token (e.g., minted via gh api or actions/create-github-app-token):
export GITHUB_TOKEN="ghs_..."

# 3. Run shortest-budget driver first (60s timeout):
cd .github/merge-queue
python -m rocm_mq.dogfood.dog_08 --owner SamuelReeder --repo rocm-libraries

# 4. Run idempotency driver (60s timeout):
python -m rocm_mq.dogfood.dog_04 --owner SamuelReeder --repo rocm-libraries

# 5. Run the longest driver last (12-min worst-case timeout):
python -m rocm_mq.dogfood.dog_02 --owner SamuelReeder --repo rocm-libraries

# 6. Inspect the per-run JSONs:
ls .planning/phases/03-handler-processor-on-fork/dogfood-runs/
#   2026-MM-DDTHH-MM-SS+00-00-dog_08.json  (passed=true)
#   2026-MM-DDTHH-MM-SS+00-00-dog_04.json  (passed=true)
#   2026-MM-DDTHH-MM-SS+00-00-dog_02.json  (passed=true)
```

Each driver exits 0 on pass / 1 on fail. PRs and branches remain in the fork for audit per D-04.

## Self-Check: PASSED

**Files created (all 7 verified present):**
- `.github/merge-queue/src/rocm_mq/dogfood/dog_02.py` ✓
- `.github/merge-queue/src/rocm_mq/dogfood/dog_04.py` ✓
- `.github/merge-queue/src/rocm_mq/dogfood/dog_08.py` ✓
- `.github/merge-queue/tests/test_dogfood_dog_02.py` ✓
- `.github/merge-queue/tests/test_dogfood_dog_04.py` ✓
- `.github/merge-queue/tests/test_dogfood_dog_08.py` ✓
- `.planning/phases/03-handler-processor-on-fork/dogfood-runs/.gitkeep` ✓ (force-added; planning tree is gitignored)

**Commits (all six verified in git log):**
- `861b7eacc0e` test(03-11): add failing tests for dog_02 merge-conflict driver ✓
- `78ef7856ba1` feat(03-11): implement dog_02 merge-conflict-at-activation driver ✓
- `9eb692a151f` test(03-11): add failing tests for dog_04 idempotency driver ✓
- `2fd9bd2b757` feat(03-11): implement dog_04 simultaneous-/merge idempotency driver ✓
- `ffe3a92b942` test(03-11): add failing tests for dog_08 handler-rejection driver ✓
- `cd3b7b31a09` feat(03-11): implement dog_08 handler-rejection driver + dogfood-runs/.gitkeep ✓

**Test status:**
- All 22 new dogfood-driver tests pass
- Full repo: 459 tests pass (no regressions)
- ruff lint: clean across all new + modified files

**TDD gate compliance:** Each `tdd="true"` task has a `test(...)` commit preceding its `feat(...)` commit.
