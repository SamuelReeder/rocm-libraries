---
phase: 01-pure-decision-layer
plan: "01"
subsystem: pure-decision-layer
tags: [python, dataclasses, hypothesis, mypy, ruff, state-model, helpers]
dependency_graph:
  requires: []
  provides:
    - rocm_mq package (importable, editable install)
    - rocm_mq.state (all frozen dataclasses, Action union, RenderContext types)
    - rocm_mq._helpers (parse_gh_timestamp, is_app_identity, is_app_identity_actor, NaiveDatetimeError)
    - rocm_mq.__init__ (full public API re-exports for Phase 2 handoff surface)
    - tests/conftest.py (hypothesis profiles, utc() helper, AppIdentity fixtures)
    - PURE-01, PURE-05, PURE-06 test coverage
  affects: []
tech_stack:
  added:
    - Python 3.12.3 (3.11 floor per pyproject.toml)
    - hatchling>=1.27 (build backend)
    - pytest>=9.0.3 (installed: 9.0.3)
    - hypothesis>=6.152.7 (installed: 6.152.7)
    - time-machine>=2.14 (installed: 3.2.0)
    - syrupy>=4 (installed: 5.2.0)
    - ruff>=0.15.13 (via venv)
    - mypy>=2.1.0 (via venv)
    - pytest-cov>=5 (installed: 7.1.0)
    - pytest-xdist>=3.6 (installed: 3.8.0)
  patterns:
    - frozen dataclasses (frozen=True, slots=True) — established convention for all rocm_mq dataclasses
    - PEP 604 Action union (Activate | Squash | Eject | UpdateComment | Defer) with assert_never exhaustiveness
    - Single-chokepoint helpers (parse_gh_timestamp, is_app_identity) — all datetime parsing and App-identity
      checks in the entire codebase go through these two functions
    - Hypothesis property tests with RuleBasedStateMachine (registered ci/dev profiles)
    - mypy --strict applied via [[tool.mypy.overrides]] on six pure-layer modules only
key_files:
  created:
    - .github/merge-queue/pyproject.toml
    - .github/merge-queue/.gitignore
    - .github/merge-queue/README.md
    - .github/merge-queue/src/rocm_mq/__init__.py
    - .github/merge-queue/src/rocm_mq/state.py
    - .github/merge-queue/src/rocm_mq/_helpers.py
    - .github/merge-queue/tests/conftest.py
    - .github/merge-queue/tests/test_helpers_is_app_identity.py
    - .github/merge-queue/tests/test_helpers_parse_gh_timestamp.py
    - .github/merge-queue/tests/test_state_dataclasses.py
  modified: []
decisions:
  - D-01 honored: RawPRState + PRState two-family split; derive_pr module home deferred to Plan 02 (decision.py)
  - D-02 honored: RawPRState.head_statuses carries all statuses unfiltered; creator filter applied in derive
  - D-03 honored: PRState contains no renderer-only fields; RenderContext is separate
  - D-04 honored: Snapshot is flat tuple[PRState, ...]; now is separate arg to decide_cycle
  - Q1 resolved: DeferredPR (number, reason) is the second arm of derive_pr's return type
  - Q4 resolved: PartialPRState (number, head_sha, labels) is the second arm of Defer.pr union
  - uv not available on dev box — using pip install -e .github/merge-queue[dev] with local venv
metrics:
  duration: 9m
  completed: "2026-05-18"
  tasks_completed: 3
  tasks_total: 3
  files_created: 10
  files_modified: 0
  tests_added: 104
  tests_passing: 104
---

# Phase 01 Plan 01: Package Scaffold + State Model + Helper Tests Summary

Established the `rocm_mq` Python package under `.github/merge-queue/` with the complete
frozen-dataclass state model (22 dataclasses across Raw*, derived, Action, Config, and Render
families), two single-chokepoint helpers (`parse_gh_timestamp` raising `NaiveDatetimeError` on
naive input, `is_app_identity` triple-checking type+slug+app_id), and a 104-test suite covering
PURE-01 (dataclass invariants), PURE-05 (is_app_identity), and PURE-06 (parse_gh_timestamp).

## Stack Pins (Final)

| Package | Requested | Installed |
|---------|-----------|-----------|
| Python | >=3.11 | 3.12.3 |
| pytest | >=9.0.3 | 9.0.3 |
| hypothesis | >=6.152.7 | 6.152.7 |
| time-machine | >=2.14 | 3.2.0 |
| syrupy | >=4 | 5.2.0 |
| pytest-cov | >=5 | 7.1.0 |
| pytest-xdist | >=3.6 | 3.8.0 |

## Public API Surface Added

### State types (rocm_mq.state)

**Raw family** (D-01, D-02):
- `CommitStatusCreator(login, type, app_slug: str|None, app_id: int|None)` — GitHub commit status creator
- `CommitStatus(context, state, creator, created_at)` — commit status with full creator metadata
- `TimelineActor(login, type, user_id)` — timeline event actor
- `LabelEvent(label_name, event, actor, created_at)` — label applied/removed event
- `RequiredCheckResult(name, state)` — required CI check result
- `RawPRState(number, head_sha, labels, head_statuses, mq_queued_label_events, required_check_results, changed_paths)` — unfiltered raw PR
- `RawSnapshot(prs)` — raw snapshot for Phase 2 output

**Derived family** (D-01, D-03, D-04):
- `PRState(number, head_sha, labels, queues, enqueued_at, is_validly_active, required_check_results)` — decision-only derived PR
- `Snapshot(prs)` — flat tuple of derived PRs for decide_cycle input

**Q1/Q4 sum types**:
- `DeferredPR(number, reason)` — second arm of derive_pr return type (Q1 resolution)
- `PartialPRState(number, head_sha, labels)` — second arm of Defer.pr union (Q4 resolution)

**Action union** (PEP 604):
- `Activate(pr)`, `Squash(pr)`, `Eject(pr, reason)`, `UpdateComment(pr, new_body)`, `Defer(pr, reason)`
- `Action = Activate | Squash | Eject | UpdateComment | Defer`

**Config types**:
- `AppIdentity(slug, app_id, bot_user_id)` — canonical App identity sentinel
- `MergeQueueConfig(all_queues, path_to_queues, app_identity, activation_status_context="merge-queue/active", queued_label="mq:queued", active_label="mq:active", label_prefix="mq:")`

**Render context** (D-03):
- `RenderContext(author_login, pr_title, queue_positions, blockers, cycle_run_url, state, eject_reason, merged_sha)`
- `ActionOutcome(action, success, error_message)` — Phase 2 executor populates
- `CycleRenderContext(cycle_started_at, cycle_completed_at, cycle_run_url, queue_depths)`

### Helpers (rocm_mq._helpers)

- `parse_gh_timestamp(s: str) -> datetime` — single chokepoint for GitHub timestamp parsing; raises `NaiveDatetimeError` on naive input
- `is_app_identity(actor: CommitStatusCreator, expected: AppIdentity) -> bool` — triple-check (type+slug+app_id)
- `is_app_identity_actor(actor: TimelineActor, expected: AppIdentity) -> bool` — bot_user_id check for timeline events
- `NaiveDatetimeError(ValueError)` — raised by parse_gh_timestamp for tz-missing timestamps

## Resolution Log

| Decision | Status | Notes |
|----------|--------|-------|
| D-01: raw + derived dataclass split | Honored | RawPRState + PRState; derive_pr home deferred to Plan 02 (decision.py) |
| D-02: head_statuses unfiltered | Honored | tuple[CommitStatus, ...] with full creator metadata |
| D-03: renderer-only fields in RenderContext | Honored | PRState has no author_login, pr_title, blockers, run_url |
| D-04: flat Snapshot.prs tuple | Honored | Snapshot(prs: tuple[PRState, ...]) with now as separate arg |
| Q1: derive_pr return type | Resolved | PRState \| DeferredPR; DeferredPR exported from state.py |
| Q4: Defer.pr union | Resolved | PRState \| PartialPRState; PartialPRState exported from state.py |

## Test Coverage

| Test file | Tests | Coverage |
|-----------|-------|---------|
| test_helpers_is_app_identity.py | 20 | Canonical+5 non-canonical variants; per-check-failure tests; 4 property tests |
| test_helpers_parse_gh_timestamp.py | 24 | Round-trip property; 4 GitHub-shaped units; naive raises; garbage raises |
| test_state_dataclasses.py | 60 | frozen+slots for all 22 dataclasses; no mutable containers; tz-aware guard; anti-Pydantic canary |
| **Total** | **104** | All passing |

## Threat Mitigations Applied (T-01-01 through T-01-04)

| Threat ID | Mitigation Status |
|-----------|------------------|
| T-01-01 (Spoofing — is_app_identity) | Applied: triple AND in is_app_identity; 6 parametrized + 4 property tests |
| T-01-02 (NaiveDatetimeError) | Applied: NaiveDatetimeError on tzinfo is None; round-trip property test |
| T-01-03 (Anti-Pydantic) | Applied: test_anti_pydantic_canary_* tests; all dataclasses are stdlib frozen |
| T-01-04 (Mutable defaults) | Applied: all collections are tuple/frozenset; ruff B006 active; parametrized mutable-annotation test |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Ruff lint violations in generated files**
- **Found during:** All tasks (ruff check runs during verification)
- **Issue:** Import ordering (I001), unsorted `__all__` (RUF022), en-dash in docstrings (RUF002/RUF003), unused imports (F401), UP017 datetime.UTC alias, SIM102 nested if simplification
- **Fix:** Applied `ruff check --fix --unsafe-fixes` on all files; manually fixed en-dash chars
- **Files modified:** `src/rocm_mq/__init__.py`, `tests/conftest.py`, `tests/test_helpers_parse_gh_timestamp.py`, `tests/test_state_dataclasses.py`
- **Commit:** included in respective task commits

### Package Manager Deviation

Per RESEARCH.md line 93: "uv not installed on dev box" — used `python3 -m venv .venv && .venv/bin/pip install -e .[dev]` instead of `uv sync`. A local `.venv/` was created inside `.github/merge-queue/` but is gitignored via standard Python ignores. This does not affect CI (the plan notes "Later plans may swap to uv sync --frozen in CI workflows").

## Known Stubs

The following intentional stubs exist in conftest.py — they are scaffolding fixtures for Plan 03 (renderer tests), not blocking the Plan 01 goal:

| Stub | File | Line | Reason |
|------|------|------|--------|
| `pr_state__*` fixtures | tests/conftest.py | ~208-280 | Placeholder PRState instances with minimal data; Plan 03 populates with scenario-specific content for syrupy snapshot tests |
| `render_ctx__*` fixtures | tests/conftest.py | ~283-365 | Placeholder RenderContext instances; Plan 03 uses them for renderer snapshot tests |

These stubs do NOT block Plan 01's goal — they are structural scaffolding that exercises dataclass construction and will be filled in by Plan 03.

## Self-Check: PASSED

**Files exist:**
- [x] `.github/merge-queue/pyproject.toml`
- [x] `.github/merge-queue/.gitignore`
- [x] `.github/merge-queue/README.md`
- [x] `.github/merge-queue/src/rocm_mq/__init__.py`
- [x] `.github/merge-queue/src/rocm_mq/state.py`
- [x] `.github/merge-queue/src/rocm_mq/_helpers.py`
- [x] `.github/merge-queue/tests/conftest.py`
- [x] `.github/merge-queue/tests/test_helpers_is_app_identity.py`
- [x] `.github/merge-queue/tests/test_helpers_parse_gh_timestamp.py`
- [x] `.github/merge-queue/tests/test_state_dataclasses.py`

**Commits exist:**
- [x] 56dd5c23206 — feat(01-01): scaffold rocm_mq package layout
- [x] ec0c52b09a0 — feat(01-01): implement state.py + _helpers.py
- [x] 5b79f3a7991 — test(01-01): add conftest + helper tests + state dataclass invariant tests
