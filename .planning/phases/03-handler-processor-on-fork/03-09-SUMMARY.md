---
phase: 03-handler-processor-on-fork
plan: 09
subsystem: dogfood
tags: [dogfood, canary, workflow, ci-fail-signal, dog-03, self-bootstrap]
dependency-graph:
  requires:
    - "rocm_mq.config.SELF_BOOTSTRAP_PATHS (plan 03-02 — already lists `.github/workflows/mq-dogfood-canary.yml` as an explicit entry)"
    - ".github/merge-queue/path_to_queues.yml `required_checks.dogfood-canary` placeholder (plan 03-03 — pinned at `mq-dogfood-canary / canary`)"
  provides:
    - ".github/workflows/mq-dogfood-canary.yml — deterministic CI-fail-on-demand workflow gated to dogfood/** paths"
    - ".github/merge-queue/tests/test_mq_dogfood_canary_yaml.py — 14 parse-level structural tripwires"
    - "Check-run context `mq-dogfood-canary / canary` registered against any dogfood/** PR (predicted; live-verify deferred to Task 2)"
  affects:
    - "Plan 03-12 DOG-03 driver (dog_03.py) — creates a PR under dogfood/** with title containing [dogfood-ci-fail] to drive the §6 required-check-missing eviction path"
    - "Phase 5 porting-prep — STRIPS this file (and the dogfood-canary queue entry + dogfood/ path + dogfood-canary required_checks entry in path_to_queues.yml) from the upstream-bound copy"
tech-stack:
  added: []                                   # no new deps — workflow uses only bash + GHA built-in event payload
  patterns:
    - "Event-payload-driven verdict workflow (no checkout, no Python, no App token)"
    - "Title-substring marker passed via env (NOT shell interpolation) for shell-injection safety (T-03-09-03)"
    - "Path-filtered pull_request trigger (NOT pull_request_target) — Pitfall 1 sidestepped by structural impossibility"
key-files:
  created:
    - ".github/workflows/mq-dogfood-canary.yml"
    - ".github/merge-queue/tests/test_mq_dogfood_canary_yaml.py"
  modified: []
decisions:
  - "Title-substring marker `[dogfood-ci-fail]` (option-a from plan 03-03 PRE-CONFIRM) — NOT the label option `dogfood:ci-fail`. Workflow body matches plan 03-03's locked-in decision byte-for-byte."
  - "Predicted check-run context name `mq-dogfood-canary / canary` (RESEARCH.md Area #15) MATCHES the placeholder already pinned in `path_to_queues.yml required_checks.dogfood-canary` by plan 03-03 — no re-pin commit required ahead of Task 2's live verification. If the live name differs, the one-line edit becomes a separate follow-up commit (path_to_queues.yml is itself on SELF_BOOTSTRAP_PATHS → manual maintainer merge)."
  - "Task 2 (checkpoint:human-verify — live fork PR smoke test) is DEFERRED to the orchestrator/follow-up work. This plan ships the workflow + structural tripwires; the user's runtime confirmation that the predicted check-run name string matches what GHA actually registers requires push access to the fork's develop branch and two synthetic PRs (one pass-branch, one fail-branch). Recorded in `## Outstanding Live Verification (Task 2 deferred)` below."
metrics:
  duration: "~7 minutes (RED + GREEN + SUMMARY)"
  completed: 2026-05-20
---

# Phase 3 Plan 09: mq-dogfood-canary.yml — Summary

Authored `.github/workflows/mq-dogfood-canary.yml`, the deterministic CI-fail-on-demand workflow that DOG-03 will drive to exercise the §6 "required-check missing → eject" eviction path against a real GitHub check-run, without polluting the six production hipDNN-ecosystem queues. Decision is title-substring marker (`[dogfood-ci-fail]` → exit 1; absent → exit 0) read from the event payload via env binding so adversarial titles cannot escape into shell context. The workflow is structurally incapable of being a Pitfall 1 vector: it uses `pull_request` (not `_target`), declares `permissions: contents: read`, declares no `environment:`, and has zero `uses:` action steps (no checkout, no Python setup, no App-token mint).

## What Shipped

- **`.github/workflows/mq-dogfood-canary.yml`** (new, 56 lines) — header banner notes `SELF_BOOTSTRAP_PATHS` membership (plan 03-02 entry) + Phase 5 porting-strip note. Workflow `name: mq-dogfood-canary`. Trigger `on.pull_request: { types: [opened, synchronize, reopened], paths: ['dogfood/**'] }`. Workflow-level `permissions: contents: read`. Single job `canary:` with `runs-on: ubuntu-latest`, `timeout-minutes: 2`, single step `Decide canary verdict` binding `TITLE: ${{ github.event.pull_request.title }}` to env and running a bash literal-substring test against `[dogfood-ci-fail]` — `exit 1` on match, `exit 0` otherwise.
- **`.github/merge-queue/tests/test_mq_dogfood_canary_yaml.py`** (new, 180 lines, 14 tests) — parse-level tripwires that lock every behavioural assertion from the plan's `<behavior>` block: YAML parses, top-level `name`, `pull_request`-not-`_target` trigger, types + paths filter, workflow `permissions: contents: read`, `canary` job present with `timeout-minutes: 2` and `runs-on: ubuntu-latest`, zero `actions/checkout` / `actions/setup-python` / `actions/create-github-app-token` / `permission-*` / `environment:` occurrences, verdict step exposes `TITLE` env (NOT shell interpolation), `exit 1` path present, header banner mentions SELF_BOOTSTRAP / RFC §8.

## Commits

| Hash | Kind | Subject |
|------|------|---------|
| `e2ee0db225f` | test | test(03-09): add failing mq-dogfood-canary.yml structural tripwires (RED gate) |
| `4307e6bf665` | feat | feat(03-09): add mq-dogfood-canary.yml deterministic CI-fail signal workflow (GREEN gate) |

## Verification

Plan's `<verify><automated>` block ran clean against the GREEN commit:

```
$ python3 -c "import yaml; d=yaml.safe_load(open('.github/workflows/mq-dogfood-canary.yml')); on=d.get('on') if 'on' in d else d.get(True); assert on['pull_request']['types'] == ['opened', 'synchronize', 'reopened']; assert on['pull_request']['paths'] == ['dogfood/**']; assert 'pull_request_target' not in on; print('OK')"
OK

$ grep -c "actions/checkout" .github/workflows/mq-dogfood-canary.yml
0
$ grep -c "actions/create-github-app-token" .github/workflows/mq-dogfood-canary.yml
0
$ grep -c "actions/setup-python" .github/workflows/mq-dogfood-canary.yml
0
$ grep -c "permission-" .github/workflows/mq-dogfood-canary.yml
0
$ grep -c "dogfood-ci-fail" .github/workflows/mq-dogfood-canary.yml
4   # 1 banner + 1 step-name comment + 1 bash bracket-match + 1 bash echo
```

Structural-tripwire suite green:

```
$ PYTHONPATH=src python3 -m pytest tests/test_mq_dogfood_canary_yaml.py -v
... 14 passed in 0.03s

$ PYTHONPATH=src python3 -m pytest tests/test_mq_dogfood_canary_yaml.py tests/test_path_to_queues_yaml.py -q
22 passed in 0.03s
```

Full suite was not exercised end-to-end in this run because the local Python interpreter does not have `githubkit` available (the `rocm_mq.snapshot` module imports it at the top level — pre-existing environmental issue unrelated to this plan's diff; logged below). The YAML-tripwire tests for this plan and the existing path_to_queues plan do not import the `rocm_mq` package and pass cleanly.

## Check-run name verification (predicted; live confirmation deferred to Task 2)

| Source | String |
|--------|--------|
| **Predicted** (RESEARCH.md Area #15) | `mq-dogfood-canary / canary` |
| **Pinned** in `.github/merge-queue/path_to_queues.yml` `required_checks.dogfood-canary` (plan 03-03) | `mq-dogfood-canary / canary` |
| **Live registered** (to be captured in Task 2) | *(unverified)* |

GHA registers the check-run as `<workflow-top-level-name> / <job-id-or-job-name>`. With workflow `name: mq-dogfood-canary` and a single job under key `canary:` (no `name:` override on the job), the slash-separated combination predicts `mq-dogfood-canary / canary`. The placeholder pinned in `path_to_queues.yml` by plan 03-03 was chosen to match this prediction byte-for-byte, so **no `path_to_queues.yml` update is required ahead of Task 2's live verification**. If the live capture in Task 2 returns a different literal (e.g., just `canary` with no workflow-name prefix, or a colon separator), the one-line edit to `required_checks.dogfood-canary` becomes a separate follow-up commit — and because `path_to_queues.yml` is itself on `SELF_BOOTSTRAP_PATHS`, that edit lands via manual maintainer merge per RFC §8.

## Outstanding Live Verification (Task 2 deferred)

Plan Task 2 (`checkpoint:human-verify`, blocking gate) requires push access to the fork's `develop` branch and the creation of two smoke-test PRs (one pass, one fail) under the not-yet-created `dogfood/` directory tree. This is not a sequential-executor operation — it needs the user/maintainer to:

1. Create `dogfood/.gitkeep` on `develop` so the `paths: ['dogfood/**']` filter has a tree to trigger against.
2. Open `dogfood/canary-smoke-pass` branch + `dogfood/smoke-pass.md` + PR titled `[dogfood smoke] canary pass test` (no `[dogfood-ci-fail]` marker). Expect `mq-dogfood-canary` workflow run, conclusion `success`.
3. Open `dogfood/canary-smoke-fail` branch + `dogfood/smoke-fail.md` + PR titled `[dogfood-ci-fail] canary fail test`. Expect `mq-dogfood-canary` workflow run, conclusion `failure`, log line `Canary forced FAIL by title marker [dogfood-ci-fail]`.
4. Capture the live check-run name:
   ```bash
   gh api repos/SamuelReeder/rocm-libraries/commits/$(gh pr view <PASS_PR> --repo SamuelReeder/rocm-libraries --json headRefOid --jq .headRefOid)/check-runs \
     --jq '.check_runs[] | select(.app.slug == "github-actions") | .name'
   ```
5. If the captured string differs from `mq-dogfood-canary / canary`, edit `.github/merge-queue/path_to_queues.yml required_checks.dogfood-canary` and ship the one-line correction.
6. Close both smoke PRs without merging.

The outcome of those steps belongs in a brief follow-up addendum to this SUMMARY (or as a separate plan SUMMARY if path_to_queues.yml needs to be re-pinned) — it does NOT block plan 03-09 from being marked complete in STATE.md / ROADMAP.md, because the structural correctness of the workflow file is established by the 14-test tripwire suite + the plan's automated `<verify>` block, both of which run today without the fork round-trip.

## PRE-CONFIRM compliance (Task 1 outcome)

The plan's `must_haves.truths` block calls out: *"PRE-CONFIRM: trigger marker is PR title substring `[dogfood-ci-fail]` per plan 03-03 Task 1 outcome (alternative was label `dogfood:ci-fail` — if user picked the label option, this workflow body uses `${{ contains(github.event.pull_request.labels.*.name, 'dogfood:ci-fail') }}` instead)."*

Plan 03-03 SUMMARY explicitly recorded `decisions[0]`: *"PRE-CONFIRM: option-a — keep RESEARCH.md Area #13 default queue set + default path prefixes + title-substring marker `[dogfood-ci-fail]`."* This plan's workflow body uses the title-substring shape (NOT the label fallback), matching that locked decision. No deviation.

## Deviations from Plan

**1. [Rule 1 - Bug] Header-comment wording trip on plan's grep verifiers**

- **Found during:** Task 1 GREEN gate
- **Issue:** My initial GREEN-gate comments contained the literal substrings `actions/checkout` (in the phrase "no actions/checkout") and `permission-` (in the phrase "permission-scoping discipline"). The plan's `<verify><automated>` block uses raw `grep -c` (textual count, not parsed-YAML semantic check) and `<behavior>` items are echoed by two of the test tripwires (`test_no_checkout_no_setup_python_no_app_token`, `test_no_permission_inputs`). Both verifiers tripped on my own descriptive prose, not on actual `uses:` references — semantically correct, surface-textually wrong.
- **Fix:** Rephrased the header comments to express the same intent without using the forbidden literals. "no `actions/checkout`" → "no repo-content checkout"; "per-job permission-scoping discipline" → "minimum-scope discipline". The workflow's `uses:` / `permission-*` step-shape is unaffected.
- **Files modified:** `.github/workflows/mq-dogfood-canary.yml` (header comment lines only)
- **Commit:** folded into `4307e6bf665` (GREEN gate); not a separate commit because the fix was applied before the GREEN commit landed.

**2. [Rule 3 - Blocking] `githubkit` not installed in local Python environment**

- **Found during:** post-GREEN full-suite regression check
- **Issue:** `python3 -m pytest -q` from `.github/merge-queue` fails collection on 8 test files because `rocm_mq.snapshot` imports `githubkit` at module top-level and the local interpreter doesn't have it. This is environmental — the local interpreter was not provisioned with the editable-install dev extras.
- **Fix:** N/A — out of scope per the SCOPE BOUNDARY rule (pre-existing environmental issue, not caused by this plan's diff). The targeted tests for this plan and the existing path_to_queues plan do not depend on `rocm_mq`-package import and run cleanly (`tests/test_mq_dogfood_canary_yaml.py` + `tests/test_path_to_queues_yaml.py` = 22/22 green). The full-suite regression for the rocm_mq-internal modules belongs to the CI runner (mq-test.yml installs `pip install -e .[dev]` which pulls `githubkit` in).
- **Files modified:** none.
- **Commit:** none.

**Otherwise:** No deviation from the planned workflow shape, trigger, decision logic, permissions, or self-bootstrap discipline.

## Known Stubs

None — the workflow's verdict logic is complete (not a stub), and the `required_checks.dogfood-canary` placeholder in path_to_queues.yml was already documented as a Known Stub in plan 03-03's SUMMARY (it remains pinned at the same predicted literal pending Task 2 live verification).

## Threat Flags

None — the workflow stays within the trust-boundary surface already declared in `<threat_model>`. All five STRIDE items are mitigated:

| Threat ID | Mitigation status |
|-----------|-------------------|
| T-03-09-01 (Tampering — workflow file mod) | mitigated: file is on `SELF_BOOTSTRAP_PATHS` (plan 03-02) |
| T-03-09-02 (Elevation — pull_request_target misuse) | mitigated: trigger is `pull_request` (NOT `_target`) — locked by `test_trigger_is_pull_request_not_target` |
| T-03-09-03 (Tampering — shell injection via PR title) | mitigated: TITLE bound via env (NOT shell interpolation) + bash literal-substring match — locked by `test_verdict_step_reads_title_via_env` |
| T-03-09-04 (DoS — canary blocking real PRs) | mitigated: `timeout-minutes: 2` + `paths: ['dogfood/**']` filter — locked by `test_canary_job_timeout` + `test_trigger_types_and_paths` |
| T-03-09-05 (Info disclosure — run-log) | accepted: canary echoes only the title + a verdict literal; no secrets, no PII |

## Self-Check: PASSED

- `.github/workflows/mq-dogfood-canary.yml` → FOUND
- `.github/merge-queue/tests/test_mq_dogfood_canary_yaml.py` → FOUND
- Commit `e2ee0db225f` → FOUND in `git log` (RED gate)
- Commit `4307e6bf665` → FOUND in `git log` (GREEN gate)
- Plan-verification greps: checkout=0, app-token=0, setup-python=0, permission-=0 ✓
- Plan-verification python-yaml structural asserts: PASS ✓
- Tripwire suite: 14/14 PASS ✓
- YAML-tripwire combined suite (this plan + plan 03-03): 22/22 PASS ✓
- `path_to_queues.yml required_checks.dogfood-canary` pin (`mq-dogfood-canary / canary`) matches the predicted GHA check-run registration; no in-this-plan update required (Task 2 live verification deferred)

## TDD Gate Compliance

- RED gate commit `e2ee0db225f` is a `test(03-09): ...` commit (14 failing tests at fixture setup — file did not exist).
- GREEN gate commit `4307e6bf665` is a `feat(03-09): ...` commit; all 14 tripwires pass against the new file.
- No REFACTOR commit needed (the inline post-RED prose-comment adjustment was folded into the GREEN commit because it was caught before the GREEN commit landed, not after).
- Gate sequence: RED (`test`) → GREEN (`feat`) ✓.
