---
phase: "01"
plan: "04"
subsystem: tests
tags: [hypothesis, property-based-testing, invariants, rfc-s6, state-machine]
dependency_graph:
  requires:
    - "01-01"  # state.py — dataclasses consumed by strategies
    - "01-02"  # decision.py — derive_pr + decide_cycle under test
    - "01-03"  # conftest.py — CANONICAL_APP + canonical_merge_queue_config
  provides:
    - hypothesis-invariant-suite
    - rfc-s6-property-tests
  affects:
    - "05"     # worked-example regression uses same conftest fixtures
tech_stack:
  added:
    - hypothesis RuleBasedStateMachine with @initialize + @rule + @invariant
    - "@given + @settings adversarial property tests"
    - shadow-state pattern (Pitfall 11) for temporal invariants
  patterns:
    - "Pre-cycle snapshot capture (last_cycle_pre_queue_order, last_cycle_pre_active_prs)"
    - "shadow_squash_had_canonical_activation cache for post-removal invariant checks"
    - "Non-Test prefix for RuleBasedStateMachine classes + @settings decorator (not class attr)"
    - "Within-cycle FIFO check (not cross-cycle history) due to Pitfall 4 timeline lag"
key_files:
  created:
    - .github/merge-queue/tests/_strategies.py
    - .github/merge-queue/tests/_state_machine_base.py
    - .github/merge-queue/tests/test_strategies_and_base_imports.py
    - .github/merge-queue/tests/test_invariant_head_of_all_queues.py
    - .github/merge-queue/tests/test_invariant_fifo.py
    - .github/merge-queue/tests/test_invariant_idempotent_re_enqueue.py
    - .github/merge-queue/tests/test_invariant_no_squash_without_app_activation.py
    - .github/merge-queue/tests/test_invariant_app_creator_filter_unspoofable.py
    - .github/merge-queue/.hypothesis/examples/.gitkeep
  modified:
    - .github/merge-queue/.gitignore
decisions:
  - "FIFO invariant uses within-cycle ordering (not cross-cycle history): Pitfall 4 timeline lag means activation order across cycles is not globally monotone — only within a single cycle's visible snapshot is the FIFO guarantee invariant-testable"
  - "RuleBasedStateMachine classes use non-Test prefix (HeadOfAllQueuesMachine etc) to prevent PytestCollectionWarning promoted to error by filterwarnings=error"
  - "@settings decorator on class (not settings = settings() class attribute) — InvalidDefinition error otherwise"
  - "shadow_squash_had_canonical_activation cache: _apply_actions_to_shadow evaluates _raw_had_canonical_activation BEFORE _remove_pr_from_shadow so @invariant can check post-removal PRs"
  - "last_cycle_pre_queue_order and last_cycle_pre_active_prs captured BEFORE decide_cycle call to enable pre-cycle headship and double-activate checks"
  - "Q3 resolution: App-creator filter test uses @given (NOT RuleBasedStateMachine) — pure function property of derive_pr, no cycle sequencing needed"
  - ".hypothesis/examples tracked in git (Hypothesis counterexample replay DB); fixed .gitignore from ignored-parent-dir pattern to ignoreing unicode_data and *.json only"
metrics:
  duration: "~3.5 hours (including prior context window)"
  completed: "2026-05-19T00:17:40Z"
  tasks_completed: 3
  files_created: 9
  files_modified: 1
  tests_added: 18
  total_tests_after: 194
---

# Phase 01 Plan 04: Hypothesis Property-Based Invariant Suite Summary

RFC §6 invariants demonstrated via Hypothesis RuleBasedStateMachine (4 temporal invariants) plus adversarial @given property tests (Q3 App-creator filter unspoofable).

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | Failing imports for strategies + base | e1b6f0c95eb | test_strategies_and_base_imports.py |
| 1 (GREEN) | `_strategies.py` + `_state_machine_base.py` | 5e6b4c18ad2 | _strategies.py, _state_machine_base.py |
| 2 (GREEN) | Four RuleBasedStateMachine invariant tests | aebf30501ab | 4 test_invariant_*.py + _state_machine_base.py updated |
| 3 (GREEN) | Adversarial App-creator filter + .hypothesis DB | 33d2ec150fc | test_invariant_app_creator_filter_unspoofable.py, .gitkeep, .gitignore |

## What Was Built

### Task 1: Hypothesis Infrastructure

**`tests/_strategies.py`** — Composable Hypothesis strategies tight to D-03 (only decision-layer fields):
- `app_identity_strategy(canonical=)` — canonical or non-canonical AppIdentity
- `commit_status_creator_strategy(canonical=)` — canonical or 5 adversarial non-canonical variants
- `utc_datetime_strategy(min_year, max_year)` — always tz-aware (Pitfall 3 guard)
- `queue_subset_strategy(config)` — non-empty frozenset of queue names
- `label_event_strategy(is_app=)` — App or non-App actor LabelEvent
- `raw_pr_strategy()` — tight RawPRState with ~75% App-queued events, ~30% valid activations
- `raw_pr_with_forged_activation_status_strategy()` — adversarial RawPRState (right context, non-canonical creator)

**`tests/_state_machine_base.py`** — `MergeQueueStateMachineBase(RuleBasedStateMachine)`:
- Shadow state: `shadow_prs`, `shadow_raw_prs`, `shadow_queue_order`, `shadow_pending_timeline`
- Pre-cycle snapshots: `last_cycle_pre_queue_order`, `last_cycle_pre_active_prs`
- Squash cache: `shadow_squash_had_canonical_activation` — populated before PR removal
- Rules: `enqueue` (50% deferred via Pitfall 4 simulation), `push_new_commit`, `simulate_timeline_lag`, `advance_cycle`

### Task 2: Four RFC §6 Invariant Machines

1. **`HeadOfAllQueuesMachine`** (`test_invariant_head_of_all_queues.py`):
   - `no_squash_without_headship`: For every Squash action, the PR was at position 0 in every queue before the cycle (uses `last_cycle_pre_queue_order`)

2. **`FifoMachine`** (`test_invariant_fifo.py`):
   - `fifo_within_cycle`: For every Activate, the PR was at position 0 in its queues before the cycle; at-most-one Activate per queue per cycle
   - `fifo_queue_order_is_sorted`: shadow_queue_order for each queue is sorted by enqueued_at

3. **`IdempotentReEnqueueMachine`** (`test_invariant_idempotent_re_enqueue.py`):
   - `decide_cycle_is_pure`: Two calls with identical snapshot return canonically equal action lists
   - `no_double_activate`: No PR number appears twice in Activate list within a cycle; no PR with mq:active before the cycle receives a new Activate

4. **`NoSquashWithoutActivationMachine`** (`test_invariant_no_squash_without_app_activation.py`):
   - `squash_requires_validly_active`: Every Squash action has pr.is_validly_active=True
   - `squash_implies_canonical_activation_in_raw`: Stronger check — raw PR had a canonical-App merge-queue/active status (uses shadow_squash_had_canonical_activation cache since PR is removed from shadow_raw_prs before @invariant fires)

### Task 3: Adversarial App-Creator Filter (Q3 Resolution)

**`test_invariant_app_creator_filter_unspoofable.py`** — `@given` property test (NOT RuleBasedStateMachine):
- `test_forged_activation_status_is_rejected`: Hypothesis generates arbitrary forged-creator RawPRState; derive_pr must always return is_validly_active=False (500 examples)
- 5 deterministic fixture-driven tests (one per attack vector from RESEARCH.md matrix)
- 1 positive control: canonical creator yields is_validly_active=True

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] PytestCollectionWarning promoted to error**
- **Found during:** Task 2 development
- **Issue:** RuleBasedStateMachine classes named `TestHeadOfAllQueues` etc. caused `PytestCollectionWarning: cannot collect test class ... because it has a __init__ constructor`; `filterwarnings = ["error"]` in pyproject.toml promoted this to a collection ERROR
- **Fix:** Renamed all state machine classes to non-Test prefix: `HeadOfAllQueuesMachine`, `FifoMachine`, `IdempotentReEnqueueMachine`, `NoSquashWithoutActivationMachine`
- **Files modified:** All 4 test_invariant_*.py files

**2. [Rule 1 - Bug] InvalidDefinition for settings as class attribute**
- **Found during:** Task 2 development
- **Issue:** `settings = settings(stateful_step_count=30, ...)` as a class attribute produces Hypothesis `InvalidDefinition: Assigning settings = ... as a class attribute does nothing`
- **Fix:** Changed to `@settings(...)` decorator on each state machine class

**3. [Rule 1 - Bug] squash_implies_canonical_activation_in_raw returning False for valid squashes**
- **Found during:** Task 2 invariant testing
- **Issue:** `_apply_actions_to_shadow` calls `_remove_pr_from_shadow` for Squash actions BEFORE @invariant runs; `_raw_had_canonical_activation` could not find the PR and returned False (conservative fallback)
- **Fix:** Added `shadow_squash_had_canonical_activation: dict[int, bool]` cache; evaluate before removal, cache result, clear at cycle start; `_raw_had_canonical_activation` checks cache first
- **Files modified:** `tests/_state_machine_base.py`

**4. [Rule 1 - Bug] no_double_activate false positive (cross-cycle)**
- **Found during:** Task 2 invariant testing
- **Issue:** After advance_cycle with Activate, shadow_prs is updated to have mq:active. @invariant fires: "PR has mq:active AND Activate was just emitted" — false positive because the activate JUST happened this cycle
- **Fix:** Added `last_cycle_pre_active_prs: frozenset[int]` captured BEFORE decide_cycle; invariant checks against pre-cycle snapshot instead of current shadow_prs
- **Files modified:** `tests/_state_machine_base.py`

**5. [Rule 1 - Bug] no_squash_without_headship false positive (stale last_cycle_actions)**
- **Found during:** Task 2 invariant testing
- **Issue:** `last_cycle_actions` persisted across non-cycle rules; after a Squash cycle, the next enqueue rule changed shadow_queue_order, but @invariant still saw the old Squash in last_cycle_actions vs the new queue order
- **Fix:** Clear `last_cycle_actions = []` at START of advance_cycle (not end); added `last_cycle_pre_queue_order` snapshot taken BEFORE `_apply_actions_to_shadow`
- **Files modified:** `tests/_state_machine_base.py`

**6. [Rule 1 - Bug] FIFO cross-cycle history violation (Pitfall 4 timeline lag)**
- **Found during:** Task 2 — Hypothesis found a legitimate counterexample to the original cross-cycle invariant
- **Issue:** PR#3 (enqueued_at=T0) deferred via timeline lag. PR#5 (enqueued_at=T1) visible and activated. simulate_timeline_lag promotes PR#3. Next cycle: PR#3 (earlier timestamp) activated. Cross-cycle history [(PR#5, T1), (PR#3, T0)] is NOT monotone ascending — this is valid under Pitfall 4 (the RFC FIFO guarantee only applies within visible PRs in a single cycle)
- **Fix:** Redesigned FIFO invariant from cross-cycle history to within-cycle invariant: checks that activated PRs were at head of last_cycle_pre_queue_order, and at most one Activate per queue per cycle
- **Files modified:** `tests/test_invariant_fifo.py`, `tests/_state_machine_base.py`

**7. [Rule 2 - Critical] .hypothesis/examples .gitignore tracking fix**
- **Found during:** Task 3 setup
- **Issue:** Original .gitignore had `.hypothesis/` as an ignored directory plus `!.hypothesis/examples/` exception — but git cannot un-ignore files within an ignored parent directory. Additionally, Hypothesis auto-generates `.hypothesis/.gitignore` with `*` pattern that ignores everything inside.
- **Fix:** Changed .gitignore to ignore `unicode_data/` and `*.json` within `.hypothesis/` (not the whole dir); removed the Hypothesis-generated `.hypothesis/.gitignore`
- **Files modified:** `.github/merge-queue/.gitignore`

## Known Stubs

None — all tests exercise live implementation code.

## TDD Gate Compliance

- RED gate commit: `e1b6f0c95eb` (`test(01-04): add failing RED tests for _strategies + _state_machine_base modules`)
- GREEN gate commit: `5e6b4c18ad2` (`feat(01-04): implement _strategies.py + _state_machine_base.py`)
- Tasks 2 and 3 extended the GREEN phase (no additional RED commits required — infrastructure already established)

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes introduced. This plan adds test code only.

## Self-Check: PASSED

Files created:
- `.github/merge-queue/tests/_strategies.py` — FOUND (committed in 5e6b4c18ad2)
- `.github/merge-queue/tests/_state_machine_base.py` — FOUND (committed in 5e6b4c18ad2, updated in aebf30501ab)
- `.github/merge-queue/tests/test_invariant_head_of_all_queues.py` — FOUND (committed in aebf30501ab)
- `.github/merge-queue/tests/test_invariant_fifo.py` — FOUND (committed in aebf30501ab)
- `.github/merge-queue/tests/test_invariant_idempotent_re_enqueue.py` — FOUND (committed in aebf30501ab)
- `.github/merge-queue/tests/test_invariant_no_squash_without_app_activation.py` — FOUND (committed in aebf30501ab)
- `.github/merge-queue/tests/test_invariant_app_creator_filter_unspoofable.py` — FOUND (committed in 33d2ec150fc)
- `.github/merge-queue/.hypothesis/examples/.gitkeep` — FOUND (committed in 33d2ec150fc)

Commits verified:
- `e1b6f0c95eb` — RED gate
- `5e6b4c18ad2` — GREEN (Task 1)
- `aebf30501ab` — GREEN (Task 2)
- `33d2ec150fc` — GREEN (Task 3)

Final test count: 194 passed (54s) — 18 new tests added in this plan.
