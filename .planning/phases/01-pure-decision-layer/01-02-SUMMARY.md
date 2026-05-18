---
phase: 01-pure-decision-layer
plan: "02"
subsystem: pure-decision-layer
tags: [python, pathmap, decision, derive, decide_cycle, hypothesis, mypy, ruff, tdd]
dependency_graph:
  requires:
    - phase: 01-01
      provides: "rocm_mq package, state.py (all frozen dataclasses), _helpers.py (is_app_identity, parse_gh_timestamp), conftest.py"
  provides:
    - rocm_mq.pathmap (queues_for_paths — RFC §4.2 path-to-queue mapping)
    - rocm_mq.decision (derive_pr, derive_snapshot, decide_cycle — RFC §4.6 algorithm)
    - rocm_mq.__init__ (queues_for_paths, derive_pr, derive_snapshot, decide_cycle added to public surface)
    - tests/test_pathmap.py (PURE-02 coverage: 10 unit tests + 4 property tests)
    - tests/test_derive.py (PURE-03 coverage: derive_pr three cases + is_validly_active matrix + adversarial property)
    - tests/test_decide_cycle_unit.py (PURE-03 coverage: 7 dispatch-table rows + bonus tests)
  affects:
    - 01-03 (renderer tests consume Snapshot + Action types)
    - 01-04 (invariant property tests exercise derive_pr + decide_cycle via RuleBasedStateMachine)
    - 01-05 (PURE-09 AST walker will lint decision.py + pathmap.py for banned imports)
    - phase-02 (snapshot.py builds RawSnapshot; cmd_process.py calls derive_snapshot + decide_cycle)
tech-stack:
  added: []
  patterns:
    - TDD (RED → GREEN) discipline for both tasks — tests committed before implementation
    - queues_for_paths: first-match-wins longest-prefix-first iteration (precondition: loader pre-sorts config)
    - derive_pr: three-case total function — never raises; Case 3 > Case 2 > no-evidence > Case 1 priority
    - decide_cycle: RFC §4.6 steps 3-5 as pure function; mandatory continue in 5a enforces per-PR invariant
    - _evaluate_required_checks: any_failed > pending > all_passed priority order
    - assert_never exhaustiveness guard on verdict match in decide_cycle
    - Pitfall 7 guard: if not pr.queues: return False in is_head_of_all
    - Q2 resolution: per-PR "no same-cycle Activate+Squash" (not per-cycle-globally)
key-files:
  created:
    - .github/merge-queue/src/rocm_mq/pathmap.py
    - .github/merge-queue/src/rocm_mq/decision.py
    - .github/merge-queue/tests/test_pathmap.py
    - .github/merge-queue/tests/test_derive.py
    - .github/merge-queue/tests/test_decide_cycle_unit.py
  modified:
    - .github/merge-queue/src/rocm_mq/__init__.py
key-decisions:
  - "Q2 resolution: Activate+Squash in same cycle is valid for DISJOINT-queue PRs; invariant is per-PR not per-cycle-globally"
  - "derive_pr is total: never raises, returns DeferredPR for all three non-normal cases (tamper > lag > no-evidence)"
  - "Pitfall 7 guard: is_head_of_all explicitly returns False for empty frozenset() to prevent vacuous all([]) headship"
  - "mandatory continue in 5a prevents any 5b action for same PR in same cycle"
  - "queues_for_paths precondition: config.path_to_queues is pre-sorted longest-prefix-first by loader (Phase 4 responsibility)"
requirements-completed: [PURE-02, PURE-03]
duration: 22min
completed: "2026-05-18"
---

# Phase 01 Plan 02: Path Mapping + RFC §4.6 Algorithm Summary

**RFC §4.2 path-to-queue mapping and RFC §4.6 decide_cycle implemented as pure functions with three-case derive_pr, Pitfall 7 guard, and Q2 resolution locked in a focused unit test**

## Performance

- **Duration:** 22 min
- **Started:** 2026-05-18T23:00:00Z
- **Completed:** 2026-05-18T23:24:56Z
- **Tasks:** 2
- **Files created:** 5
- **Files modified:** 1
- **Tests added:** 48 (17 pathmap + 19 derive + 12 decide_cycle unit)
- **Tests total:** 152 (all passing)

## Accomplishments

- `pathmap.queues_for_paths` implements RFC §4.2 membership matrix including the asymmetric provider/integration-tests edge (integration-tests enters all provider queues but NOT hipdnn)
- `decision.derive_pr` implements the three-case total function: Case 3 (tampered non-App event), Case 2 (timeline lag), Case 1 (normal with `max(app_events.created_at)`)
- `decision.decide_cycle` implements RFC §4.6 steps 3-5 with mandatory 5a `continue`, Pitfall 7 empty-queues guard, and `assert_never` exhaustiveness
- Q2 resolution (RESEARCH.md Open Question 2) locked in a focused unit test with inline rationale: `[Squash(A), Activate(B)]` for disjoint-queue PRs is valid — invariant is per-PR, not per-cycle-globally

## Task Commits

Each task was committed atomically (TDD RED → GREEN):

1. **Task 1: pathmap.py (RED)** - `12425308f05` (test)
2. **Task 1: pathmap.py (GREEN)** - `a9c0598e23e` (feat)
3. **Task 2: decision.py (RED)** - `f9e782808ae` (test)
4. **Task 2: decision.py (GREEN)** - `6e092e6ab2d` (feat)

## Files Created/Modified

- `.github/merge-queue/src/rocm_mq/pathmap.py` — RFC §4.2 queues_for_paths: longest-prefix-first iteration over config.path_to_queues, returns frozenset[str]
- `.github/merge-queue/src/rocm_mq/decision.py` — derive_pr (three-case), derive_snapshot (walks RawSnapshot), decide_cycle (RFC §4.6 steps 3-5), _evaluate_required_checks, _first_failed_check_name
- `.github/merge-queue/src/rocm_mq/__init__.py` — added queues_for_paths, decide_cycle, derive_pr, derive_snapshot to __all__
- `.github/merge-queue/tests/test_pathmap.py` — 10 parametrized unit tests + 4 property tests (union, hipdnn-implies-all-six, asymmetric edge, non-opted-in-empty)
- `.github/merge-queue/tests/test_derive.py` — Case 1/2/3 unit tests, is_validly_active matrix (5 parametrized + 2 standalone), adversarial-creator property test, derive_snapshot integration tests
- `.github/merge-queue/tests/test_decide_cycle_unit.py` — 7 dispatch-table row tests + Pitfall 7 guard (2 tests) + 5a continue bonus + Q2 resolution test + 2 edge tests

## Decisions Made

- **Q2 resolution**: Kept per-PR "no same-cycle Activate+Squash" (not per-cycle-globally). When PRs A and B belong to disjoint queues, `[Squash(A), Activate(B)]` in one cycle is correct behavior. Locked in `test_bonus__q2_resolution__disjoint_queues_squash_and_activate_same_cycle` with inline comment referencing RESEARCH.md.
- **derive_pr is total**: Returns DeferredPR for all non-derivable cases rather than raising. Priority: Case 3 (tamper) > Case 2 (lag) > no-evidence > Case 1 (normal). Means the caller (`derive_snapshot`) never needs exception handling.
- **_evaluate_required_checks priority**: any_failed > pending > all_passed. Mixed failed+pending yields Eject (not "retry next cycle"). Zero results yields all_passed (PR with no required checks may be squashed — RFC §4.6 allows).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Ruff lint violations (import order, unsorted __all__)**
- **Found during:** Both tasks (ruff check runs during verification)
- **Issue:** `I001` import ordering in decision.py; `RUF022` unsorted `__all__` in __init__.py
- **Fix:** Applied `ruff check --fix --unsafe-fixes` on affected files; ruff reordered imports and re-sorted `__all__`
- **Files modified:** `src/rocm_mq/decision.py`, `src/rocm_mq/__init__.py`
- **Verification:** `ruff check` exits 0 after fix
- **Committed in:** included in respective task GREEN commits

---

**Total deviations:** 1 auto-fixed (Rule 1 - ruff lint)
**Impact on plan:** Lint-only fix; no logic change. No scope creep.

## Threat Mitigations Applied (T-01-05 through T-01-08)

| Threat ID | Mitigation Status |
|-----------|------------------|
| T-01-05 (Spoofing — is_validly_active) | Applied: AND-joined context + is_app_identity on head_statuses; 5-variant matrix + adversarial property test in test_derive.py |
| T-01-06 (Spoofing — enqueued_at tamper) | Applied: is_app_identity_actor filter on mq_queued_label_events; Case 3 tamper → DeferredPR |
| T-01-07 (Tampering — Squash without headship) | Applied: is_head_of_all guard with Pitfall 7 empty-frozenset check; per-row dispatch tests |
| T-01-08 (Naive datetime in enqueued_at) | Applied: all datetimes come from conftest.utc() helper or existing parse_gh_timestamp; no bare datetime() in decision.py |

## Q2 Resolution Implementation Note

RESEARCH.md Open Question 2 asks: "Does decide_cycle do a second pass after Squash to activate the newly-head PR?" The resolution (confirmed by PLAN.md):

**No second pass.** The "activation and evaluation never in the same cycle" invariant is **per-PR**. In a single decide_cycle call:
- PR A (in queue "hipdnn"): has `mq:active`, checks pass → `Squash(A)` (5b path)
- PR B (in queue "miopen-provider", disjoint from A): has `mq:queued`, no `mq:active` → `Activate(B)` (5a path)

Both actions in the same list. Neither PR got both Activate AND Squash — the per-PR invariant holds. This is captured in `test_bonus__q2_resolution__disjoint_queues_squash_and_activate_same_cycle` with an inline comment referencing RESEARCH.md.

## Plan 04 Forward-Looking Items

Plan 04 (invariant property tests) will need:
- `_strategies.py`: adversarial `CommitStatusCreator` strategy (forged right-context wrong-creator; used by T-01-05 machine)
- `_strategies.py`: raw_pr_strategy generating arbitrary `RawPRState` instances via `st.builds(...)`
- `_state_machine_base.py`: shadow-state pattern, `simulate_timeline_lag` rule, `advance_cycle` calling `derive_snapshot + decide_cycle`
- The five per-invariant subclasses extending the base

## Known Stubs

None — pathmap.py and decision.py are complete implementations with no placeholder returns.

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes introduced. pathmap.py and decision.py are pure functions with no trust-boundary surface.

## Self-Check: PASSED

**Files exist:**
- [x] `.github/merge-queue/src/rocm_mq/pathmap.py`
- [x] `.github/merge-queue/src/rocm_mq/decision.py`
- [x] `.github/merge-queue/tests/test_pathmap.py`
- [x] `.github/merge-queue/tests/test_derive.py`
- [x] `.github/merge-queue/tests/test_decide_cycle_unit.py`

**Commits exist:**
- [x] `12425308f05` — test(01-02): add failing tests for pathmap.queues_for_paths (RED)
- [x] `a9c0598e23e` — feat(01-02): implement pathmap.queues_for_paths (GREEN)
- [x] `f9e782808ae` — test(01-02): add failing tests for derive_pr + decide_cycle (RED)
- [x] `6e092e6ab2d` — feat(01-02): implement decision.py (derive_pr, derive_snapshot, decide_cycle) (GREEN)
