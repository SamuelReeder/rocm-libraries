---
phase: 03-handler-processor-on-fork
verified: 2026-05-20T00:00:00Z
status: human_needed
score: 4/6 must-haves verified by code; 2/6 require live-fork operator runs
overrides_applied: 0
re_verification:
  previous_status: none
  previous_score: n/a
  gaps_closed: []
  gaps_remaining: []
  regressions: []
human_verification:
  - test: "Deploy mq-handler.yml + mq-processor.yml + mq-dogfood-canary.yml + path_to_queues.yml to develop on SamuelReeder/rocm-libraries"
    expected: "After PR merges to develop on the fork, mq-processor cron begins firing every 3 min and posts $GITHUB_STEP_SUMMARY entries visible at https://github.com/SamuelReeder/rocm-libraries/actions/workflows/mq-processor.yml. mq-handler subscribes to issue_comment + pull_request_target + pull_request_review events with the per-job App-token mint succeeding (verifiable from the workflow run logs showing the create-github-app-token@bcd2ba49... step's post-step revoke)."
    why_human: "Workflow files only fire from the branch the deployment-branch rule trusts (mq-secrets Environment is scoped to develop-only deployment branches per plan 03-05). They cannot fire from this feature branch; merging the Phase 3 implementation PR is the operator step that brings them live. This is a one-time deployment action with no automated equivalent. Suggested command: `gh pr create --base develop --title 'Phase 3: handler+processor on fork' --body <ROADMAP Phase 3 summary>` then merge once Phase 3 PR review completes."
  - test: "Run DOG-02 driver against live fork — merge conflict at activation"
    expected: "`python -m rocm_mq.dogfood.dog_02 --owner SamuelReeder --repo rocm-libraries` exits 0 within TIMEOUT_S=720 (12 min); writes .planning/phases/03-handler-processor-on-fork/dogfood-runs/dog_02-<utc>.json with `status: pass` and a timeline containing `merge_conflict_detected` + `ejected` events; eject status comment on the dogfood PR contains the verbatim phrase `merge conflict with develop`."
    why_human: "Driver creates real PR on the fork, requires live App-installation token + processor cron to be firing (i.e., post-deployment step above must be complete). Sequencing: this is the first dogfood run because its 12-min budget is the shortest and its seed pattern (Contents-API write to develop before PR create) exercises the activation-time merge path most directly."
  - test: "Run DOG-03 driver against live fork — CI failure during evaluation"
    expected: "`python -m rocm_mq.dogfood.dog_03 --owner SamuelReeder --repo rocm-libraries` exits 0 within TIMEOUT_S=1200 (20 min); JSON output records `status: pass` and an eject status comment naming the canary check (substring resolved at runtime from path_to_queues.yml `required_checks.dogfood-canary`)."
    why_human: "Requires mq-dogfood-canary.yml deployed to develop on the fork — the canary fires from `pull_request` on `dogfood/**` paths. Same live-fork prerequisites as DOG-02."
  - test: "Run DOG-04 driver against live fork — simultaneous /merge idempotency"
    expected: "`python -m rocm_mq.dogfood.dog_04 --owner SamuelReeder --repo rocm-libraries` exits 0 within TIMEOUT_S=60; the second /merge comment receives only the eyes-reaction (no duplicate status comment, no label flip); single status comment with marker `<!-- rocm-mq-status -->`."
    why_human: "Handler-fast path — runs in <1 min but still requires live handler workflow + App token."
  - test: "Run DOG-05 driver against live fork — author push between activation and squash"
    expected: "`python -m rocm_mq.dogfood.dog_05 --owner SamuelReeder --repo rocm-libraries` exits 0 within TIMEOUT_S=900 (15 min); eject status comment contains the verbatim RFC §6 phrase `activation invalid (branch updated or label tampered)`."
    why_human: "Requires both handler + processor live on develop; the eject only fires on the cycle AFTER the author push, which depends on the cron schedule. Activation → push → next-cycle eject is the full WF-12 demonstration loop."
  - test: "Run DOG-06 driver against live fork — RFC §4.2 5-PR worked example (marquee scenario)"
    expected: "`python -m rocm_mq.dogfood.dog_06 --owner SamuelReeder --repo rocm-libraries` exits 0 within the ~60-min budget; PR squash-merge order observable on the fork's develop matches A → B,C (parallel) → D → E; aggregator records `status: pass`."
    why_human: "60-min wall-time, requires real CI on five PRs (~$0.50–$2 GHA budget per the plan's threat register). This is the marquee Phase 3 demonstration and also the Phase 5 porting-prep reference run — its JSON output is the canonical fork-vs-RFC §4.2 evidence."
  - test: "Run DOG-07 driver against live fork — approval revoked between enqueue and squash"
    expected: "`APPROVER_TOKEN=<second-account-PAT> python -m rocm_mq.dogfood.dog_07 --owner SamuelReeder --repo rocm-libraries` exits 0 within TIMEOUT_S=900; eject status comment contains the verbatim RFC §6 phrase `approval revoked`."
    why_human: "Requires a SECOND collaborator account on the fork (the approver identity, distinct from the App identity that posts /merge). Plan 03-15 PRE-CONFIRM resolved this mechanism but defers the actual collaborator provisioning to operator runtime. `APPROVER_TOKEN` env var must be set before invocation."
  - test: "Run DOG-08 driver against live fork — /merge on PR touching no opted-in path"
    expected: "`python -m rocm_mq.dogfood.dog_08 --owner SamuelReeder --repo rocm-libraries` exits 0 within TIMEOUT_S=60; handler posts an explanatory rejection comment on the PR (no mq:queued label applied, no status comment with active state)."
    why_human: "Handler-fast path. Requires live handler workflow."
  - test: "Re-run aggregator to populate DOGFOOD-RESULTS.md from live-fork JSONs"
    expected: "`python -m rocm_mq.dogfood.aggregator` (run from .github/merge-queue/) reads the 7 dogfood-runs/*.json files produced above and replaces the 7 'not yet run' sections in DOGFOOD-RESULTS.md with the most-recent passing JSON per scenario. Result file shows `status: pass` for DOG-02..DOG-08."
    why_human: "Aggregator is automated and unit-tested; the human-needed prerequisite is that the JSON inputs exist (i.e., all 7 live drivers above have been run at least once)."
  - test: "Phase 3 SC#5 — full RFC §4.2 5-PR worked example replays end-to-end on the fork"
    expected: "Observable from DOG-06's live run above: A merges first, B and C merge in parallel, D merges next, E merges last (per RFC §4.2). Squash-merge timestamps + processor $GITHUB_STEP_SUMMARY entries on https://github.com/SamuelReeder/rocm-libraries/actions/workflows/mq-processor.yml are the canonical evidence."
    why_human: "Identical to DOG-06 above; this is the success-criterion-level statement of the same operator action. Listed separately because it is the named Phase 3 SC#5 and Phase 5 porting-prep will cite it by SC number."
gaps: []
deferred: []
---

# Phase 3: Handler + Processor on Fork — Verification Report

**Phase Goal:** A real fork PR can be `/merge`d and squash-merged through the queue end-to-end, with all RFC §6 happy-path and edge-case scenarios demonstrated against the fork's real CI.
**Verified:** 2026-05-20
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (mapped from ROADMAP Phase 3 Success Criteria)

| #   | Truth (from ROADMAP SC)                                                                                                                                  | Status            | Evidence                                                                                                                                                                                                                                                                                                                                                                                                  |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | SC#1: App registered on fork with §4.9 perms; `mq-secrets` Environment scoped to `develop` deployment branches; default-branch pre-flight check live.    | ✓ VERIFIED        | Plan 03-05 SUMMARY captures live `gh api` responses: App `rocm-mq-fork` ID `3776213`, Environment `mq-secrets` (id 15564037987) with custom branch policy `develop` (id 49833186), repo vars `MQ_APP_CLIENT_ID/MQ_APP_ID/MQ_APP_SLUG` set. `preflight.py` module exists (line 73: `_parse_args`, line 129: `main`). `python -m rocm_mq preflight --help` succeeds. mq-processor.yml runs preflight first. |
| 2   | SC#2: `/merge` runs live perm check, at-enqueue gates, applies labels via App token, posts marker-scoped status comment; second `/merge` is a no-op.     | ✓ VERIFIED (code) | `cmd_handle.py` exists with `_check_perm` (line 168), `_check_at_enqueue_gates` (line 213), `_apply_labels` (line 265), `_upsert_status_comment` (line 307), `_post_eyes_reaction` (line 365), `parse_commands` (line 116), `is_self_bootstrap` (line 140), `_handle_merge` (line 461), `_handle_dequeue` (line 594). Wired via `python -m rocm_mq handle` (verified). Unit tests pass.                   |
| 3   | SC#3: 3-min cron + `workflow_dispatch:` + static `concurrency: mq-processor` + per-job timeouts; §4.6 algorithm + `$GITHUB_STEP_SUMMARY`.                | ✓ VERIFIED (code) | `mq-processor.yml`: `schedule: cron '*/3 * * * *'`, `workflow_dispatch:` with `dry-run` bool input, `concurrency: { group: mq-processor, cancel-in-progress: false }` (lines 72–74, STATIC literal — not interpolated). `actions/create-github-app-token@bcd2ba49...` SHA-pinned. cmd_process.py threads `now` value, runs build→derive→decide→dispatch.                                                  |
| 4   | SC#4: Every RFC §6 fork-dogfood scenario (DOG-02..DOG-08) demonstrably ejects with documented reason on the fork against real CI.                        | ✗ LIVE-DEFERRED   | All 7 drivers exist (`.github/merge-queue/src/rocm_mq/dogfood/dog_{02,03,04,05,06,07,08}.py`); all have green unit tests via `_DogfoodFake` (74 tests pass). However, `DOGFOOD-RESULTS.md` reports `Status: not yet run` for all 7 scenarios; `dogfood-runs/` contains only `.gitkeep`. Operator-run live drivers required — see `human_verification` block.                                              |
| 5   | SC#5: RFC §4.2 5-PR worked example replays end-to-end on the fork against real CI.                                                                       | ✗ LIVE-DEFERRED   | DOG-06 driver authored (plan 03-14 SUMMARY); unit-test mode green. Live-fork Task 2 (60-min checkpoint) explicitly deferred per executor objective. Same operator action as the DOG-06 row in `human_verification`.                                                                                                                                                                                       |
| 6   | SC#6: Processor `--dry-run` flag emits Action list to stdout/`$GITHUB_STEP_SUMMARY` without invoking mutators.                                           | ✓ VERIFIED        | `cmd_process.py` line 99 takes `dry_run: bool`; line 113 documents "If dry_run: print each action to stdout and return empty"; line 159 implements the short-circuit. mq-processor.yml `workflow_dispatch.inputs.dry-run` exposes the flag at the workflow surface.                                                                                                                                       |

**Score:** 4/6 SCs verified by code today; 2/6 (SC#4, SC#5) require live-fork operator runs that are intentionally deferred to post-deployment.

### Required Artifacts

| Artifact                                                          | Expected                                                                          | Status     | Details                                                                                                                            |
| ----------------------------------------------------------------- | --------------------------------------------------------------------------------- | ---------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `.github/workflows/mq-handler.yml`                                | issue_comment + pull_request_target + pull_request_review triggers; App-token mint | ✓ VERIFIED | Exists; SHA-pinned `actions/create-github-app-token@bcd2ba49...`; command job + audit-stub job; workflow-level `contents: read`.   |
| `.github/workflows/mq-processor.yml`                              | cron `*/3 * * * *`, dispatch fallback, static concurrency, preflight gate         | ✓ VERIFIED | Exists; static `concurrency: mq-processor`; SHA-pinned App-token action; preflight runs before App-token mint per plan 03-08.      |
| `.github/workflows/mq-dogfood-canary.yml`                         | `pull_request` (NOT `_target`), `dogfood/**` path filter, deterministic exit code | ✓ VERIFIED | Exists; title-marker `[dogfood-ci-fail]` switches exit code. In `SELF_BOOTSTRAP_PATHS`.                                            |
| `.github/workflows/mq-test.yml` (pre-existing, P2 carry-forward)  | Per-commit pytest CI                                                              | ✓ VERIFIED | Exists; triggers on push to develop + user branches and PRs to develop.                                                            |
| `.github/merge-queue/src/rocm_mq/config.py`                       | `SELF_BOOTSTRAP_PATHS` constant + `load_from_develop` loader + APP env constants  | ✓ VERIFIED | `SELF_BOOTSTRAP_PATHS` tuple at line 54 (4 entries + commented future-slot); `APP_SLUG_ENV` / `APP_ID_ENV` at lines 84–85; `load_from_develop` at line 97. |
| `.github/merge-queue/src/rocm_mq/preflight.py`                    | Module exposing main() for default-branch + path_to_queues-loadable checks        | ✓ VERIFIED | File exists; `_parse_args` (73), `_fail` (101), `main` (129). Wired via `python -m rocm_mq preflight`.                             |
| `.github/merge-queue/src/rocm_mq/cmd_handle.py`                   | /merge + /dequeue parser, gates, label apply, status-comment upsert               | ✓ VERIFIED | File exists; `parse_commands` (116), `is_self_bootstrap` (140), full gate stack + comment upsert + eyes-reaction. Wired via `python -m rocm_mq handle`. |
| `.github/merge-queue/src/rocm_mq/cmd_process.py` (refactor)       | argparse subcommands: process-cycle/handle/audit/preflight                        | ✓ VERIFIED | `add_subparsers(dest='subcommand', required=True)` at line 323; 4 subparsers (lines 326/359/381/390); each with `set_defaults(func=run_*)`. `--help` confirms wiring. |
| `.github/merge-queue/src/rocm_mq/dogfood/__init__.py` + `_base.py` | DogfoodResult + create_dogfood_pr + post_command + poll_pr_state + emit_result + download_cycle_summary_artifact | ✓ VERIFIED | All 6 names present at expected line numbers in `_base.py`.                                                                        |
| `.github/merge-queue/src/rocm_mq/dogfood/dog_{02..08}.py` (7 drivers) | One driver per RFC §6 scenario                                                | ✓ VERIFIED | 7 files present. Each has `run_scenario` (unit-test seam) + `main` (live-fork CLI). Unit tests pass.                               |
| `.github/merge-queue/src/rocm_mq/dogfood/aggregator.py`            | Reads dogfood-runs/*.json, renders DOGFOOD-RESULTS.md                            | ✓ VERIFIED | `collect_runs`/`latest_passing`/`render_markdown`/`main` present. `--help` succeeds. Unit test `test_dogfood_aggregator.py` passes. |
| `.github/merge-queue/path_to_queues.yml`                          | Hand-written config: hipDNN + 4 providers + integration-tests + dogfood-canary    | ✓ VERIFIED | Exists; preamble cites live-verification of required-check name (TheRock CI Summary) against fork PR #21 head SHA per plan 03-03. |
| `.planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md` | Phase 3 verification artifact aggregating dog_02..dog_08 outcomes                | ⚠ STUB     | File exists with correct structure (7 scenario sections, regenerate command, anchors); every section reads `Status: not yet run` because `dogfood-runs/` is empty (`.gitkeep` only). This is the visible signal that SC#4 + SC#5 are live-deferred. |

### Key Link Verification

| From                                  | To                                                  | Via                                                              | Status   | Details                                                                                          |
| ------------------------------------- | --------------------------------------------------- | ---------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------ |
| `mq-handler.yml` command job          | `rocm_mq.cmd_handle.main`                           | `python -m rocm_mq handle --repo ... --event-path $GITHUB_EVENT_PATH` | ✓ WIRED  | Subparser path verified in cmd_process.py:359 + cmd_handle.main signature.                       |
| `mq-processor.yml` process job        | `rocm_mq.cmd_process.run_process_cycle`             | `python -m rocm_mq process-cycle --repo ...`                     | ✓ WIRED  | Subparser path verified; SHA-pinned App-token mints the auth token consumed by the cycle.        |
| `mq-processor.yml` pre-flight step    | `rocm_mq.preflight.main`                            | `python -m rocm_mq preflight --repo ...` (before App-token mint) | ✓ WIRED  | Plan 03-08 places preflight BEFORE the App-token step so a misconfigured fork never holds creds. |
| `cmd_handle.is_self_bootstrap`        | `rocm_mq.config.SELF_BOOTSTRAP_PATHS`               | fnmatch intersection of PR-changed-files                         | ✓ WIRED  | Function at cmd_handle.py:140; reads the constant from config.py:54.                             |
| `cmd_handle._load_config`             | `rocm_mq.config.load_from_develop`                  | GitHub Contents API at `ref=develop`                             | ✓ WIRED  | cmd_handle.py:420 → config.py:97.                                                                |
| `dogfood/aggregator.collect_runs`     | `dogfood-runs/*.json` inputs                        | filesystem glob                                                  | ⚠ EMPTY  | Aggregator code wired; input directory empty (no live-fork runs yet). Triggers fallback "not yet run" rendering. |
| `mq-handler.yml` audit job            | (Phase 4 stub today)                                | per-job permissions `issues: write + statuses: write` (NO `pull-requests: write`) | ✓ WIRED  | Permissions block locked NOW per RFC §4.3.1; body is a no-op pending Phase 4.                    |

### Data-Flow Trace (Level 4)

Phase 3 produces no UI surfaces; cycle-summary.md is the only generated artifact. Data flow checked at the workflow level (live triggers + cron schedule + concurrency group) rather than per-component rendering.

| Artifact                                       | Data Variable          | Source                                          | Produces Real Data            | Status                                                                                                  |
| ---------------------------------------------- | ---------------------- | ----------------------------------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------- |
| `$GITHUB_STEP_SUMMARY` (processor)             | render_cycle_summary() | snapshot.build_snapshot → decide_cycle outcomes | ✓ when processor runs live    | Code path wired (cmd_process.py imports render_cycle_summary from summary.py); awaits live cron firing. |
| `DOGFOOD-RESULTS.md`                           | aggregator render      | `dogfood-runs/*.json`                           | ✗ static placeholders today   | Aggregator wired; input set empty until operator runs the 7 drivers.                                    |
| `cycle-summary.md` artifact upload             | render_cycle_summary() | same as above                                   | ✓ when processor runs live    | mq-processor.yml uploads it per plan 03-08; dogfood drivers download via `download_cycle_summary_artifact`. |

### Behavioral Spot-Checks

| Behavior                                                     | Command                                                                         | Result                                       | Status |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------- | -------------------------------------------- | ------ |
| Full test suite passes                                       | `pytest --tb=no -q` (in `.github/merge-queue/`)                                 | 529 passed in 52.81s; 8 snapshots passed     | ✓ PASS |
| Dogfood driver unit tests pass                               | `pytest tests/test_dogfood_dog_*.py --tb=no -q`                                 | 74 passed in 0.71s                           | ✓ PASS |
| `python -m rocm_mq preflight --help` lists --repo            | `python -m rocm_mq preflight --help`                                            | usage line + `--repo REPO` shown             | ✓ PASS |
| `python -m rocm_mq handle --help` lists --repo + --event-path | `python -m rocm_mq handle --help`                                              | usage line + both flags shown                | ✓ PASS |
| `python -m rocm_mq.dogfood.aggregator --help` lists --input  | `python -m rocm_mq.dogfood.aggregator --help`                                   | usage line + `--input` shown                 | ✓ PASS |
| App-token action SHA-pinned (not `@v3` floating tag)         | `grep create-github-app-token@ .github/workflows/mq-{handler,processor}.yml`    | both pin SHA `bcd2ba49...` with `# v3.2.0` comment | ✓ PASS |
| Processor concurrency is STATIC literal `mq-processor`       | `grep -A2 concurrency: .github/workflows/mq-processor.yml`                      | `group: mq-processor` + `cancel-in-progress: false` | ✓ PASS |
| Live workflows actually fire on develop                      | `gh run list --workflow=mq-processor.yml --repo SamuelReeder/rocm-libraries`    | (cannot run from verifier — branch not on develop) | ? SKIP — see human_verification |

### Probe Execution

No probe scripts declared in PLAN/SUMMARY for this phase (the dogfood drivers are the closest analog and are intentionally deferred to operator-runtime per the executor's directive).

### Requirements Coverage

| Requirement | Source Plan(s)           | Description                                                                                       | Status              | Evidence                                                                                                                                |
| ----------- | ------------------------ | ------------------------------------------------------------------------------------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| WF-01       | 03-06, 03-07             | mq-handler.yml /merge & /dequeue with live perm check via repos.getCollaboratorPermissionLevel    | ✓ SATISFIED         | cmd_handle._check_perm; mq-handler.yml issue_comment trigger.                                                                            |
| WF-02       | 03-06                    | At-enqueue gates: ≥1 approval, no failing required check, maintainer-edits, queue set non-empty   | ✓ SATISFIED         | cmd_handle._check_at_enqueue_gates (line 213).                                                                                          |
| WF-03       | 03-06                    | /merge idempotent (second invocation is no-op acknowledgement)                                    | ✓ SATISFIED (code)  | cmd_handle._handle_merge inspects existing mq:queued/mq:active labels; needs live demonstration via DOG-04.                              |
| WF-04       | 03-08                    | cron */3 + workflow_dispatch fallback; single concurrency group `mq-processor`; `cancel-in-progress: false` | ✓ SATISFIED         | mq-processor.yml schedule + dispatch + STATIC concurrency block.                                                                         |
| WF-05       | 03-08                    | Processor implements §4.6 algorithm; activation+evaluation never in same cycle                    | ✓ SATISFIED         | cmd_process.process_cycle → build_snapshot → derive_snapshot → decide_cycle → dispatch (line 100+).                                     |
| WF-06       | 03-08                    | Per-cycle $GITHUB_STEP_SUMMARY with queue depth, active PRs, outcomes, wall-time                  | ✓ SATISFIED (code)  | render_cycle_summary wired into cmd_process; awaits live cron firing for end-to-end demonstration.                                       |
| WF-07       | 03-08                    | --dry-run emits Action list without mutators                                                      | ✓ SATISFIED         | cmd_process line 113 + line 159 short-circuit; mq-processor.yml exposes `dry-run` workflow_dispatch input.                              |
| WF-08       | 03-02, 03-06, 03-07, 03-08 | actions/create-github-app-token@v3 SHA-pinned; GITHUB_TOKEN permissions scoped per §4.9         | ✓ SATISFIED         | SHA `bcd2ba49...` pinned in both workflows; `GITHUB_TOKEN` reserved for eyes-reaction only.                                              |
| WF-09       | 03-05, 03-07, 03-08      | App registered on fork w/ §4.9 perms; key in `mq-secrets` Environment scoped to develop refs      | ✓ SATISFIED         | Plan 03-05 SUMMARY captures live `gh api` responses for App ID 3776213 + Environment id 15564037987 + branch policy `develop`.          |
| WF-10       | 03-04, 03-07, 03-08      | Pre-flight verifies fork default branch IS `develop`                                              | ✓ SATISFIED         | preflight.py default-branch check; runs BEFORE App-token mint in mq-processor.yml.                                                       |
| WF-11       | 03-06                    | Single status comment via `<!-- rocm-mq-status -->` marker; renders queued/active/merged/ejected  | ✓ SATISFIED (code)  | cmd_handle._upsert_status_comment (line 307) + comment.py renderer (P1 carry-forward).                                                  |
| WF-12       | 03-06, 03-08, 03-13      | Force-push/new-commit invalidates activation; ejected next cycle                                  | ✓ SATISFIED (code)  | decision.py activation-status SHA binding (P1); DOG-05 driver authored; needs live demonstration via DOG-05.                            |
| DOG-02      | 03-10, 03-11             | Fork dogfood: merge conflict at activation → eject "merge conflict with develop"                  | ⚠ NEEDS LIVE RUN    | Driver + 9-test unit suite pass; live-fork Task 5 deferred.                                                                              |
| DOG-03      | 03-09, 03-12             | Fork dogfood: CI failure during evaluation → eject naming failed check                            | ⚠ NEEDS LIVE RUN    | Driver + canary workflow + 11-test unit suite pass; live-fork Task 2 deferred.                                                          |
| DOG-04      | 03-11                    | Fork dogfood: simultaneous /merge → idempotent no-op                                              | ⚠ NEEDS LIVE RUN    | Driver + unit tests pass; live-fork deferred.                                                                                            |
| DOG-05      | 03-13                    | Fork dogfood: author push between activation and squash → eject "activation invalid (...)"        | ⚠ NEEDS LIVE RUN    | Driver + 12-test unit suite pass; live-fork Task 2 deferred.                                                                            |
| DOG-06      | 03-14                    | Fork dogfood: RFC §4.2 5-PR worked example end-to-end                                             | ⚠ NEEDS LIVE RUN    | Driver + unit-test mode pass; live-fork Task 2 (60-min, ~$0.50–$2 GHA budget) deferred.                                                  |
| DOG-07      | 03-15                    | Fork dogfood: approval revoked between enqueue and squash → eject "approval revoked"              | ⚠ NEEDS LIVE RUN    | Driver + 14-test unit suite pass; live-fork Task 3 deferred AND requires second collaborator account + APPROVER_TOKEN.                  |
| DOG-08      | 03-11                    | Fork dogfood: /merge on no-opted-in path → handler-level rejection                                | ⚠ NEEDS LIVE RUN    | Driver + unit tests pass; live-fork deferred. cmd_handle.is_self_bootstrap covers this path in code.                                     |

All 12 WF-* + 7 DOG-* requirements claimed by Phase 3 plans are accounted for in code. The 7 DOG-* IDs require live-fork operator runs to flip from "code complete" to "demonstrated" per the phase goal's "demonstrated against the fork's real CI" clause.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| ---- | ---- | ------- | -------- | ------ |
| (none) | — | grep for FIXME/XXX/TBD across `src/rocm_mq/` returned zero hits | — | No unresolved debt markers in Phase 3 source. |

### Self-Check Pass Rate

All 17 Phase 3 SUMMARYs carry a Self-Check section; 16 explicitly write `## Self-Check: PASSED`; one (03-08) writes `## Self-Check` followed by `Verifying claims before handing back:` body — content reviewed, no failures recorded.

### Human Verification Required

10 items listed in frontmatter `human_verification:` block:

1. Deploy the four mq-* workflow files + path_to_queues.yml to develop on `SamuelReeder/rocm-libraries` (PR + merge).
2–8. Run each of the 7 dogfood drivers (DOG-02, DOG-03, DOG-04, DOG-05, DOG-06, DOG-07, DOG-08) against the live fork after step 1 completes.
9. Re-run `python -m rocm_mq.dogfood.aggregator` to refresh `DOGFOOD-RESULTS.md` from the 7 new JSON files.
10. Confirm Phase 3 SC#5 (RFC §4.2 5-PR worked example) is observable from the DOG-06 run's PR timeline + processor summaries.

Notes on sequencing:

- Item 1 is a one-time prerequisite for items 2–8 (workflows cannot fire until merged to develop; the `mq-secrets` Environment is deployment-branch-scoped to `develop` per plan 03-05).
- Items 2, 4, and 8 (DOG-02, DOG-04, DOG-08) are the fastest (60s–12min wall) — sensible smoke order.
- Item 6 (DOG-06) is the longest (~60 min wall, ~$0.50–$2 GHA cost) — the marquee scenario and the Phase 5 porting-prep reference.
- Item 7 (DOG-07) has the largest setup tax — requires a second collaborator account on the fork plus an `APPROVER_TOKEN` env var.
- Item 9 (aggregator) is automated and unit-tested; the human dependency is only the existence of the 7 JSON inputs.

### Gaps Summary

The Phase 3 codebase delivers every artifact and key link required by the ROADMAP Success Criteria 1, 2, 3, and 6. SC#4 and SC#5 are CODE-COMPLETE but LIVE-DEFERRED: every dogfood driver exists with green unit tests, but the phase goal's "demonstrated against the fork's real CI" clause cannot be evaluated until (a) the Phase 3 implementation lands on `develop` of the fork and (b) the 7 live drivers are run by the operator. This deferral was an explicit per-plan executor directive (search "deferred per the user's plan execution objective" across plans 03-09, 03-11..03-16) and matches the orchestrator policy stated in the verification prompt.

Verdict: **`human_needed`** — the gating step is operator action on the live fork, not additional code work. No coding gaps blocking the goal; the 7 live-fork runs + the workflow-to-develop merge are the remaining items.

---

_Verified: 2026-05-20_
_Verifier: Claude (gsd-verifier)_
