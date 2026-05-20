---
plan_id: 03-02
phase: 03-handler-processor-on-fork
plan: 02
subsystem: config
tags: [self-bootstrap-protection, path-to-queues-loader, app-identity-env, yaml-safe-load, rfc-section-8]

status: complete
completed_at: "2026-05-19T00:00:00Z"

# Dependency graph
requires:
  - phase: 02-i-o-layer-executor
    plan: 03
    provides: "rocm_mq.gh.GitHubClient — runtime parameter type for load_from_develop; consumed via TYPE_CHECKING guard to avoid runtime import-cycle risk"
  - phase: 02-i-o-layer-executor
    plan: 02
    provides: "tests/gh_fake.FakeGitHub — base fake the new test_config.py extends with a get_content seed helper (the base fake does not model the Contents API; per-test extension is the established pattern)"
provides:
  - "rocm_mq.config.SELF_BOOTSTRAP_PATHS — Final[tuple[str, ...]] containing the four RFC §8 self-bootstrap-protected globs (today: .github/workflows/**, .github/merge-queue/**, .github/merge-queue/path_to_queues.yml, .github/workflows/mq-dogfood-canary.yml) plus a documented future-slot comment for terraform/github/** branch-protection-as-code per CLAUDE.md"
  - "rocm_mq.config.load_from_develop(client, owner, repo) -> dict[str, Any] — non-validating loader that reads .github/merge-queue/path_to_queues.yml from the develop ref via the Contents API, base64-decodes, and parses with yaml.safe_load. No schema validation (Phase 4 territory)."
  - "rocm_mq.config.APP_SLUG_ENV: Final[str] = 'MQ_APP_SLUG' — env-var NAME constant for plan 03-06's handler"
  - "rocm_mq.config.APP_ID_ENV: Final[str] = 'MQ_APP_ID' — env-var NAME constant for plan 03-06's handler"
affects: [03-04, 03-06, 04-XX]   # preflight + handler + Phase 4 config validator all consume this module

# Tech tracking
tech-stack:
  added:
    - "types-PyYAML>=6.0 (dev) — mypy needs the yaml stub package now that config.py is the first src/ module to import yaml; without it mypy fails with [import-untyped]. Added to the [project.optional-dependencies].dev table."
  patterns:
    - "Module-level Final[tuple[str, ...]] constant with inline comment documenting a future slot — mirrors executor.py's module-level constants block style; supersedes a Python-list literal so callers can use the constant as a hashable key (Pitfall 11 alignment with frozen-tuple discipline)"
    - "Loader returns dict[str, Any] explicitly typed at the assignment site (parsed: dict[str, Any] = yaml.safe_load(raw); return parsed) — silences mypy's [no-any-return] without requiring a runtime cast() call; the documented contract (callers validate further) makes Any acceptable here"
    - "TYPE_CHECKING guard for GitHubClient import — config.py is I/O layer, gh.py imports config nowhere today, but guarding the type-only import keeps the future-proofing identical to executor.py's pattern"
    - "FakeGitHub extension via per-test monkey-patch (fake.rest.repos.get_content = ...) instead of widening the base fake — established pattern in the suite per the existing repos.compare_commits seeded-state approach"

key-files:
  created:
    - ".github/merge-queue/src/rocm_mq/config.py"
    - ".github/merge-queue/tests/test_config.py"
    - ".planning/phases/03-handler-processor-on-fork/03-02-SUMMARY.md"
  modified:
    - ".github/merge-queue/pyproject.toml"

key-decisions:
  - "App slug literal is NOT hardcoded in config.py — only the env-var-NAME constant (APP_SLUG_ENV = 'MQ_APP_SLUG') is exported. The recommended slug literal ('rocm-mq-fork' per 03-RESEARCH.md Area #1) lives in plan 03-05's App-registration manual task and is supplied to workflows via the mq-secrets Environment / repo variables. Locking the literal here would couple the App registration step to a code change."
  - "load_from_develop returns dict[str, Any] (not a more constrained shape) — Phase 4 owns schema validation; this loader is intentionally non-validating per CONTEXT.md <decisions> 'PATH_TO_QUEUES bootstrap'. The dict[str, Any] annotation honors the contract (callers in plans 03-04 / 03-06 do their own structural checks) without forcing a premature schema."
  - "Plan was 2 tasks both tagged tdd='true' but they describe the SAME work from two angles (Task 1 'create config.py'; Task 2 'add tests for config.py'). Executed as one combined RED → GREEN cycle: RED commit (e05df2c9460) contains all 11 tests; GREEN commit (c77d67edc1b) contains config.py + pyproject.toml types-PyYAML dep + ruff-fix on test imports. Rule 3 — plan redundancy, same as the established Phase 3 pattern documented in 03-01-SUMMARY's Deviation 2."
  - "Test count: 11 (plan asked for 8). Extra 3 come from parametrizing test_self_bootstrap_paths_contains_required_globs over the four required globs — pytest reports each parametrize id as a separate test. The eight logical behaviors in the plan's <behavior> block are all covered."
  - "Added types-PyYAML>=6.0 to dev deps — Rule 2 (correctness): mypy was previously clean because no src/ module imported yaml; config.py is the first. Without the stub package, mypy --strict on the decision layer is unaffected but the non-strict src/ pass fails with [import-untyped]. Pinning the stub at >=6.0 matches the runtime pin (PyYAML>=6.0.3) per CLAUDE.md version-compatibility discipline."

requirements-completed: [WF-08]   # SELF_BOOTSTRAP_PATHS — handler's self-bootstrap protection constant per RFC §8

# Metrics
duration: ~12min
completed: 2026-05-19
tests_added: 11
tests_total_before: 326
tests_total_after: 337
---

# Phase 03 Plan 02: rocm_mq.config (SELF_BOOTSTRAP_PATHS + load_from_develop) Summary

**One-liner:** Creates `rocm_mq.config` with two public surfaces required by Phase 3 plans 03-04 (preflight) and 03-06 (cmd_handle): (1) `SELF_BOOTSTRAP_PATHS` — the RFC §8 self-bootstrap-protected globs the handler intersects against PR changed files to reject `/merge` on infrastructure-touching PRs; (2) `load_from_develop(client, owner, repo)` — the non-validating Contents API + `yaml.safe_load` loader for `.github/merge-queue/path_to_queues.yml` from the develop ref. Plus two `Final[str]` env-var-NAME constants (`APP_SLUG_ENV`, `APP_ID_ENV`) for plan 03-06's App identity pinning.

## What this plan delivered

**`SELF_BOOTSTRAP_PATHS`** is a module-level `Final[tuple[str, ...]]` containing exactly the four globs CONTEXT.md `<code_context>` Established Patterns enumerates for Phase 3:

```python
SELF_BOOTSTRAP_PATHS: Final[tuple[str, ...]] = (
    ".github/workflows/**",
    ".github/merge-queue/**",
    ".github/merge-queue/path_to_queues.yml",      # explicit even though covered by **
    ".github/workflows/mq-dogfood-canary.yml",     # explicit
    # ---- Future slot: "terraform/github/**" — branch-protection-as-code (RFC §8) ----
    # (long-form comment documenting the IaC-tool slot per CLAUDE.md guidance)
)
```

The two `**`-covered explicit entries (`path_to_queues.yml` and `mq-dogfood-canary.yml`) are kept visible at this layer so a future refactor that narrows the `**` globs (e.g., to a more specific subtree) doesn't silently drop them from the protection list. The future-slot comment documents the `terraform/github/**` (or equivalent IaC) extension point per CLAUDE.md "branch-protection-as-code integration (RFC §8)" so the next person to add Terraform / Probot-Settings / Rulesets knows where to extend the constant.

**`load_from_develop`** is a 10-line function that hits `GET /repos/{owner}/{repo}/contents/.github/merge-queue/path_to_queues.yml?ref=develop` via the supplied client, base64-decodes `resp.parsed_data.content`, and parses with `yaml.safe_load`. `safe_load` (not `load`) is non-negotiable per CLAUDE.md "What NOT to Use" + the plan's T-03-02-01 mitigation: it blocks `!!python/object` and similar code-execution constructs that would let a poisoned `path_to_queues.yml` trigger arbitrary Python inside the workflow runner (which holds the App installation token). No schema validation here — Phase 4's `mq-config-validate.yml` job owns that surface.

**`APP_SLUG_ENV = "MQ_APP_SLUG"`** and **`APP_ID_ENV = "MQ_APP_ID"`** are `Final[str]` constants pinning the env-var NAMES plan 03-06's handler will read at runtime. The slug LITERAL ('rocm-mq-fork' recommended) is registered manually in plan 03-05's App-registration task and supplied to the workflow via repo variables — this module only owns the NAMES so a future rename ripples through one source of truth.

## Task execution

| Task | Type | Outcome | Commit |
|---|---|---|---|
| 1 | auto, tdd=true (create config.py) | RED + GREEN both green; folded into the combined cycle per Deviation 1 below | RED: `e05df2c9460` / GREEN: `c77d67edc1b` |
| 2 | auto, tdd=true (create test_config.py) | All 11 tests pass; behaviour identical to plan's enumerated 8 (3 extra come from parametrize ids) | (same as Task 1) |

## Verification command output

```
$ cd .github/merge-queue && .venv/bin/python -m pytest tests/test_config.py -v 2>&1 | tail -15
tests/test_config.py::test_self_bootstrap_paths_is_immutable_tuple PASSED [  9%]
tests/test_config.py::test_self_bootstrap_paths_contains_required_globs[.github/workflows/**] PASSED [ 18%]
tests/test_config.py::test_self_bootstrap_paths_contains_required_globs[.github/merge-queue/**] PASSED [ 27%]
tests/test_config.py::test_self_bootstrap_paths_contains_required_globs[.github/merge-queue/path_to_queues.yml] PASSED [ 36%]
tests/test_config.py::test_self_bootstrap_paths_contains_required_globs[.github/workflows/mq-dogfood-canary.yml] PASSED [ 45%]
tests/test_config.py::test_self_bootstrap_paths_documents_future_slot PASSED [ 54%]
tests/test_config.py::test_load_from_develop_returns_dict PASSED         [ 63%]
tests/test_config.py::test_load_from_develop_uses_develop_ref PASSED     [ 72%]
tests/test_config.py::test_load_from_develop_rejects_unsafe_yaml_tags PASSED [ 81%]
tests/test_config.py::test_app_slug_env_constant PASSED                  [ 90%]
tests/test_config.py::test_app_id_env_constant PASSED                    [100%]
============================== 11 passed in 0.25s ==============================

$ .venv/bin/python -m pytest -q | tail -3
337 passed in 52.06s

$ .venv/bin/python -m ruff check .
All checks passed!

$ .venv/bin/python -m mypy src/rocm_mq/ tests/gh_fake.py
Success: no issues found in 14 source files

$ .venv/bin/python -c "from rocm_mq.config import SELF_BOOTSTRAP_PATHS, load_from_develop, APP_SLUG_ENV, APP_ID_ENV; assert len(SELF_BOOTSTRAP_PATHS) == 4"
# exit 0

$ grep -c "terraform" .github/merge-queue/src/rocm_mq/config.py
1
```

All three plan `<verification>` block commands return the expected shapes (pytest 0 / `len(SELF_BOOTSTRAP_PATHS) == 4` / `grep -c terraform >= 1`). The `<success_criteria>` block is satisfied: `config.py` is importable; `SELF_BOOTSTRAP_PATHS` contains the four globs; `load_from_develop` reads from develop ref via Contents API + safe_load; `APP_SLUG_ENV` / `APP_ID_ENV` constants exposed; tests cover all of the above plus safe_load enforcement against `!!python/object` injection.

## Deviations from Plan

### 1. [Rule 3 — Plan redundancy] Tasks 1 and 2 collapsed into one RED → GREEN cycle

**Found during:** Reading the plan's Task 1 and Task 2 `<behavior>` blocks.

**Issue:** Plan 03-02 declares Task 1 (create `config.py`) and Task 2 (create `test_config.py`) both with `tdd="true"`. They describe the same delivery from two angles — Task 1's `<behavior>` block enumerates exactly the same tests Task 2's `<behavior>` block lists. TDD requires the tests to be written BEFORE the implementation (the RED gate); treating Task 2 as a sequential follow-up would invert the gate sequence (Task 1 GREEN would have nothing to make pass without Task 2's tests).

**Fix:** Wrote all 11 tests under one RED commit (`e05df2c9460`); landed `config.py` + the supporting `pyproject.toml` change + ruff-fix on the test imports under one GREEN commit (`c77d67edc1b`). The gate sequence reads correctly in git log: `test(03-02): ...` precedes `feat(03-02): ...`. The done-criteria for both tasks are satisfied:
- Task 1 done: `config.py` exists with `SELF_BOOTSTRAP_PATHS` (4 entries), `load_from_develop`, `APP_SLUG_ENV`, `APP_ID_ENV`; `pytest tests/test_config.py` exits 0; the importability check exits 0.
- Task 2 done: `tests/test_config.py` covers all eight behaviors enumerated; all pass; file is ~170 LOC (slightly above the plan's ~80 LOC heuristic because the FakeGitHub get_content seed helper and parametrize fixture together add structural lines without changing logical coverage).

This is the same pattern documented in `03-01-SUMMARY.md` Deviation 2 ("Tasks 2 and 3 collapsed into one RED → GREEN cycle"); the executor template treats it as a Rule 3 plan-redundancy auto-fix without a user prompt.

### 2. [Rule 2 — Missing critical functionality] Added types-PyYAML to dev deps

**Found during:** Running `mypy src/rocm_mq/ tests/gh_fake.py` after the GREEN refactor.

**Issue:** `config.py` is the first `src/` module to import `yaml`. Without `types-PyYAML` installed, mypy reports `error: Library stubs not installed for "yaml" [import-untyped]`. This is a correctness requirement (CI keeps `mypy` clean; landing config.py without the stub would silently break the next mypy run).

**Fix:** Added `"types-PyYAML>=6.0"` to `pyproject.toml`'s `[project.optional-dependencies].dev` table and installed it into the venv. The pin matches the runtime `PyYAML>=6.0.3` per CLAUDE.md version-compatibility discipline.

**Files modified:** `.github/merge-queue/pyproject.toml` (folded into the GREEN commit `c77d67edc1b`).

### 3. [Rule 1 — Mypy correctness] Annotated yaml.safe_load result locally to silence no-any-return

**Found during:** Running `mypy src/rocm_mq/ tests/gh_fake.py` after installing types-PyYAML.

**Issue:** `yaml.safe_load(raw)` returns `Any`; the function signature declares `-> dict[str, Any]`. Mypy raises `error: Returning Any from function declared to return "dict[str, Any]" [no-any-return]`.

**Fix:** Assigned to a locally-typed variable before returning:
```python
parsed: dict[str, Any] = yaml.safe_load(raw)
return parsed
```
This silences `[no-any-return]` without a runtime `cast()` call. The documented contract — callers in plans 03-04 / 03-06 / 04-XX validate the dict shape further — makes the implicit narrowing acceptable here; the loader is intentionally non-validating per CONTEXT.md `<decisions>` "PATH_TO_QUEUES bootstrap".

**Files modified:** `.github/merge-queue/src/rocm_mq/config.py` (folded into the GREEN commit `c77d67edc1b`).

### 4. [Auto-fix] Ruff import-ordering on test_config.py

**Found during:** Running `ruff check .` on the RED commit.

**Issue:** The initial RED draft of `test_config.py` had a mixed import block (stdlib + third-party + local without isort grouping). Ruff's `I` rules flagged it.

**Fix:** Ran `ruff check --fix tests/test_config.py`; the autofix reorganized imports into stdlib / third-party / first-party / local groups per the existing test_*.py convention. No behavior change. Folded into the GREEN commit.

## Threat Flags

None new. The plan's threat register (T-03-02-01 / T-03-02-02 / T-03-02-03) is addressed by the implementation as specified:
- **T-03-02-01 (Tampering, path_to_queues.yml content) — MITIGATED:** `yaml.safe_load` (not `load`) is used; `test_load_from_develop_rejects_unsafe_yaml_tags` proves the `!!python/object` rejection path; load reads exclusively from the `develop` ref (hardcoded `_DEVELOP_REF` constant).
- **T-03-02-02 (Information Disclosure, App slug exposure) — ACCEPTED:** Confirmed — only env-var NAMES are exported; the slug literal stays out of source.
- **T-03-02-03 (Tampering, SELF_BOOTSTRAP_PATHS bypass) — MITIGATED:** `config.py` lives at `.github/merge-queue/src/rocm_mq/config.py` which is inside one of its own globs (`.github/merge-queue/**`), so modifications to this constant via a PR will trigger the handler's self-bootstrap rejection (defense in depth per CONTEXT.md).

No new security-relevant surface introduced beyond what the plan enumerated.

## Files modified

| File | Change |
|---|---|
| `.github/merge-queue/src/rocm_mq/config.py` | NEW. Module docstring + `SELF_BOOTSTRAP_PATHS` (4 entries + future-slot comment) + `APP_SLUG_ENV` / `APP_ID_ENV` `Final[str]` constants + `load_from_develop` (Contents API → base64 → yaml.safe_load). 142 lines including the long-form docstrings and future-slot comment. |
| `.github/merge-queue/tests/test_config.py` | NEW. 11 tests covering the eight plan behaviors (parametrize over the four required globs adds 3 extra ids). Uses a per-test `_seed_contents` helper to monkey-patch `fake.rest.repos.get_content` (the base FakeGitHub does not model the Contents API; per-test extension is the established pattern). |
| `.github/merge-queue/pyproject.toml` | Added `"types-PyYAML>=6.0"` to `[project.optional-dependencies].dev`. Pin matches runtime `PyYAML>=6.0.3` per CLAUDE.md version-compatibility discipline. |
| `.planning/phases/03-handler-processor-on-fork/03-02-SUMMARY.md` | This file. |

## Commits

| Type | Hash | Message |
|---|---|---|
| test | `e05df2c9460` | `test(03-02): add failing tests for rocm_mq.config (SELF_BOOTSTRAP_PATHS + load_from_develop)` |
| feat | `c77d67edc1b` | `feat(03-02): add rocm_mq.config (SELF_BOOTSTRAP_PATHS + load_from_develop)` |

TDD gate sequence verified in git log: `test(...)` (`e05df2c9460`) precedes `feat(...)` (`c77d67edc1b`).

## Closure assertion

Phase 3 plan-02 deliverables are complete. The `rocm_mq.config` module is importable, fully typed, fully tested. Downstream plans may now proceed:

- **Plan 03-04 (preflight):** consume `load_from_develop(client, owner, repo)` for Check 3 of the pre-flight (PATH_TO_QUEUES loadable from develop ref).
- **Plan 03-06 (cmd_handle):** import `SELF_BOOTSTRAP_PATHS` for the path-intersection rejection check; import `APP_SLUG_ENV` / `APP_ID_ENV` for the App identity pinning at runtime via `os.environ[APP_SLUG_ENV]`.
- **Plan 04-XX (config validator):** layer schema validation on top of `load_from_develop`'s dict output.

## Self-Check: PASSED

- `.github/merge-queue/src/rocm_mq/config.py` — FOUND (4 entries + future-slot comment + load_from_develop + APP_SLUG_ENV + APP_ID_ENV)
- `.github/merge-queue/tests/test_config.py` — FOUND (11 tests, all pass)
- `.github/merge-queue/pyproject.toml` — MODIFIED (types-PyYAML>=6.0 in dev deps)
- `.planning/phases/03-handler-processor-on-fork/03-02-SUMMARY.md` — FOUND (this file)
- Commit `e05df2c9460` — FOUND in git log (test RED commit)
- Commit `c77d67edc1b` — FOUND in git log (feat GREEN commit)
- `pytest tests/test_config.py -v` returns exit 0 (11/11 pass)
- Full suite (337 tests) green; ruff + mypy clean
- Verification commands match plan `<verification>` block expectations (importability, `len(SELF_BOOTSTRAP_PATHS) == 4`, `grep -c terraform >= 1`)
- TDD gate sequence verified in git log: `test(...)` precedes `feat(...)`
