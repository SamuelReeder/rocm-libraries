---
phase: 03-handler-processor-on-fork
plan: 08
subsystem: infra
tags: [github-actions, cron, workflow, processor, app-token, upload-artifact, concurrency, stateless-processor]

requires:
  - phase: 02-i-o-layer-executor
    provides: rocm_mq.cmd_process.process_cycle, --dry-run flag, $GITHUB_STEP_SUMMARY append
  - phase: 03-handler-processor-on-fork
    provides: |
      03-01 argparse subparser dispatch (process-cycle subcommand);
      03-04 preflight CLI (default-branch + path_to_queues Check 1/3);
      03-05 App + mq-secrets Environment live on the fork;
      03-07 mq-handler.yml SHA-pin patterns + sparse-checkout-from-develop discipline (Pitfall 1 mitigation reused verbatim)
provides:
  - "mq-processor.yml: 3-min cron + workflow_dispatch fallback driving rocm_mq.process-cycle on the fork's develop branch"
  - "Static-group concurrency block ('mq-processor' + cancel-in-progress:false) preserving the RFC §4.7 stateless-processor invariant"
  - "Pre-flight-FIRST step ordering (runs with read-only GITHUB_TOKEN BEFORE App-token mint) carrying plan 03-04 threat T-03-04-01 mitigation through"
  - "App-token mint with the full processor scope (contents/pull-requests/issues/statuses :write) per RFC §4.9"
  - "cycle-summary.md dual-sink in cmd_process.process_cycle (file + $GITHUB_STEP_SUMMARY) — RESEARCH.md Area #11"
  - "actions/upload-artifact step publishing cycle-summary-<RUN_ID> for Wave-4 dogfood drivers to download via the artifacts API"
affects: [03-09, 03-10, 03-11, 03-12, 03-13, 03-14, 03-15, 03-16, phase-04-tamper-audit]

tech-stack:
  added:
    - "actions/upload-artifact@v4 (SHA ea165f8d65b6e75b540449e92b4886f43607fa02)"
  patterns:
    - "STATIC concurrency group literal (never ${{ github.ref }}) for the singleton processor — Pitfall 9 mitigation"
    - "Dual-sink summary write ($GITHUB_STEP_SUMMARY append + MQ_CYCLE_SUMMARY_PATH overwrite) for both per-run UI and artifact consumers"
    - "MQ_CYCLE_SUMMARY_PATH env var as the workflow→Python contract for the upload-artifact path"

key-files:
  created:
    - ".github/workflows/mq-processor.yml — the cron-driven processor workflow"
    - ".planning/phases/03-handler-processor-on-fork/03-08-SUMMARY.md (this file)"
  modified:
    - ".github/merge-queue/src/rocm_mq/cmd_process.py — cycle-summary.md file sink added to process_cycle (RESEARCH.md Area #11)"
    - ".github/merge-queue/tests/test_cmd_process.py — three new tests for the dual-sink behavior"
    - ".github/merge-queue/.gitignore — ignore the generated cycle-summary.md artifact"

key-decisions:
  - "Concurrency group is the STATIC literal `mq-processor` (NEVER ${{ github.ref }}) — required for the stateless-processor invariant; the 2025 `queue:` keyword was rejected because CLAUDE.md identifies it as defeating the 'evicted = skipped tick = next cycle catches up' property"
  - "cancel-in-progress: false (NEVER true) — a cron-fire interrupting a mid-squash cycle would leave half-applied state, breaking RFC §4.6 crash-safety"
  - "Pre-flight runs BEFORE the App-token mint (carrying plan 03-04 threat T-03-04-01 mitigation through) — a misconfigured fork must never even hold the App installation credential"
  - "MQ_CYCLE_SUMMARY_PATH file sink is OVERWRITTEN per cycle (not appended) — each cycle's summary stands alone; the $GITHUB_STEP_SUMMARY append continues to follow GHA convention because multiple steps may add to it"
  - "App-identity Check 2 stays PASSIVE in process_cycle (slug-pin mismatch surfaces as a clear-cause cycle error) — explicit deferral from pre-flight per plan 03-04 Task 1 and plan 03-08 must_haves, mirroring the 03-06 WF-12 passive-coverage pattern"
  - "actions/upload-artifact@v4 resolved 2026-05-20 via `gh api repos/actions/upload-artifact/git/refs/tags/v4 --jq .object.sha` → ea165f8d65b6e75b540449e92b4886f43607fa02 (commit type, not annotated tag — direct SHA pin)"
  - "if: always() on the upload-artifact step so a failed cycle still publishes whatever the file sink wrote before exit (e.g., when dispatch raises CorruptSquashError mid-cycle)"
  - "if-no-files-found: warn (not error) on upload-artifact so a defensive cycle that exits before the summary render does not turn the workflow run red on top of the underlying failure"
  - "Per-job timeout-minutes: 5 (< 3-min cron interval — Pitfall 6) — typical cycles run 10–30s; a 5-min ceiling tolerates one overrun before back-to-back cron fires would queue"

patterns-established:
  - "Pattern: workflow-Python contract via env var. The workflow YAML sets MQ_CYCLE_SUMMARY_PATH to ${{ github.workspace }}/.github/merge-queue/cycle-summary.md and process_cycle honors it; default falls back to cycle-summary.md in cwd so local pytest runs work identically. Future workflow→Python configuration follows this shape (env var, not CLI flag, to keep the Python module's CLI surface minimal)."
  - "Pattern: SHA-pin discovery comment. The upload-artifact step embeds the exact `gh api` command used to resolve the SHA so future bumps are reproducible."
  - "Pattern: dual-sink rendered output. When a value needs to appear in the GHA UI AND be machine-consumed downstream, write to $GITHUB_STEP_SUMMARY (for the UI) AND to a file (for the artifact) — same string, different sinks."

requirements-completed: [WF-04, WF-05, WF-06, WF-07, WF-08, WF-09, WF-10]

duration: 7min
completed: 2026-05-20
---

# Phase 03 Plan 08: mq-processor.yml + cycle-summary.md dual-sink

**Stateless 3-min cron processor workflow with static concurrency, pre-flight-first step ordering, and a cycle-summary.md artifact channel published via actions/upload-artifact for downstream dogfood drivers.**

## Performance

- **Duration:** ~7 minutes
- **Started:** 2026-05-20T03:56:26Z
- **Completed:** 2026-05-20T04:03:24Z
- **Tasks:** 2 of 3 (Task 3 is operator-only — manual fork verification, see below)
- **Files modified:** 4 (1 new workflow, 1 modified Python module, 1 modified test file, 1 modified .gitignore)

## Accomplishments

- **mq-processor.yml created** — the cron-driven processor cycle workflow with the static `mq-processor` concurrency group, `cancel-in-progress: false`, pre-flight-first step ordering, and a 5-minute per-job timeout sized below the 3-min cron interval.
- **cycle-summary.md dual-sink** added to `process_cycle` — the rendered summary now lands in both `$GITHUB_STEP_SUMMARY` (existing append) and `MQ_CYCLE_SUMMARY_PATH` (new overwrite, default `cycle-summary.md`), unblocking the Wave-4 dogfood drivers that need a structured per-cycle summary they can fetch via the artifacts API.
- **actions/upload-artifact@v4 pinned and integrated** — SHA discovered live (`gh api`) and embedded in the workflow with the discovery command preserved as a comment so future bumps are reproducible.
- **App-token discipline** preserved end-to-end: pre-flight runs with read-only `GITHUB_TOKEN`, mint runs only after pre-flight passes, full processor scope on the minted token (contents/pull-requests/issues/statuses all `:write`), and `GITHUB_TOKEN` is overridden to the App-minted value in the cycle step so every cycle write is attributed to the App identity.

## Task Commits

Task 1 followed the RED → GREEN TDD discipline; Task 2 is a single workflow-author commit (no behaviour-adding Python).

1. **Task 1 RED: failing tests for cycle-summary.md sink** — `e0ebe9f42b3` (`test(03-08-01)`)
2. **Task 1 GREEN: write cycle summary to file sink** — `229451dd371` (`feat(03-08-01)`)
3. **Task 2: add mq-processor.yml** — `93a4743d21b` (`feat(03-08-02)`)

## Files Created/Modified

- `.github/workflows/mq-processor.yml` (NEW, 215 lines) — cron + workflow_dispatch, static concurrency, pre-flight-first, App-token mint, process-cycle invocation, upload-artifact publish
- `.github/merge-queue/src/rocm_mq/cmd_process.py` — added the `MQ_CYCLE_SUMMARY_PATH` file sink after the existing `$GITHUB_STEP_SUMMARY` append; uses `pathlib.Path(...).parent.mkdir(parents=True, exist_ok=True)` so a nested target dir is created on the fly
- `.github/merge-queue/tests/test_cmd_process.py` — three new tests (Section 8b in the file): dual-sink content equality, file-sink-only when `$GITHUB_STEP_SUMMARY` is unset, and missing-parent-dir auto-create
- `.github/merge-queue/.gitignore` — added `cycle-summary.md` so the artifact is never committed (generated by every local `pytest` run and every CI cycle)

## Cron + Concurrency Verification

**Static-block checks (local, via PyYAML):**

```
$ python3 -c "import yaml; d=yaml.safe_load(open('.github/workflows/mq-processor.yml')); ..."
STEP ORDER:
  1. Checkout merge-queue package from develop (pre-flight)
  2. Set up Python 3.12
  3. Install rocm_mq
  4. Pre-flight (WF-10 default-branch + PATH_TO_QUEUES loadable)
  5. Mint App installation token
  6. Process cycle
  7. Upload cycle summary as artifact
OK
```

- `concurrency.group` is the literal `mq-processor` (NO `${{ github.ref }}` interpolation — Pitfall 9 satisfied)
- `concurrency.cancel-in-progress` is `False` (RFC §4.7 invariant)
- `grep -c "cancel-in-progress: true" mq-processor.yml` → `0`
- `grep -cE "^\s+queue:" mq-processor.yml` → `0` (forbidden 2025 keyword absent)
- `cron` is the literal `'*/3 * * * *'`
- `permissions.contents: read` at workflow scope (default-deny per Pitfall 16)
- `timeout-minutes: 5` on the `process` job (< 3-min cron interval per Pitfall 6)
- `environment: mq-secrets` on the `process` job

**SHA pins (all four actions pinned per RFC §8):**

| Action | SHA | Tag |
|---|---|---|
| `actions/checkout` | `b4ffde65f46336ab88eb53be808477a3936bae11` | `v4.2.2` |
| `actions/setup-python` | `0b93645e9fea7318ecaed2b359559ac225c90a2b` | `v5.3.0` |
| `actions/create-github-app-token` | `bcd2ba49218906704ab6c1aa796996da409d3eb1` | `v3.2.0` |
| `actions/upload-artifact` | `ea165f8d65b6e75b540449e92b4886f43607fa02` | `v4` |

The `actions/upload-artifact@v4` SHA was resolved live on 2026-05-20 via:

```
gh api repos/actions/upload-artifact/git/refs/tags/v4 --jq .object.sha
→ ea165f8d65b6e75b540449e92b4886f43607fa02   (.object.type = "commit", direct SHA — no de-reference needed)
```

The discovery command is preserved as a comment in the workflow above the `uses:` line so the next person to bump v4 (or move to v5) has the same one-liner.

**Pytest suite:** `402 passed in 60.17s` (full `.github/merge-queue` suite); +3 new tests vs. Phase 2 baseline.

**Cron-fire / workflow_dispatch / artifact-download live verification:** _Pending operator action — see "Task 3 — Manual Fork Verification" below._

## Passive App-Identity Check 2 (Pitfall 2 / RESEARCH Area #23 Check 2)

Per the plan's `must_haves`, the App-identity slug-pin check (Check 2 in the original three-check pre-flight design) is satisfied **passively** by `process_cycle`'s `resolve_app_identity(client)` call at cycle startup (see `cmd_process.py:447`). A slug-pin mismatch surfaces as a clear-cause processor cycle error rather than as a pre-flight rejection. This is the deferral pre-confirmed in plan 03-04 Task 1, and it mirrors the passive-coverage pattern that plan 03-06 used for WF-12. This SUMMARY documents the location so a future reader (e.g., the Phase 4 audit author) can find where the check lives without re-reading the plan.

## Task 3 — Manual Fork Verification (Operator-Only, Not Executed)

Plan 03-08 Task 3 is a `checkpoint:human-verify` requiring fork access (the live App must be installed on `SamuelReeder/rocm-libraries`, the `mq-secrets` Environment must be live with `MQ_APP_CLIENT_ID` + `MQ_APP_PRIVATE_KEY`, and the workflow must be on `develop`). The orchestrator explicitly instructed this executor not to update STATE.md/ROADMAP.md, so the plan is intentionally left in a "code complete, operator verification pending" state.

When the operator runs Task 3:

1. Confirm file lands on develop: `gh api repos/SamuelReeder/rocm-libraries/contents/.github/workflows/mq-processor.yml?ref=develop --jq '.name'` → `mq-processor.yml`
2. Wait ≤5 min for the first cron fire OR trigger via `gh workflow run mq-processor.yml --repo SamuelReeder/rocm-libraries -f dry-run=false`
3. `gh run list --workflow=mq-processor.yml --repo SamuelReeder/rocm-libraries --limit 3` → recent run with conclusion `success`
4. Drill into the run: `gh run view <RUN_ID> --repo SamuelReeder/rocm-libraries --log | head -100` → pre-flight exits 0, App-token mint succeeds, process-cycle runs to completion
5. Verify artifact: `gh api repos/SamuelReeder/rocm-libraries/actions/runs/<RUN_ID>/artifacts --jq '.artifacts[].name'` → `cycle-summary-<RUN_ID>` present and downloadable
6. Verify dry-run: `gh workflow run mq-processor.yml --repo SamuelReeder/rocm-libraries -f dry-run=true` → success + zero PR mutations
7. Verify concurrency: trigger two `workflow_dispatch` invocations back-to-back; second is queued (NOT canceled)

When Task 3 completes successfully, the operator can update STATE.md / ROADMAP.md / REQUIREMENTS.md per the standard end-of-plan flow.

## Decisions Made

See `key-decisions` in the frontmatter — nine load-bearing decisions, all sourced from RFC §4.7 / §4.9, CLAUDE.md "What NOT to Use", RESEARCH.md Areas #9/#11/#23, or the plan's `must_haves` block. The most consequential:

- **Concurrency stays static + `cancel-in-progress: false`** — the foundational stateless-processor property. The 2025 `queue:` keyword was deliberately rejected.
- **Pre-flight runs BEFORE the App-token mint** — carries plan 03-04 threat T-03-04-01 mitigation through; verified via the explicit step-order check in this SUMMARY.
- **App-identity Check 2 stays PASSIVE in process_cycle** — pre-confirmed deferral from plan 03-04; mirrors plan 03-06's WF-12 passive coverage.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Added `cycle-summary.md` to `.github/merge-queue/.gitignore`**
- **Found during:** Task 1 GREEN, after running the full test suite
- **Issue:** Tests not setting `MQ_CYCLE_SUMMARY_PATH` default to writing `cycle-summary.md` in cwd (= `.github/merge-queue` for pytest). Every `pytest` run would leave an untracked `cycle-summary.md` in the package directory; without a `.gitignore` rule, future agents would inevitably commit it.
- **Fix:** Added `cycle-summary.md` entry to `.github/merge-queue/.gitignore` with a comment explaining the dual-sink rationale and pointing at the upload-artifact step.
- **Files modified:** `.github/merge-queue/.gitignore`
- **Verification:** `git status --short` after a fresh `pytest -x -q` run shows the file ignored, not untracked.
- **Committed in:** `229451dd371` (Task 1 GREEN commit — packaged with the implementation since the new feature is the file's source).

**2. [Rule 2 - Missing Critical] Added `if-no-files-found: warn` to the upload-artifact step**
- **Found during:** Task 2 (workflow authoring)
- **Issue:** The plan's action block did not specify `if-no-files-found`. The action defaults to `warn`, but a defensive cycle that exits *before* the summary render (e.g., a crash in `resolve_app_identity`) would produce no file at all. The default `warn` is correct but is worth being explicit — making it implicit would force a future reader to chase the action's default behavior. Explicit `if-no-files-found: warn` documents the intent (failed cycle → warning, not workflow-red on top of the underlying failure).
- **Fix:** Added `if-no-files-found: warn` under the `with:` block of the upload step.
- **Files modified:** `.github/workflows/mq-processor.yml`
- **Verification:** Static PyYAML parse confirms the key is set.
- **Committed in:** `93a4743d21b` (Task 2 commit).

**3. [Rule 3 - Blocking] Added `owner: ${{ github.repository_owner }}` to the App-token mint step**
- **Found during:** Task 2 (workflow authoring), mirroring mq-handler.yml's pattern
- **Issue:** The RESEARCH.md Area #9 example does not show the `owner:` input, but `actions/create-github-app-token@v3` requires it when the App is installed on an organization and the workflow needs an installation token scoped to that org. Without it, the action may fail to discover the installation in the fork context.
- **Fix:** Added `owner: ${{ github.repository_owner }}` to the `with:` block of the mint step, matching mq-handler.yml exactly.
- **Files modified:** `.github/workflows/mq-processor.yml`
- **Verification:** Matches the equivalent line in mq-handler.yml committed by plan 03-07; consistent with the action's published v3 input shape.
- **Committed in:** `93a4743d21b` (Task 2 commit).

---

**Total deviations:** 3 auto-fixed (2 missing critical + 1 blocking).
**Impact on plan:** All three are correctness/operability requirements (.gitignore prevents future commits of generated artifacts; `if-no-files-found: warn` documents the failure-mode contract; `owner:` is required for the action to function in the fork org context). No scope creep.

## Issues Encountered

None — the plan executed cleanly through its two automatable tasks. The TDD RED phase failed as expected (file sink did not exist); the GREEN phase made the three new tests pass on the first run; the full suite remained green (402 passed). The workflow YAML parsed and passed every static structural check on the first author pass.

## User Setup Required

None at this stage. Live verification requires the operator to perform Task 3 (see "Task 3 — Manual Fork Verification" above), which depends on plan 03-05's already-completed App + mq-secrets Environment setup on the fork. No new secrets, vars, or App permissions are introduced by this plan beyond what plan 03-05 already delivered.

## Next Phase Readiness

- **Plan 03-09 (whatever follows the cron processor)** can rely on `mq-processor.yml` being on develop and on `cycle-summary.md` being uploaded as `cycle-summary-<RUN_ID>` every cycle.
- **Plans 03-11 .. 03-16 (Wave 4 dogfood drivers)** now have a structured per-cycle summary they can download via the GitHub artifacts API (`actions/list_workflow_run_artifacts` → `download_artifact_archive`) — this was the explicit unblocker called out in RESEARCH.md Area #11.
- **Phase 4 (tamper audit)** inherits the SHA-pin discipline and the SELF_BOOTSTRAP banner pattern from this file alongside mq-handler.yml; no changes needed to the audit's path-rejection list (`.github/workflows/**` already covers it).
- **No blockers.** The only outstanding item is the operator-only Task 3 manual verification.

## Self-Check

Verifying claims before handing back:

| Claim | Verification | Result |
|---|---|---|
| `.github/workflows/mq-processor.yml` exists | `[ -f .github/workflows/mq-processor.yml ]` | FOUND |
| Task 1 RED commit exists | `git log --oneline -10 \| grep e0ebe9f42b3` | FOUND |
| Task 1 GREEN commit exists | `git log --oneline -10 \| grep 229451dd371` | FOUND |
| Task 2 commit exists | `git log --oneline -10 \| grep 93a4743d21b` | FOUND |
| No `cancel-in-progress: true` | `grep -c "cancel-in-progress: true" mq-processor.yml` → `0` | OK |
| No `queue:` keyword | `grep -cE "^\s+queue:" mq-processor.yml` → `0` | OK |
| Static `mq-processor` group | PyYAML asserts `d['concurrency']['group'] == 'mq-processor'` | OK |
| Cron `*/3 * * * *` literal | PyYAML asserts `d[True]['schedule'][0]['cron']` matches | OK |
| Pre-flight before App-token mint | Step-order index check | OK |
| Full test suite passes | `pytest -x -q` → `402 passed` | OK |
| ruff + mypy clean on touched files | `ruff check` + `mypy` → `All checks passed` / `Success` | OK |

## Self-Check: PASSED

---
*Phase: 03-handler-processor-on-fork*
*Completed: 2026-05-20*
