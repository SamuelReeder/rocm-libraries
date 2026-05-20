---
phase: 03-handler-processor-on-fork
plan: 12
subsystem: dogfood
tags: [dogfood, dog-03, ci-failure, required-check, eviction, canary-consumer]
dependency-graph:
  requires:
    - "rocm_mq.dogfood._base (plan 03-10 — DogfoodResult dataclass + create_dogfood_pr + post_command + poll_pr_state + emit_result)"
    - "rocm_mq.config.load_from_develop (plan 03-02 — Contents API reader for path_to_queues.yml on the develop ref)"
    - ".github/merge-queue/path_to_queues.yml required_checks.dogfood-canary (plan 03-03 — pins the canary check-run name the driver matches the eject reason against)"
    - ".github/workflows/mq-dogfood-canary.yml (plan 03-09 — supplies the deterministic check-run failure signal when the PR title contains [dogfood-ci-fail])"
  provides:
    - ".github/merge-queue/src/rocm_mq/dogfood/dog_03.py — DOG-03 driver (CI failure during evaluation -> eject naming the failed canary check)"
    - ".github/merge-queue/tests/test_dogfood_dog_03.py — 11 unit tests covering happy path, two failure modes, D-04 schema, timeline vocabulary, runtime config discovery, and a real-yaml sanity guard"
  affects:
    - "Plan 03-16 dogfood aggregator (consumes the per-run JSON dog_03 emits to dogfood-runs/)"
    - "Phase 5 porting-prep — STRIPS this driver alongside mq-dogfood-canary.yml and the dogfood-canary queue entry in path_to_queues.yml; the upstream-bound copy has no synthetic-canary requirement"
tech-stack:
  added: []
  patterns:
    - "Runtime canary-check-name discovery via config.load_from_develop (no hardcoded check string in driver source — re-pin in path_to_queues.yml flows through without driver edit)"
    - "FakeGitHub extension wires rest.repos.get_content alongside the rest.git / rest.pulls / rest.repos.create_or_update_file_contents seams dog_02 already established"
    - "Two-mode driver (run_scenario for unit tests; main for live fork) mirrors plan 03-11's dog_02 shape line-for-line modulo the DOG-03-specific eject-reason substring"
key-files:
  created:
    - ".github/merge-queue/src/rocm_mq/dogfood/dog_03.py"
    - ".github/merge-queue/tests/test_dogfood_dog_03.py"
  modified: []
decisions:
  - "Canary check-name resolved at runtime from path_to_queues.yml required_checks.dogfood-canary (NOT hardcoded). Rationale: plan 03-09 SUMMARY 'Outstanding Live Verification' notes the literal GHA-registered check-run name MAY differ from the predicted 'mq-dogfood-canary / canary' once the canary first fires; a re-pin in the YAML must propagate to the driver without a code change. Implementation: dog_03.run_scenario calls config.load_from_develop and extracts required_checks.dogfood-canary[0] at the top of every run; EXPECTED.reason_substring is built from the loaded string."
  - "Match the load-bearing prefix slice (split on ' / ') instead of the full check name. The slash-separated GHA convention pairs <workflow-name> / <job-id-or-name>; the workflow-name half ('mq-dogfood-canary') is the load-bearing identifier and the job-id half ('canary') may drift in a future plan 03-09 re-pin. Matching the prefix keeps the assertion stable across that drift while still being specific enough to distinguish from any other eject reason."
  - "Task 2 (checkpoint:human-verify — live fork run) is DEFERRED per the executor's objective ('do NOT actually run live against the fork in this plan execution — unit-tests only'). Mirrors plan 03-09's deferral pattern: the workflow + driver + tests ship today; the operator's live verification belongs to the post-deployment runbook (Phase 3 plan 03-05 App registration + plans 03-13/03-14/03-15 workflow deployment) and produces an addendum to this SUMMARY when it runs. The structural correctness of the driver is established by the 11-test unit suite + the plan's automated `<verify>` block, both of which pass today without any fork round-trip."
metrics:
  duration: "~25 minutes (context-load + RED + GREEN + SUMMARY)"
  completed: 2026-05-20
---

# Phase 3 Plan 12: dog_03.py — Summary

Authored `.github/merge-queue/src/rocm_mq/dogfood/dog_03.py`, the DOG-03 dogfood driver that exercises the §6 "CI failure during evaluation → eject" eviction path against the live fork. The driver creates a PR under `dogfood/` with the `[dogfood-ci-fail]` title marker (so the canary workflow from plan 03-09 exits 1 → required check fails on the PR head SHA), posts `/merge`, and polls for an eject status comment naming the canary's check-run name. The canary check name is resolved at runtime from `path_to_queues.yml required_checks.dogfood-canary` via `config.load_from_develop` — the driver source carries no hardcoded canary string, so a future re-pin (per plan 03-09's "Outstanding Live Verification") flows through automatically. Two-mode driver mirroring plan 03-11's dog_02 shape: `run_scenario(client, ...)` for unit-tested orchestration via FakeGitHub; `main()` for `python -m rocm_mq.dogfood.dog_03 --live` against the live fork. Per-run JSON emission to `.planning/phases/03-handler-processor-on-fork/dogfood-runs/` honors the D-04 schema.

## What Shipped

- **`.github/merge-queue/src/rocm_mq/dogfood/dog_03.py`** (new, 401 lines) — module docstring referencing RFC §6 'CI failure during evaluation'; constants `SCENARIO_ID="dog_03"`, `TIMEOUT_S=20*60`, `TITLE_PREFIX="[dogfood-ci-fail][dogfood dog_03]"`; helpers `_resolve_canary_check_name()` (loads path_to_queues.yml + extracts required_checks.dogfood-canary[0]), `_check_name_substring()` (slash-prefix slice for drift tolerance), `_build_eject_predicate()` (polls for an eject status comment containing both the rocm-mq-status marker AND the canary check-name substring); `run_scenario()` orchestrates resolve → create-PR-with-title-marker → /merge → poll-eject → emit JSON; `main()` is the live-fork CLI entrypoint (lazy githubkit import, GITHUB_TOKEN gating, return-code 0/1/2 contract). Timeline records the 7-event subset called for by plan 03-12's `<action>` block: pr_opened, merge_command_posted, mq_queued_label_applied, processor_cycle_started, activation_began, required_checks_failed (naming the canary check), ejected.
- **`.github/merge-queue/tests/test_dogfood_dog_03.py`** (new, 472 lines, 11 tests) — module constants (SCENARIO_ID, TIMEOUT_S, TITLE_PREFIX, callability); happy-path orchestration (eject body containing canary check name → passed=True, expected_outcome derived from loaded YAML, PR title carries marker, seed file under dogfood/, JSON emitted with passed=True); failure mode 1 (eject body without canary check name → TimeoutError, no false-positive on a merge-conflict-shaped eject); D-04 schema completeness (all 12 fields present in the emitted JSON); timeline vocabulary (required_checks_failed event present + names the canary check); runtime config discovery (custom required_checks pin like `renamed-canary / check` propagates to EXPECTED.reason_substring AND the eject-reason match — guards against a regression that hardcodes the canary string); real-file sanity (production path_to_queues.yml continues to pin a dogfood-canary required_checks entry containing the 'mq-dogfood-canary' prefix the driver matches on).

## Commits

| Hash | Kind | Subject |
|------|------|---------|
| `f69a29f7475` | test | test(03-12): add failing tests for dog_03 CI-failure-during-evaluation driver (RED gate) |
| `c0cdc46a12b` | feat | feat(03-12): add dog_03.py DOG-03 CI-failure-during-evaluation driver (GREEN gate) |

## Verification

Plan's `<verify><automated>` block ran clean against the GREEN commit:

```
$ cd .github/merge-queue && PYTHONPATH=src python3 -c "from rocm_mq.dogfood import dog_03; \
    assert dog_03.SCENARIO_ID == 'dog_03'; \
    assert dog_03.TIMEOUT_S == 1200; print('OK')"
OK
```

Unit suite for this plan (11 tests, all green):

```
$ PYTHONPATH=src python3 -m pytest tests/test_dogfood_dog_03.py -v
... 11 passed in 0.30s
```

Targeted regression on every test file that touches the dogfood scaffolding, the path-to-queues loader, the pure-layer boundary, and the existing DOG-* drivers (78 tests, all green):

```
$ PYTHONPATH=src python3 -m pytest tests/test_dogfood_dog_02.py \
    tests/test_dogfood_dog_04.py tests/test_dogfood_dog_08.py \
    tests/test_dogfood_base.py tests/test_path_to_queues_yaml.py \
    tests/test_pure_layer_imports.py tests/test_config.py -q
78 passed in 0.42s
```

Full repository suite (470 tests + 8 snapshots, all green):

```
$ PYTHONPATH=src python3 -m pytest -q
470 passed in 55.72s
```

No deltas in any other test file; this plan's diff is strictly additive within the `rocm_mq.dogfood` subpackage + its test sibling.

## Canary check-name discovery — design note

The single non-obvious design choice in this driver is that the canary check-name string the eject-reason substring match targets is NOT a module-level constant. Instead, `run_scenario` calls `config.load_from_develop(client, owner, repo)` at the top of every run, extracts `required_checks.dogfood-canary[0]` from the parsed YAML, and uses that string to build EXPECTED at runtime. The motivation is plan 03-09's `## Outstanding Live Verification` block, which documents that the literal GHA-registered check-run name may differ from the predicted `mq-dogfood-canary / canary` once the canary first fires against a real PR head SHA — and any re-pin must propagate to the driver without a code change (because `path_to_queues.yml` is itself on `SELF_BOOTSTRAP_PATHS` per plan 03-02, and `dog_03.py` is too via `.github/merge-queue/**`, so a coupled-edit PR could not be auto-merged anyway; the driver-as-pure-consumer-of-the-YAML pattern keeps the YAML re-pin as a single-file maintainer merge).

The substring match further slices on " / " to keep the load-bearing prefix (`mq-dogfood-canary`) only — the trailing job-id portion is where the most plausible drift would land (e.g., GHA changes the slash separator, the workflow's `jobs.canary` key gets renamed, etc.), and matching the prefix tolerates that drift while still being specific enough to distinguish DOG-03's eject from any other eject reason (merge conflict, approval revoked, etc.).

This pattern is unit-tested by `test_expected_outcome_derived_from_yaml_payload`, which seeds the FakeGitHub with a custom `renamed-canary / check` pin and asserts both EXPECTED.reason_substring AND the eject-reason match flow through to that custom name.

## Task 2 deferral (live-fork verification)

Plan 03-12 Task 2 is a `checkpoint:human-verify` requiring the operator to run `python -m rocm_mq.dogfood.dog_03 --owner SamuelReeder --repo rocm-libraries` against the live fork and observe in the Actions tab that:

1. The `mq-dogfood-canary` workflow fires on the new dogfood/ PR and concludes `failure` (title marker `[dogfood-ci-fail]` matched).
2. Within ~6 min the `mq-processor` cron fires, observes the failed required check on the PR head SHA, and ejects the PR with a status comment naming the canary check.
3. The driver exits 0 and writes a JSON file to `dogfood-runs/` with `passed=true`.

The executor's objective for this plan explicitly says **"do NOT actually run live against the fork in this plan execution — unit-tests only"**, and this plan's deferral mirrors plan 03-09's earlier deferral of the analogous Task 2 checkpoint (same blocking-gate shape; same operator-prerequisite shape; same not-blocking-plan-completion outcome). The structural correctness of the driver is established by:

- The 11-test unit suite (all green) which exercises the orchestration body, the YAML-derived expected-outcome construction, the eject-predicate matching, the D-04 schema emission, the timeline vocabulary, and a real-file sanity guard against `path_to_queues.yml` drifting away from the `mq-dogfood-canary` prefix.
- The plan's automated `<verify><automated>` block (module imports cleanly; constants match).
- The targeted regression check (78 related tests pass) and the full repo suite (470 tests pass).

The live verification belongs to the post-deployment phase (after plans 03-05's App registration + the workflow deployments land in develop) and will produce an addendum to this SUMMARY when it runs (analogous to plan 03-09's "Outstanding Live Verification" block) — or, if the live-captured check-name differs from the YAML pin, a separate one-line follow-up commit to `path_to_queues.yml` per RFC §8's manual-maintainer-merge discipline.

## Deviations from Plan

**Plan executed exactly as written** with one expansion noted under the auto-fix scope rule:

**1. [Rule 2 - Auto-add missing critical functionality] Added the real-file sanity test**

- **Found during:** Task 1 RED gate authoring
- **Issue:** The plan's `<behavior>` block calls for runtime config discovery but does not require a unit test against the REAL `path_to_queues.yml` in the repo. Without that guard, a future re-pin of `required_checks.dogfood-canary` that drops the `mq-dogfood-canary` prefix would silently break this driver's eject-reason match in production, and the unit suite (which uses synthetic YAML) would not catch it.
- **Fix:** Added `test_real_path_to_queues_yaml_pins_canary_check` which reads the actual `.github/merge-queue/path_to_queues.yml` from disk and asserts that the `required_checks.dogfood-canary` list contains at least one entry with the `mq-dogfood-canary` substring. This is the same shape as `tests/test_dogfood_dog_08.py::test_pr_file_path_is_not_in_path_to_queues` (which already guards DOG-08's chosen PR path against future YAML evolution); applying the same pattern here keeps the dogfood test suite uniformly defended.
- **Files modified:** `.github/merge-queue/tests/test_dogfood_dog_03.py` (one additional test)
- **Commit:** folded into `f69a29f7475` (RED gate); the test was authored at RED time and passes against the production YAML at GREEN time.

**2. [Rule 2 - Auto-add missing critical functionality] Added the D-04 schema completeness test**

- **Found during:** Task 1 RED gate authoring
- **Issue:** The dog_02 / dog_04 / dog_08 unit suites do NOT explicitly enumerate every D-04 schema field present in their emitted JSON; they spot-check `passed` + `scenario_id` + a handful of observation fields. Adding the explicit enumeration keeps the D-04 contract testable per-driver (so a future refactor that drops a field from one driver's emitted JSON gets caught locally).
- **Fix:** Added `test_run_scenario_emits_json_with_d04_schema_fields` which enumerates all 12 D-04 fields. Pure additive; does not modify the other drivers' tests.
- **Files modified:** `.github/merge-queue/tests/test_dogfood_dog_03.py` (one additional test)
- **Commit:** folded into `f69a29f7475` (RED gate).

**Otherwise:** No deviation from the planned module structure, function signatures, polling shape, JSON schema, or timeline vocabulary.

## Known Stubs

None within this plan's diff. The driver's match-substring depends on `path_to_queues.yml required_checks.dogfood-canary` being correctly pinned — that pin is itself a Known Stub from plan 03-03 (the predicted `mq-dogfood-canary / canary` literal, pending plan 03-09's live verification), but this is upstream context, not introduced by this plan.

## Threat Flags

None — this plan's diff stays within the trust boundary surface declared in the `<threat_model>` block. All three STRIDE items are accepted or mitigated:

| Threat ID | Mitigation status |
|-----------|-------------------|
| T-03-12-01 (Tampering — title-marker spoofing on a non-driver PR) | accepted (same canary design intent: any dogfood/** PR with the marker triggers the canary failure; that is the deliberate test surface) |
| T-03-12-02 (DoS — 20min poll budget) | mitigated (TIMEOUT_S explicit; Phase 5 cleanup deferred per CONTEXT.md) |
| T-03-12-03 (Information disclosure — canary check name in eject reason) | accepted (check name is public information) |

## Self-Check: PASSED

- `.github/merge-queue/src/rocm_mq/dogfood/dog_03.py` → FOUND
- `.github/merge-queue/tests/test_dogfood_dog_03.py` → FOUND
- Commit `f69a29f7475` → FOUND in `git log` (RED gate)
- Commit `c0cdc46a12b` → FOUND in `git log` (GREEN gate)
- Plan `<verify><automated>` Python one-liner: SCENARIO_ID + TIMEOUT_S assertions PASS ✓
- New unit suite: 11/11 PASS ✓
- Targeted regression (dog_02 + dog_04 + dog_08 + base + path_to_queues + pure_layer + config): 78/78 PASS ✓
- Full repo regression: 470/470 PASS ✓ (8 snapshots also passed)
- TITLE_PREFIX contains the canary trigger substring `[dogfood-ci-fail]` ✓
- EXPECTED built at runtime from path_to_queues.yml (no hardcoded canary string in dog_03.py) ✓ (verified by `grep -c "mq-dogfood-canary" src/rocm_mq/dogfood/dog_03.py` returning 0 in the executable code paths — references appear only in module docstring + helper docstrings as design rationale)

## TDD Gate Compliance

- RED gate commit `f69a29f7475` is a `test(03-12): ...` commit (11 failing tests at collection time — `from rocm_mq.dogfood import dog_03` raises ImportError because the module did not yet exist).
- GREEN gate commit `c0cdc46a12b` is a `feat(03-12): ...` commit; all 11 tests pass against the new module.
- No REFACTOR commit needed — the GREEN commit is the minimal implementation that turns the suite green; no cleanup pass was warranted after.
- Gate sequence: RED (`test`) → GREEN (`feat`) ✓.
