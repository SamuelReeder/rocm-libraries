---
phase: 02-i-o-layer-executor
plan: 01
subsystem: infra
tags: [githubkit, httpx, pyyaml, github-app, retry, io-layer, pure-09]

# Dependency graph
requires:
  - phase: 01-pure-decision-layer
    provides: AppIdentity dataclass; PURE-09 AST walker scaffolding; tests/ ruff baseline
provides:
  - GitHubClient(token) — sync wrapper around githubkit.GitHub with retry-on-SecondaryRateLimitExceeded
  - resolve_app_identity(client) — two-call startup resolver (apps.get_authenticated + users.get_by_username) that populates AppIdentity.bot_user_id
  - CorruptSquashError — RuntimeError subclass used by Phase 2 executor for post-squash parent-SHA verification failures
  - PURE-09 positive lint test (test_io_modules_do_import_githubkit) for gh/snapshot/executor
  - Runtime dependencies pinned in pyproject.toml (githubkit, httpx, PyYAML)
  - Clean ruff baseline across all of .github/merge-queue/
affects: [02-02, 02-03, 02-04, 03-handler-audit, 04-cmd-process]

# Tech tracking
tech-stack:
  added:
    - "githubkit 0.15.5 (sync GitHub REST client; sole approved site for GitHub(token=...))"
    - "httpx 0.28.1 (transitive via githubkit; pinned floor 0.27 in pyproject)"
    - "PyYAML 6.0.3 (planned PATH_TO_QUEUES parser; reserved early per RESEARCH.md)"
  patterns:
    - "I/O wrapper class isolation — GitHubClient owns _gh (githubkit.GitHub); callers use .rest passthrough; only gh.py imports githubkit"
    - "request_with_retry pattern — caller-supplied callable invoked under retry-loop; monkeypatchable rocm_mq.gh.time.sleep keeps tests fast"
    - "Startup-only two-call AppIdentity resolution — Integration model lacks bot_user_id so users.get_by_username(slug+'[bot]') is required (OQ-1/A1 resolution)"
    - "PURE-09 symmetric guards — Test 1 (pure-must-not-import-io) + Test 4 (io-must-import-githubkit) prevent both pure-fication of I/O modules and I/O-leak into pure layer"
    - "Anti-pydantic canary runs in clean subprocess — in-process sys.modules check would false-positive once any test imports rocm_mq.gh"

key-files:
  created:
    - ".github/merge-queue/src/rocm_mq/gh.py"
    - ".github/merge-queue/tests/test_gh_client.py"
    - ".planning/phases/02-i-o-layer-executor/02-01-SUMMARY.md"
  modified:
    - ".github/merge-queue/pyproject.toml"
    - ".github/merge-queue/tests/test_pure_layer_imports.py"
    - ".github/merge-queue/tests/test_state_dataclasses.py"
    - ".github/merge-queue/tests/test_decide_cycle_unit.py"
    - ".github/merge-queue/tests/test_derive.py"
    - ".github/merge-queue/tests/test_pathmap.py"
    - ".github/merge-queue/tests/test_strategies_and_base_imports.py"
    - ".github/merge-queue/tests/test_summary_render.py"

key-decisions:
  - "OQ-1 / A1 resolved: bot_user_id MUST be resolved via users.get_by_username(slug+'[bot]') — apps.get_authenticated() returns Integration without a bot-user field"
  - "CorruptSquashError lives in gh.py (single home) so executor.py can import from one place — chose option A from <deviations_allowed>"
  - "request_with_retry uses time.sleep at module level (monkeypatched in tests) instead of a fixed-fake injection — keeps production code path identical to test code path"
  - "Anti-pydantic canary tightened to subprocess form — in-process check was a latent bug once any test imported rocm_mq.gh (Rule 1 deviation)"
  - "Phase 2 I/O modules (gh, snapshot, executor) stay in BANNED_IMPORTS for the pure-layer Test 1 — they are not added to ROCM_MQ_ALLOWLIST"

patterns-established:
  - "I/O module file layout: from __future__ import annotations; explicit module docstring naming PURE-09 compliance; import githubkit + githubkit.exception at top of file"
  - "Test fake-exception construction: bypass __init__ via __new__ + manual attribute set (avoids the httpx.Response dep)"
  - "Test mocking pattern for GitHubClient: assign client._gh = SimpleNamespace(rest=...) instead of mocking GitHubClient itself (lets us exercise the real .rest property)"

requirements-completed: [IO-01]

# Metrics
duration: 23min
completed: 2026-05-19
---

# Phase 02 Plan 01: I/O Foundation — GitHubClient + AppIdentity Resolver Summary

**Thin sync githubkit wrapper (GitHubClient) with SecondaryRateLimitExceeded retry, two-call AppIdentity startup resolver (resolves OQ-1/A1), CorruptSquashError exception, PURE-09 positive lint test, and clean Phase 1 ruff baseline.**

## Performance

- **Duration:** ~23 min
- **Completed:** 2026-05-19
- **Tasks:** 3
- **Files modified:** 9 (1 src created, 1 test created, 1 SUMMARY created, 6 test files cleaned)
- **Commits:** 4 (1 chore + 1 test [RED] + 1 feat [GREEN] + 1 test [PURE-09 positive])

## Accomplishments

- **IO-01 foundation landed.** `rocm_mq.gh` is the only module in the package that touches `githubkit.GitHub`; every other Phase 2+ module will import `GitHubClient` and `resolve_app_identity` from here.
- **OQ-1 / Assumption A1 empirically resolved.** Inspection of githubkit 0.15.5 confirmed `apps.get_authenticated()` returns an `Integration` carrying only `(id, slug)` — no bot-user field. `resolve_app_identity` therefore makes the second `users.get_by_username(f"{slug}[bot]")` call to populate `bot_user_id`. Documented in `gh.py` module docstring.
- **Retry-loop semantics locked.** `SecondaryRateLimitExceeded` (and only that exception) triggers retry; primary rate limits and any other `RequestFailed` subclass propagate immediately. `max_retries=3` cap; sleep uses `e.retry_after.total_seconds()`; tests monkeypatch `rocm_mq.gh.time.sleep` for fast execution.
- **PURE-09 made symmetric.** Phase 1 lint forbade I/O imports from pure modules; this plan adds the matching positive test `test_io_modules_do_import_githubkit` so an accidental pure-fication of `gh.py` / `snapshot.py` / `executor.py` fails loudly. Skips gracefully for not-yet-created modules.
- **Phase 1 lint debt cleared.** 29 pre-existing ruff violations (I001, UP017, C409, F401) in Phase 1 test files fixed; one line was rewritten by hand to drop a 114-char chain. `ruff check .` is now green across the whole `.github/merge-queue/` tree.
- **Runtime deps pinned.** `githubkit>=0.15.5`, `httpx>=0.27`, `PyYAML>=6.0.3` added to `[project.dependencies]` per `RESEARCH.md` standard stack (PyYAML reserved early for Phase 4).

## Task Commits

1. **Task 1: Fix Phase 1 ruff violations + add runtime deps** — `f3bec719080` (chore)
2. **Task 2 (RED): Failing tests for gh.GitHubClient + resolve_app_identity** — `75b11b072b5` (test)
3. **Task 2 (GREEN): Implement gh.py — GitHubClient + resolve_app_identity + CorruptSquashError** — `33eb8984352` (feat) — bundles the Rule-1 fix to `test_anti_pydantic_canary_pydantic_not_imported`
4. **Task 3: PURE-09 positive I/O lint test** — `9b12654780a` (test)

## Files Created/Modified

### Created

- `.github/merge-queue/src/rocm_mq/gh.py` — `GitHubClient`, `request_with_retry`, `resolve_app_identity`, `CorruptSquashError`. ~160 LOC including module docstring + per-method docstrings.
- `.github/merge-queue/tests/test_gh_client.py` — 10 unit tests covering constructor, `.rest` passthrough, retry success/exhaustion/non-rate-limit-passthrough/args-forwarding, `resolve_app_identity` (two-call wiring + bot-login pattern + alternate-slug), and `CorruptSquashError` is `RuntimeError`. Uses `SimpleNamespace` + `MagicMock` to avoid network and httpx.Response construction; bypasses `SecondaryRateLimitExceeded.__init__` via `__new__` + attribute set.

### Modified

- `.github/merge-queue/pyproject.toml` — `[project].dependencies` populated.
- `.github/merge-queue/tests/test_pure_layer_imports.py` — adds `IO_LAYER_MODULES` constant + `test_io_modules_do_import_githubkit` parametrized over (`gh`, `snapshot`, `executor`); ruff auto-fixed one I001.
- `.github/merge-queue/tests/test_state_dataclasses.py` — `test_anti_pydantic_canary_pydantic_not_imported` rewritten to run in a clean subprocess (Rule 1 deviation, see below).
- `.github/merge-queue/tests/test_decide_cycle_unit.py`, `test_derive.py`, `test_pathmap.py`, `test_strategies_and_base_imports.py`, `test_summary_render.py` — ruff auto-fixes for I001 / UP017 / C409 / F401 (Phase 1 lint debt). One line in `test_strategies_and_base_imports.py` rewritten by hand to fit the 100-char limit.

## Decisions Made

- **CorruptSquashError owner = gh.py.** The plan allowed either gh.py or executor.py; gh.py was chosen so all Phase 2 callers import the exception from the same place they import `GitHubClient` (single import line).
- **`Any`-typed `.rest` property.** githubkit's `RestVersionSwitcher` is a private detail; exposing it as `Any` keeps the static surface clean while still letting callers chain `client.rest.apps.get_authenticated()`. githubkit's own typed responses cover the per-call shapes downstream.
- **No retry wrapper on `resolve_app_identity`.** Startup is a single best-effort lookup; failing here should fail the whole cycle rather than mask a misconfigured token. Per-call retry will be added at the snapshot/executor call sites in Plans 02-02 / 02-03.
- **`time.sleep` monkeypatching over fixed-fake injection.** Production and test code paths are identical; tests do `monkeypatch.setattr("rocm_mq.gh.time.sleep", lambda _: None)`. This was the simpler of the two options the plan offered.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug] `test_anti_pydantic_canary_pydantic_not_imported` was order-dependent**

- **Found during:** Task 2 GREEN gate (full suite run after implementing `gh.py`).
- **Issue:** The canary used `importlib.reload(rocm_mq)` then checked `"pydantic" not in sys.modules`. Before Plan 02-01, no test ever imported `rocm_mq.gh`, so the check passed by luck. After this plan, `tests/test_gh_client.py` imports `rocm_mq.gh` → `githubkit` → `pydantic`, leaving `pydantic` in `sys.modules` for the rest of the pytest session and tripping the canary regardless of `rocm_mq` reload behaviour.
- **Fix:** Rewrote the test to spawn a clean Python subprocess that `import rocm_mq` and asserts `pydantic not in sys.modules`. This matches the test's actual contract (importing the public `rocm_mq` surface must not drag pydantic in) without being polluted by other tests.
- **Files modified:** `tests/test_state_dataclasses.py` (also dropped unused `importlib` import; corrected `hasattr(rocm_mq.state, ...)` to `hasattr(state_module, ...)` since `import rocm_mq.state as state_module` does not bind the name `rocm_mq` in scope — pre-existing latent bug that surfaced when removing the `importlib` line).
- **Verification:** Test passes both standalone and as part of the full suite (227+1 tests green).
- **Committed in:** `33eb8984352` (bundled with the GREEN commit because the breakage was caused by the same change — adding `rocm_mq.gh`).

---

**Total deviations:** 1 auto-fixed (1 bug fix)
**Impact on plan:** The fix tightens the canary's enforcement and matches its docstring claim more accurately. No scope creep — same contract, more accurate mechanism.

## Issues Encountered

- **ruff's import sort moved `rocm_mq.gh` into the third-party block.** `rocm_mq` is installed via `pip install -e .` so isort treats it as third-party, not first-party. This is cosmetic and stable across ruff runs; left as-is rather than reconfiguring `known-first-party` because the existing Phase 1 tests already follow the same convention and adding config here would create a §8 self-bootstrap surface for trivial gain.
- **githubkit's `SecondaryRateLimitExceeded.__init__` requires a real `httpx.Response`.** Sidestepped in tests by constructing via `__new__` + manual attribute set (`exc.retry_after = ...`, `exc.response = SimpleNamespace(...)`). Documented in the test file's helper docstrings.

## User Setup Required

None — `actions/create-github-app-token@v3` will mint tokens in the GHA workflow (added in a later plan); no local secrets needed for the Phase 2-01 test suite.

## Next Phase Readiness

- **Ready for Plan 02-02 (snapshot.py).** `GitHubClient` and `resolve_app_identity` are stable imports; `request_with_retry` provides the rate-limit-safe call wrapper that snapshot's per-PR fetches will use.
- **Ready for Plan 02-03 (executor.py).** `CorruptSquashError` is importable from `rocm_mq.gh` for the post-squash parent-SHA check.
- **No blockers.** Lint, mypy (non-strict on `gh.py`), and the full 228-test suite are green.

## Self-Check: PASSED

- File `.github/merge-queue/src/rocm_mq/gh.py` — FOUND
- File `.github/merge-queue/tests/test_gh_client.py` — FOUND
- Commit `f3bec719080` — FOUND (chore Task 1)
- Commit `75b11b072b5` — FOUND (test Task 2 RED)
- Commit `33eb8984352` — FOUND (feat Task 2 GREEN)
- Commit `9b12654780a` — FOUND (test Task 3)

---
*Phase: 02-i-o-layer-executor*
*Completed: 2026-05-19*
