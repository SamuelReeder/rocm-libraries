---
phase: "01"
plan: "05"
subsystem: tests
tags: [regression, ast-lint, rfc-s4-2, pure-09, worked-example, datetime-chokepoint]
dependency_graph:
  requires:
    - "01-01"  # state.py + _helpers.py (PRState, RequiredCheckResult, etc.)
    - "01-02"  # decision.py (decide_cycle — the function under test)
    - "01-03"  # conftest.py (canonical_merge_queue_config, utc helper)
  provides:
    - rfc-s4-2-worked-example-regression
    - pure-09-ast-lint
  affects:
    - phase-2  # any new pure-layer module must be added to PURE_LAYER_MODULES
tech_stack:
  added:
    - stdlib ast module (ast.parse + ast.walk, zero external deps)
    - "@pytest.mark.parametrize for 8-timestep RFC §4.2 regression"
  patterns:
    - "Pitfall 8 set-vs-list equality tolerance at disjoint-queue-set timesteps"
    - "Q2 resolution: per-PR not per-cycle-globally activation/evaluation separation"
    - "AST walker with BANNED_IMPORTS + STDLIB_ALLOWLIST + ROCM_MQ_ALLOWLIST"
    - "Q5 tzinfo= keyword check (detect naive datetime constructors only)"
key_files:
  created:
    - .github/merge-queue/tests/test_worked_example.py
    - .github/merge-queue/tests/test_pure_layer_imports.py
  modified: []
decisions:
  - "Q2 resolution honored in T1 test: [Squash(A)] only; B/C activate at T2 (per-PR semantics)"
  - "Pitfall 8 set equality used at T2+T3 (disjoint queue sets); list equality everywhere else"
  - "PURE-09 walker adds __future__ to STDLIB_ALLOWLIST (all modules use from __future__ import annotations)"
  - "Q5 walker bans datetime() WITHOUT tzinfo= keyword only (not all datetime calls); test_state_dataclasses.py excluded (intentionally tests naive datetime rejection)"
  - "Wave 4 parallelism preserved: test_worked_example.py imports canonical_merge_queue_config from tests.conftest only, zero references to tests._strategies"
metrics:
  duration: "~30 minutes"
  completed: "2026-05-19T00:33:39Z"
  tasks_completed: 2
  files_created: 2
  files_modified: 0
  tests_added: 23
  total_tests_after: 217
---

# Phase 01 Plan 05: RFC §4.2 Worked-Example + PURE-09 AST Lint Summary

RFC §4.2 5-PR/8-timestep worked-example regression test plus PURE-09 AST walker
enforcing zero I/O imports, datetime chokepoint, and Q5 test-file naive-datetime ban.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | RFC §4.2 worked-example regression (PURE-08) | 0dc018a56b2 | test_worked_example.py |
| 2 | PURE-09 AST lint + Q5 test-file datetime ban | cbabfead799 | test_pure_layer_imports.py |

## What Was Built

### Task 1: RFC §4.2 Worked-Example Regression (`test_worked_example.py`)

**8-timestep parametrized test** (T0–T7, one per RFC §4.2 timestep transition):

| Timestep | t | Snapshot inputs | Expected actions | Equality |
|----------|---|-----------------|------------------|----------|
| T0 | 10:00 | A, B, C, D (all queued) | `[Activate(A)]` | list |
| T1 | 10:03 | A (active), B, C, D, E | `[Squash(A)]` | list |
| T2 | 10:06 | B, C, D, E | `{Activate(B), Activate(C)}` | set |
| T3 | 10:09 | B (active), C (active), D, E | `{Squash(B), Squash(C)}` | set |
| T4 | 10:12 | D, E | `[Activate(D)]` | list |
| T5 | 10:15 | D (active), E | `[Squash(D)]` | list |
| T6 | 10:18 | E | `[Activate(E)]` | list |
| T7 | 10:21 | E (active) | `[Squash(E)]` | list |

**Q2 resolution** (RESEARCH.md Open Question 2): T1 emits `[Squash(A)]` only.
The "activation and evaluation never in same cycle" rule is per-PR. At T1, B and C
are blocked behind A (which is still in the snapshot as mq:active). After A is
squashed and removed by the executor, B and C become heads of their disjoint
queues at T2.

**Pitfall 8** (set-vs-list equality): T2 and T3 use `set(actions) == expected`
because B and C are in disjoint queue sets (miopen-provider and hipblaslt-provider
respectively) and the RFC specifies ordering across disjoint sets is unspecified.

**`_make_pr` helper** uses `queues_for_paths` from `rocm_mq.pathmap` — implicitly
regression-testing the pathmap function against the RFC §4.2 worked-example paths.

**`_active(pr)` helper** models the post-Activate next-cycle state:
- Removes `mq:queued` from labels, adds `mq:active`
- Sets `is_validly_active=True`
- Sets required check to `"success"` (so step 5b dispatches Squash)

**Bonus tests**:
- `test_worked_example_full_sequence`: end-to-end integration test walks all 8 cycles
- `test_pitfall8_t2_set_equality_rationale`: documents WHY set equality is used at T2

**Import independence**: zero references to `tests._strategies` (Plan 04 territory).
`canonical_merge_queue_config` imported from `tests.conftest` only (Plan 03 single home).

### Task 2: PURE-09 AST Lint (`test_pure_layer_imports.py`)

**Constants:**
- `PURE_LAYER_MODULES = ("_helpers", "state", "pathmap", "decision", "comment", "summary")`
- `STDLIB_ALLOWLIST` — 12 stdlib modules + `__future__` (for `from __future__ import annotations`)
- `ROCM_MQ_ALLOWLIST` — 6 intra-rocm_mq modules
- `BANNED_IMPORTS` — 14 I/O and forbidden modules

**Test 1 (6 cases)**: `test_pure_layer_no_io_imports` — walks every `Import`/`ImportFrom`
node per pure-layer module. Banned imports produce `"banned import: <name> in rocm_mq.<module>"`.
Non-allowlisted imports produce `"non-allowlisted import: <name> ..."`.

**Test 2 (5 cases, excludes `_helpers`)**: `test_pure_layer_no_naive_datetime_constructors` —
walks `ast.Attribute` nodes; flags `datetime.now`, `datetime.utcnow`, `datetime.fromisoformat`
outside `_helpers.py` (Pitfall 3 chokepoint enforcement).

**Test 3 (Q5 resolution)**: `test_test_files_no_naive_datetime_constructor` — walks all
`test_*.py` files and bans `datetime(...)` calls WITHOUT a `tzinfo=` keyword argument.
Only flags demonstrably naive constructors. Exclusions:
- `conftest.py` — defines `utc()` helper
- `_strategies.py`, `_state_machine_base.py` — Hypothesis scaffolding
- `test_pure_layer_imports.py` — the lint itself (avoid recursion)
- `test_state_dataclasses.py` — intentionally tests naive-datetime rejection

**Test 4**: `test_io_import_walker_is_not_a_noop` — positive regression asserting the
walker flags a synthetic `import requests` source (T-01-16 guard).

## PURE-09 Allowlist Reference (for Phase 2/3/4 maintainers)

When adding a new pure-layer module, add its name to `PURE_LAYER_MODULES` and the
allowlist updates automatically via `ROCM_MQ_ALLOWLIST = {f"rocm_mq.{m}" for m in PURE_LAYER_MODULES}`.

When a new stdlib import is needed in a pure-layer module, add the top-level module
name to `STDLIB_ALLOWLIST`. The walker allows submodules automatically (e.g.,
`collections.abc` is allowed because `collections` is on the list).

When a new banned I/O import pattern is discovered, add to `BANNED_IMPORTS`.

## Phase 1 Completion Checklist

| Requirement | Delivered in | Test file | Status |
|-------------|-------------|-----------|--------|
| PURE-01: State dataclasses (frozen, slots, tz-aware) | Plan 01 | test_state_dataclasses.py | DONE |
| PURE-02: pathmap implements RFC §4.2 mapping | Plan 01 | test_pathmap.py | DONE |
| PURE-03: derive_pr three-case logic | Plan 02 | test_derive.py | DONE |
| PURE-04: comment.py + summary.py renderers | Plan 02 | test_comment_render.py, test_summary_render.py | DONE |
| PURE-05: is_app_identity triple-check | Plan 01 | test_helpers_is_app_identity.py | DONE |
| PURE-06: parse_gh_timestamp rejects naive | Plan 01 | test_helpers_parse_gh_timestamp.py | DONE |
| PURE-07: RFC §6 invariants (Hypothesis suite) | Plan 04 | test_invariant_*.py (5 files) | DONE |
| PURE-08: RFC §4.2 worked example regression | Plan 05 (this) | test_worked_example.py | DONE |
| PURE-09: CI lint — no I/O imports in pure layer | Plan 05 (this) | test_pure_layer_imports.py | DONE |

All 9 PURE-* requirements satisfied across Plans 01–05.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `__future__` not in STDLIB_ALLOWLIST**
- **Found during:** Task 2, first test run
- **Issue:** All pure-layer modules use `from __future__ import annotations` (PEP 563 deferred evaluation). The initial STDLIB_ALLOWLIST did not include `__future__`, causing Test 1 to fail for all 6 modules.
- **Fix:** Added `"__future__"` to `STDLIB_ALLOWLIST`.
- **Files modified:** `tests/test_pure_layer_imports.py`
- **Commit:** `cbabfead799`

**2. [Rule 1 - Bug] Q5 walker would false-positive on tz-aware datetime() in prior-plan test files**
- **Found during:** Task 2 design analysis
- **Issue:** Plans 01–04 test files use `datetime(..., tzinfo=UTC)` directly (not the `utc()` helper). Banning ALL `datetime()` calls would fail on these existing files, contradicting the acceptance criterion "passes against current test files."
- **Fix:** Q5 walker checks for `datetime()` calls WITHOUT a `tzinfo=` keyword argument only (the actual Pitfall 3 risk is naive datetime construction). Added `test_state_dataclasses.py` to exclusion list (intentionally tests naive datetime behavior). All existing tz-aware calls pass the check.
- **Files modified:** `tests/test_pure_layer_imports.py`
- **Commit:** `cbabfead799`

## Known Stubs

None — all tests exercise live implementation code.

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes.
T-01-16 through T-01-20 mitigations from the plan's threat register are all implemented:
- T-01-16: Test 4 positive regression guards against silent no-op walker refactors.
- T-01-17: Test 2 flags `datetime.now/utcnow/fromisoformat` in pure-layer modules.
- T-01-18: Test 3 (Q5) flags naive `datetime()` calls in test files.
- T-01-19: Set equality at T2+T3 tolerates implementation-defined Pitfall 8 ordering.
- T-01-20: Zero `tests._strategies` references in `test_worked_example.py` (verified).

## Self-Check: PASSED

Files created:
- `.github/merge-queue/tests/test_worked_example.py` — FOUND (committed in 0dc018a56b2)
- `.github/merge-queue/tests/test_pure_layer_imports.py` — FOUND (committed in cbabfead799)

Commits verified:
- `0dc018a56b2` — Task 1 (worked example)
- `cbabfead799` — Task 2 (PURE-09 lint)

Final test count: 217 passed (53.55s) — 23 new tests added in this plan.

Acceptance criteria verified:
- `grep -c "tests._strategies" tests/test_worked_example.py` → 0 (PASS)
- `grep -c "from tests.conftest import.*canonical_merge_queue_config" tests/test_worked_example.py` → 1 (PASS)
- `grep -c "Q2" tests/test_worked_example.py` → 5 (PASS)
- `grep -c "Q5" tests/test_pure_layer_imports.py` → 7 (PASS)
- `pytest tests/test_worked_example.py tests/test_pure_layer_imports.py` → 23 passed (PASS)
- `pytest` (full suite) → 217 passed (PASS)
