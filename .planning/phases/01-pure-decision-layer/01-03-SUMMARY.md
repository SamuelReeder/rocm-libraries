---
phase: 01-pure-decision-layer
plan: "03"
subsystem: pure-decision-layer
tags: [python, comment, summary, syrupy, snapshots, tdd, mypy, ruff, renderers]
dependency_graph:
  requires:
    - phase: 01-01
      provides: "PRState, RenderContext, CycleRenderContext, Action union, ActionOutcome, AppIdentity, MergeQueueConfig frozen dataclasses"
    - phase: 01-02
      provides: "Snapshot, Action variants (Activate, Squash, Eject, UpdateComment, Defer) used by render_cycle_summary"
  provides:
    - rocm_mq.comment (render_status_body — four-state markdown renderer with <!-- rocm-mq-status --> marker)
    - rocm_mq.summary (render_cycle_summary — four-section RFC §4.6 cycle summary renderer)
    - rocm_mq.__init__ (render_status_body, render_cycle_summary added to public surface)
    - tests/conftest.py (CANONICAL_APP constant, canonical_merge_queue_config() factory, realistic pr_state__*/render_ctx__* fixtures)
    - tests/__init__.py (makes tests a package for from tests.conftest import ... pattern)
    - tests/__snapshots__/test_comment_render.ambr (golden markdown for queued/active/merged/ejected x2)
    - tests/__snapshots__/test_summary_render.ambr (golden markdown for empty/mixed/all-Defer cycles)
    - PURE-04 test coverage (24 tests across comment + summary render)
  affects:
    - 01-04 (invariant property suite imports canonical_merge_queue_config from tests.conftest)
    - 01-05 (worked-example imports canonical_merge_queue_config from tests.conftest)
    - phase-02 (cmd_process.py calls render_cycle_summary; cmd_handle.py calls render_status_body and greps for <!-- rocm-mq-status -->)
    - phase-03 (cmd_handle.py upserts status comment using <!-- rocm-mq-status --> marker)
tech-stack:
  added: []
  patterns:
    - "syrupy snapshot testing (.ambr goldens committed — reviewer MUST diff before approving PR)"
    - "Pure renderer pattern: dispatches on string state field via match with assert_never default"
    - "canonical_merge_queue_config() as module-level callable in tests/conftest.py (not a pytest fixture)"
    - "tests/__init__.py enables from tests.conftest import ... for downstream plans"
key-files:
  created:
    - .github/merge-queue/src/rocm_mq/comment.py
    - .github/merge-queue/src/rocm_mq/summary.py
    - .github/merge-queue/tests/__init__.py
    - .github/merge-queue/tests/test_comment_render.py
    - .github/merge-queue/tests/test_summary_render.py
    - .github/merge-queue/tests/__snapshots__/test_comment_render.ambr
    - .github/merge-queue/tests/__snapshots__/test_summary_render.ambr
  modified:
    - .github/merge-queue/src/rocm_mq/__init__.py (render_status_body, render_cycle_summary added)
    - .github/merge-queue/tests/conftest.py (CANONICAL_APP, canonical_merge_queue_config, realistic fixtures)
    - .github/merge-queue/pyproject.toml (added pythonpath=["."] for tests package import)
key-decisions:
  - "<!-- rocm-mq-status --> marker literal in comment.py _STATUS_MARKER constant — Phase 3 cmd_handle.py greps for this string to upsert the comment (T-01-09)"
  - "render_cycle_summary hardcodes 'mq:active' as the active label — project-wide constant, Phase 4 may thread through CycleRenderContext if per-config label names needed"
  - "RenderContext.state is a plain str ('queued'/'active'/'merged'/'ejected') dispatched via match — Phase 4 may swap to Literal enum (deferred per CONTEXT.md)"
  - "canonical_merge_queue_config() is a callable (not a pytest fixture) so Plans 04+05 can call it at module scope or in @initialize methods"
  - "tests/__init__.py + pythonpath=['.'] enables 'from tests.conftest import canonical_merge_queue_config' — single owning home, eliminates two-copy drift (T-01-12b)"
  - "CycleRenderContext.queue_depths carries (queue_name, total_depth) — 'Queue depth' section renders total depth column only; Active count derived from snapshot.prs filter in section 2"
requirements-completed: [PURE-04]
duration: 35min
completed: "2026-05-18"
---

# Phase 01 Plan 03: Status Comment Renderer + Cycle Summary Renderer Summary

**Pure markdown renderers for PR status comments and cycle summaries with syrupy golden-file tests, assert_never exhaustiveness, and the canonical_merge_queue_config() factory establishing the single shared configuration for Plans 04 and 05**

## Performance

- **Duration:** ~35 min
- **Started:** 2026-05-18T23:30:00Z
- **Completed:** 2026-05-18T23:59:00Z
- **Tasks:** 2
- **Files created:** 7
- **Files modified:** 3
- **Tests added:** 24 (14 comment + 10 summary)
- **Tests total:** 176 (all passing)

## Accomplishments

- `render_status_body` renders four PR states (queued/active/merged/ejected) with `<!-- rocm-mq-status -->` marker in every body — Phase 3 `cmd_handle.py` greps this string for comment upsert
- `render_cycle_summary` renders four RFC §4.6 sections with `match action: ... case _: assert_never(action)` exhaustiveness over all five Action variants — enforced by mypy --strict (T-01-10)
- `canonical_merge_queue_config()` factory in `tests/conftest.py` — single owning home for the six hipDNN-ecosystem queues config, consumed by Plans 04 (invariant suite) and 05 (worked-example regression) without copy-paste drift
- syrupy `.ambr` goldens committed for 8 snapshot scenarios (5 comment + 3 summary) — drift surfaces in code review

## Task Commits

Each task was committed atomically using TDD (RED → GREEN):

1. **Task 1 RED: comment.py failing tests** - `7467d412e14` (test)
2. **Task 1 GREEN: comment.py implementation** - `ded156935f1` (feat)
3. **Task 2 RED: summary.py failing tests** - `2f1fa190859` (test)
4. **Task 2 GREEN: summary.py implementation** - `c1cf194824e` (feat)

## Files Created/Modified

- `.github/merge-queue/src/rocm_mq/comment.py` — `render_status_body(pr_state, render_ctx, now) -> str`: match dispatch on render_ctx.state, common footer with <!-- rocm-mq-status --> marker
- `.github/merge-queue/src/rocm_mq/summary.py` — `render_cycle_summary(snapshot, actions, outcomes, render_ctx) -> str`: four sections, match+assert_never, _format_duration helper
- `.github/merge-queue/src/rocm_mq/__init__.py` — added render_status_body, render_cycle_summary to public surface
- `.github/merge-queue/tests/__init__.py` — makes tests a package for `from tests.conftest import ...`
- `.github/merge-queue/tests/conftest.py` — CANONICAL_APP constant, canonical_merge_queue_config() factory, realistic pr_state__* and render_ctx__* fixture data
- `.github/merge-queue/pyproject.toml` — added `pythonpath=["."]` to pytest config
- `.github/merge-queue/tests/test_comment_render.py` — 14 tests: 5 syrupy snapshots + 9 smoke tests
- `.github/merge-queue/tests/test_summary_render.py` — 10 tests: 3 syrupy snapshots + 7 smoke tests
- `.github/merge-queue/tests/__snapshots__/test_comment_render.ambr` — 5 golden files (queued, active, merged, ejected×2)
- `.github/merge-queue/tests/__snapshots__/test_summary_render.ambr` — 3 golden files (empty, mixed, all-Defer)

## Decisions Made

**Marker string locked:** `<!-- rocm-mq-status -->` is the load-bearing comment-upsert marker. Stored as `_STATUS_MARKER` constant in `comment.py`. Any change requires coordinated Phase 3 change. Asserted in both syrupy goldens and smoke tests (T-01-09).

**Hardcoded `"mq:active"` in summary.py:** `render_cycle_summary` filters `snapshot.prs` with `"mq:active"` to identify active PRs for section 2. This is the project-wide label constant (matches `MergeQueueConfig.active_label` default). Phase 4 may thread the config's `active_label` through `CycleRenderContext` if per-config label names are ever needed. Documented in module docstring.

**RenderContext.state as plain str:** The four state values (`"queued"`, `"active"`, `"merged"`, `"ejected"`) are plain strings dispatched via `match`. Phase 4 may upgrade to `Literal["queued", "active", "merged", "ejected"]` — the renderer match arms are already exactly this set, so the upgrade is backward-compatible.

**canonical_merge_queue_config() is a callable (not a fixture):** Plan 04 state-machine `@initialize` methods and Plan 05 module-scope constants both need to invoke this at module load time — a pytest fixture would not be available outside fixture context. The callable pattern works in both contexts.

**Snapshot review rule:** `.ambr` files are committed to the repo. Reviewers MUST diff the `.ambr` changes before approving any PR that touches `comment.py` or `summary.py`. This is the entire contract — the snapshot IS the specification.

**CycleRenderContext queue_depths carries total depth only:** `queue_depths: tuple[tuple[str, int], ...]` has one int per queue (total depth). The "Queue depth" table renders this as a single "Depth" column. The "Active PRs" section separately filters `snapshot.prs` by `mq:active` label to get per-PR active status. Phase 4 may add per-queue active count to `CycleRenderContext` if the depth table needs a split Queued/Active display.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Ruff lint violations on new files**
- **Found during:** Both tasks (ruff check runs during verification)
- **Issue:** Import ordering (I001), unsorted `__all__` (RUF022), unused noqa directives (RUF100), line length (E501), zip without strict= (B905), SIM108 ternary
- **Fix:** `ruff check --fix --unsafe-fixes` on affected files; manual fixes for remaining issues
- **Files modified:** `src/rocm_mq/comment.py`, `src/rocm_mq/summary.py`, `src/rocm_mq/__init__.py`, `tests/test_comment_render.py`, `tests/test_summary_render.py`
- **Committed in:** included in respective task GREEN commits

**2. [Rule 2 - Missing Critical] Added tests/__init__.py + pythonpath config**
- **Found during:** Task 1 verification (`from tests.conftest import canonical_merge_queue_config` failed)
- **Issue:** Tests directory was not a Python package, so `from tests.conftest import ...` raised `ModuleNotFoundError: No module named 'tests'`
- **Fix:** Created `tests/__init__.py` (empty except for docstring); added `pythonpath=["."]` to `[tool.pytest.ini_options]` in `pyproject.toml`
- **Files modified:** `tests/__init__.py` (new), `pyproject.toml`
- **Verification:** `python -c "from tests.conftest import canonical_merge_queue_config"` exits 0
- **Committed in:** `ded156935f1` (Task 1 GREEN commit)

**3. [Rule 1 - Bug] Fixed utc() helper signature in test_summary_render.py**
- **Found during:** Task 2 GREEN (snapshot generation run)
- **Issue:** Local `utc()` in test_summary_render.py defined with 5 positional args (no `second`), but called with 6 positional args including seconds for cycle_completed_at
- **Fix:** Added `second: int = 0` parameter to the local `utc()` helper
- **Files modified:** `tests/test_summary_render.py`
- **Committed in:** `c1cf194824e` (Task 2 GREEN commit)

---

**Total deviations:** 3 auto-fixed (1 lint, 1 missing critical infrastructure, 1 bug)
**Impact on plan:** All necessary for correctness and importability. No scope creep.

## Known Stubs

None — both renderers are complete implementations. The `_ACTIVE_LABEL = "mq:active"` hardcoding in `summary.py` is a documented design decision (not a stub), with a Phase 4 revisit note in the module docstring.

## Threat Mitigations Applied

| Threat ID | Mitigation Status |
|-----------|------------------|
| T-01-09 (Marker drift breaks Phase 3 comment-upsert) | Applied: `_STATUS_MARKER` constant in comment.py; asserted in syrupy golden + smoke test |
| T-01-10 (New Action variant without summary.py match dispatch) | Applied: `case _: assert_never(action)` enforced by mypy --strict; exhaustiveness regression test in test_summary_render.py |
| T-01-11 (Markdown injection via RenderContext fields) | Accepted: Phase 1 trusts RenderContext field values; escaping is Phase 2/3 concern at the construction site |
| T-01-12b (Drift between Plan 04 strategies copy and Plan 05 copy of canonical config) | Applied: `canonical_merge_queue_config()` in `tests/conftest.py` only; single owning home; tests/__init__.py makes it importable |

## Snapshot Reviewer Rule

**tests/__snapshots__/test_comment_render.ambr and tests/__snapshots__/test_summary_render.ambr MUST be reviewed in every PR that modifies comment.py or summary.py.**

The `.ambr` diff is the contract review — it shows exactly what the rendered markdown looks like for each scenario. An unexpected change in the golden output (e.g., the `<!-- rocm-mq-status -->` marker disappearing, or a section being reordered) must be caught at review time, not at Phase 3 integration time.

To update goldens after intentional changes: `cd .github/merge-queue && pytest tests/test_comment_render.py tests/test_summary_render.py --snapshot-update`

## Self-Check: PASSED

**Files exist:**
- [x] `.github/merge-queue/src/rocm_mq/comment.py`
- [x] `.github/merge-queue/src/rocm_mq/summary.py`
- [x] `.github/merge-queue/src/rocm_mq/__init__.py` (updated)
- [x] `.github/merge-queue/tests/__init__.py`
- [x] `.github/merge-queue/tests/conftest.py` (updated)
- [x] `.github/merge-queue/tests/test_comment_render.py`
- [x] `.github/merge-queue/tests/test_summary_render.py`
- [x] `.github/merge-queue/tests/__snapshots__/test_comment_render.ambr`
- [x] `.github/merge-queue/tests/__snapshots__/test_summary_render.ambr`
- [x] `.github/merge-queue/pyproject.toml` (updated)

**Commits exist:**
- [x] `7467d412e14` — test(01-03): add failing tests for comment.render_status_body (RED)
- [x] `ded156935f1` — feat(01-03): implement comment.py + populate conftest fixtures + snapshots (GREEN)
- [x] `2f1fa190859` — test(01-03): add failing tests for summary.render_cycle_summary (RED)
- [x] `c1cf194824e` — feat(01-03): implement summary.py + cycle summary snapshots (GREEN)
