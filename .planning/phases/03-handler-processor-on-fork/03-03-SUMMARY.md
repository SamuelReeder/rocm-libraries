---
phase: 03-handler-processor-on-fork
plan: 03
subsystem: config
tags: [path-to-queues, rfc-4.1, rfc-4.8, self-bootstrap, dogfood-canary]
dependency-graph:
  requires:
    - "rocm_mq.config.SELF_BOOTSTRAP_PATHS (plan 03-02 — already includes .github/merge-queue/path_to_queues.yml)"
    - "rocm_mq.config.load_from_develop (plan 03-02 — Contents-API + safe_load consumer)"
  provides:
    - ".github/merge-queue/path_to_queues.yml — the hand-written routing config the handler / processor read on every cycle"
    - ".github/merge-queue/tests/test_path_to_queues_yaml.py — parse-level tripwire suite (8 tests)"
  affects:
    - "Plan 03-06 cmd_handle.py (will intersect PR changed files with this file's `paths:` entries)"
    - "Plan 03-08 mq-processor.yml (will discover queue depth via load_from_develop reading this file)"
    - "Plan 03-09 mq-dogfood-canary.yml (the canary supplies the required-check for the `dogfood-canary` queue)"
    - "Plan 03-12 DOG-03 dogfood driver (creates PR under dogfood/** so the canary required-check is what evicts it)"
    - "Plan 04-XX mq-config-validate.yml (will add graph-closure schema validation on top of this hand-written file)"
    - "Phase 5 porting-prep (will strip the `dogfood-canary` queue entry + `dogfood/` path + `dogfood-canary` required_checks for the upstream-bound copy)"
tech-stack:
  added: []                                   # no new deps — uses already-installed PyYAML 6.0.3
  patterns:
    - "Hand-written YAML config loaded via Contents API at ref=develop (RFC §4.8)"
    - "Synthetic-test-fixture-as-config-entry (dogfood-canary queue) with porting-strip note inline in the YAML banner"
key-files:
  created:
    - ".github/merge-queue/path_to_queues.yml"
    - ".github/merge-queue/tests/test_path_to_queues_yaml.py"
  modified: []
decisions:
  - "PRE-CONFIRM: option-a — keep RESEARCH.md Area #13 default queue set + default path prefixes + title-substring marker `[dogfood-ci-fail]`. Justified by Task 2 live verification confirming the fork's `develop` layout matches RFC §4.1 defaults exactly (no path-prefix divergence)."
  - "dogfood-canary's required-check string is the placeholder `mq-dogfood-canary / canary`; the actual GHA-registered context string is verified and re-pinned after plan 03-09's first canary PR runs."
metrics:
  duration: "~9 minutes (live verify + RED + GREEN + SUMMARY)"
  completed: 2026-05-19
---

# Phase 3 Plan 03: path_to_queues.yml + smoke tests — Summary

Authored the hand-written `path_to_queues.yml` config the handler + processor will read from `develop` on every cycle, plus an eight-test parse-level tripwire suite. Live-verified the fork's `develop` directory layout and the existing `TheRock CI Summary` required-check context name against fork PR #21 before pinning the YAML literals; no deviation from RESEARCH.md Area #13 defaults was needed.

## What Shipped

- **`.github/merge-queue/path_to_queues.yml`** (new) — six production queues (`hipdnn`, `miopen-provider`, `hipblaslt-provider`, `hip-kernel-provider`, `fusilli-provider`, `integration-tests`) + one synthetic `dogfood-canary` queue (Phase-5-strip-marked inline). `paths:` realizes RFC §4.1 + §4.2 routing (hipDNN core blocks all five providers and integration-tests; each provider blocks only its own queue; integration-tests blocks all four providers and itself; `dogfood/` routes only to `dogfood-canary`). `required_checks:` pins `TheRock CI Summary` for every production queue (verified live) and a placeholder `mq-dogfood-canary / canary` for the canary (to be re-verified after plan 03-09).
- **`.github/merge-queue/tests/test_path_to_queues_yaml.py`** (new) — eight tests covering parse success, top-level shape, queues-list length + types, dogfood-canary presence, `required_checks` ↔ `queues` set-equality, paths-entries-reference-only-known-queues, dogfood-path-routes-to-canary-only, and the six-production-queues-present subset assertion. No schema validation (that's Phase 4's `mq-config-validate.yml`).

## Commits

| Hash | Kind | Subject |
|------|------|---------|
| `5a29a11a35c` | test | test(03-03): add failing path_to_queues.yml smoke + structural tests (RED gate) |
| `677b7c9915d` | feat | feat(03-03): add path_to_queues.yml with six production queues + dogfood-canary (GREEN gate) |

## Live Verification Log (Task 2)

All three verification items were captured BEFORE the YAML was pinned. Outputs reproduced here verbatim so a future reviewer can replay them without re-running the commands.

### Top-level path prefixes (live)

**Command:**
```bash
git fetch fork develop
git ls-tree -d fork/develop --name-only
```
**Output (verbatim):**
```
.dvc
.github
cmake
dnn-providers
docs
projects
shared
test
```
**Verdict:** Both `projects/` and `dnn-providers/` exist exactly as RFC §4.1 expects. No prefix divergence.

### Per-queue subdirectory existence (live)

**Commands:**
```bash
git ls-tree -d fork/develop:projects --name-only
git ls-tree -d fork/develop:dnn-providers --name-only
```
**Output (verbatim):**
```
# projects/
composablekernel
hipblas-common
hipblas
hipblaslt
hipcub
hipdnn
hipfft
hiprand
hipsolver
hipsparse
hipsparselt
hiptensor
miopen
rocblas
rocfft
rocprim
rocrand
rocsolver
rocsparse
rocthrust
rocwmma

# dnn-providers/
cmake
fusilli-provider
hip-kernel-provider
hipblaslt-provider
integration-tests
miopen-provider
```
**Verdict:**
- `projects/hipdnn/` exists ✓ (the hipDNN core queue's path).
- `dnn-providers/{miopen-provider, hipblaslt-provider, hip-kernel-provider, fusilli-provider, integration-tests}/` all exist ✓ (the five remaining production-queue paths). All six RFC §4.1 path prefixes confirmed valid on the fork.

### Required-check context name (live)

**Commands:**
```bash
# 1. Find a recent PR with check-runs (develop tip has no PR-only check-runs).
gh api 'repos/SamuelReeder/rocm-libraries/pulls?state=all&per_page=5' \
  --jq '.[] | {number, head_sha: .head.sha, title}'
# → PR #21 head_sha = 04070ca96b77e75d180f09dd673e4c343260a8c6

# 2. Read check-runs for that head SHA.
gh api repos/SamuelReeder/rocm-libraries/commits/04070ca96b77e75d180f09dd673e4c343260a8c6/check-runs \
  --jq '.check_runs[] | {name, app: .app.slug, conclusion}'
```
**Output (verbatim, first row only — the rest are matrix-job + ancillary entries):**
```
{"app":"github-actions","conclusion":"failure","name":"TheRock CI Summary"}
{"app":"github-actions","conclusion":"skipped","name":"Linux (hipdnn_install,miopenprovider,hipdnn-samples,hipdnn,hipblasltprovider | gfx94X-dcgpu) / Test (gfx94X-dcgpu)"}
{"app":"github-actions","conclusion":"skipped","name":"Linux (hipdnn_install,miopenprovider,hipdnn-samples,hipdnn,hipblasltprovider | gfx950-dcgpu) / Test (gfx950-dcgpu)"}
{"app":"github-actions","conclusion":"skipped","name":"Windows (hipblasltprovider,hipdnn_install,miopenprovider,hipdnn-samples,hipdnn | gfx1151) / Test (gfx1151)"}
{"app":"mergify","conclusion":"neutral","name":"Mergify Merge Queue"}
{"app":"github-actions","conclusion":"cancelled","name":"Linux (hipdnn_install,miopenprovider,hipdnn-samples,hipdnn,hipblasltprovider | gfx950-dcgpu) / Build (gfx950-dcgpu)"}
{"app":"github-actions","conclusion":"cancelled","name":"Linux (hipdnn_install,miopenprovider,hipdnn-samples,hipdnn,hipblasltprovider | gfx94X-dcgpu) / Build (gfx94X-dcgpu)"}
{"app":"github-actions","conclusion":"cancelled","name":"Windows (hipblasltprovider,hipdnn_install,miopenprovider,hipdnn-samples,hipdnn | gfx1151) / Build (gfx1151)"}
{"app":"github-actions","conclusion":"success","name":"Setup"}
{"app":"github-actions","conclusion":"failure","name":"pre-commit"}
{"app":"github-actions","conclusion":"success","name":"scopes"}
{"app":"github-actions","conclusion":"skipped","name":"eject-on-push"}
{"app":"github-actions","conclusion":"success","name":"labeler"}
```
**Cross-check (file vs registered):**
```bash
gh api repos/SamuelReeder/rocm-libraries/contents/.github/workflows/therock-ci.yml?ref=develop \
  --jq '.content' | base64 -d | sed -n '170,172p'
```
Returns:
```
  therock_ci_summary:
    name: TheRock CI Summary
    if: always()
```
**Verdict:** The exact string `TheRock CI Summary` is the registered context name on the head SHA of an active fork PR, matching `.github/workflows/therock-ci.yml` line 171 byte-for-byte. The literal is safe to pin in `required_checks:` for all six production queues. (Note: the matrix-job rows above — `Linux (... | gfx...) / Test (...)` — are sub-jobs of `TheRock CI Summary` and are not separately required.)

## Decisions Made

- **Pre-confirm option-a (default queue set + default path prefixes + title-substring canary marker `[dogfood-ci-fail]`)**: live verification matched RESEARCH.md Area #13 defaults exactly, so the safe-by-construction default is also the live-correct default. Option-b (customize after live verify) collapses to option-a here; option-c (label marker `dogfood:ci-fail`) was not selected — the canary workflow body in plan 03-09 remains the title-substring shape per 03-PATTERNS.md.
- **Canary required-check is a placeholder** (`mq-dogfood-canary / canary`). The actual context string GitHub registers against a head SHA depends on the canary workflow's `jobs.<id>.name:` field, which is authored in plan 03-09. After plan 03-09's first canary PR runs, the literal MUST be re-verified via the same `gh api .../check-runs` invocation as above and updated here.

## Deviations from Plan

None — plan executed as written, with Task 2's live verification batched into the same agent run as Task 1's PRE-CONFIRM so the user could see actual fork data before locking the gray-area choice.

## Verification

- `python3 -c "import yaml; d = yaml.safe_load(open('.github/merge-queue/path_to_queues.yml')); assert 'dogfood-canary' in d['queues']; assert set(d['required_checks'].keys()) == set(d['queues'])"` → exit 0 ✓
- `cd .github/merge-queue && pytest tests/test_path_to_queues_yaml.py -v` → 8 passed ✓
- Full suite `pytest -q` → 345 passed, no regressions ✓

## Known Stubs

- **`required_checks.dogfood-canary` = `["mq-dogfood-canary / canary"]`** — placeholder. Marked in the YAML banner + above. Will be re-verified live and pinned during/after plan 03-09 (mq-dogfood-canary.yml + first canary PR).

## Threat Flags

None — no new trust-boundary surface introduced beyond what plan 03-02 already declared. The file's tamper resistance is delegated to the SELF_BOOTSTRAP_PATHS check in plan 03-06's `cmd_handle.py` (already registered in plan 03-02) and the future Phase 4 graph-closure validator (`mq-config-validate.yml`).

## Self-Check: PASSED

- `.github/merge-queue/path_to_queues.yml` → FOUND
- `.github/merge-queue/tests/test_path_to_queues_yaml.py` → FOUND
- Commit `5a29a11a35c` → FOUND in `git log` (RED gate)
- Commit `677b7c9915d` → FOUND in `git log` (GREEN gate)
- Smoke suite 8/8 PASS; full suite 345/345 PASS
- Live-verification log captured (top-level prefixes, per-queue subdirs, required-check context name)
