---
phase: 03-handler-processor-on-fork
plan: 04
subsystem: workflow-preflight
tags: [preflight, wf-10, workflow, github-token, cli]
requirements_completed: [WF-10]
dependency_graph:
  requires:
    - .github/merge-queue/src/rocm_mq/gh.py (Phase 2 GitHubClient + _RetryProxy)
    - .github/merge-queue/src/rocm_mq/config.py (load_from_develop pattern; plan 03-02)
    - .github/merge-queue/src/rocm_mq/cmd_process.py (run_preflight NotImplementedError stub; plan 03-01)
    - .github/merge-queue/path_to_queues.yml (loadable artifact; plan 03-03)
  provides:
    - rocm_mq.preflight.main (CLI entry point — workflow-first-step pre-flight check)
    - cmd_process.run_preflight (now dispatches through preflight.main; no longer a NotImplementedError stub)
  affects:
    - .github/workflows/mq-handler.yml (plan 03-07) — will call `python -m rocm_mq preflight` as first step
    - .github/workflows/mq-processor.yml (plan 03-08) — will call `python -m rocm_mq preflight` as first step
tech_stack:
  added: []
  patterns:
    - "Pre-flight runs BEFORE App-token mint, using read-only GITHUB_TOKEN (RFC §8 T-03-04-01)"
    - "Exit-code-2 convention for usage errors mirrors cmd_process.py — Phase 3 workflow tests rely on the distinction"
    - "$GITHUB_STEP_SUMMARY append-mode (`a`) — never truncate prior step content (GHA convention)"
    - "Deferred-import dispatch (run_preflight → preflight.main) — cheap import-cycle invariant guard"
key_files:
  created:
    - .github/merge-queue/src/rocm_mq/preflight.py
    - .github/merge-queue/tests/test_preflight.py
  modified:
    - .github/merge-queue/src/rocm_mq/cmd_process.py (run_preflight body: NotImplementedError → dispatch to preflight.main)
    - .github/merge-queue/tests/test_cmd_process.py (renamed test_main_preflight_stub_raises_not_implemented → test_main_preflight_dispatches_to_module)
decisions:
  - "Task 1: option-a (comprehensive 2-check scope) — Check 1 default-branch + Check 3 path_to_queues loadable; Check 2 App-identity slug-match DEFERRED to processor startup per RESEARCH.md Area #23 nuance"
  - "Catch Exception broadly at Check 3 (githubkit.RequestFailed / httpx transport / base64 decode all collapse to 'file not loadable' for preflight purposes)"
  - "Deferred-import for preflight.main inside run_preflight (matches _build_fake_client pattern; future-proofs against import cycle)"
  - "Re-serialize --repo as flag form (`--repo=owner/repo`) when calling preflight.main — keeps both CLI entrypoints independently usable with parser re-parsing"
metrics:
  duration: "~25 min (TDD RED/GREEN/REFACTOR x 2 tasks)"
  tasks_completed: 3
  tests_added: 10
  commits: 4
  completed: 2026-05-19
---

# Phase 3 Plan 04: Pre-flight Check (preflight.py + cmd_process wire-through) Summary

## One-Liner

Comprehensive 2-check pre-flight (`rocm_mq.preflight.main`) for fork default-branch + `path_to_queues.yml` loadability using read-only `GITHUB_TOKEN`, wired through `cmd_process.run_preflight` from its plan 03-01 NotImplementedError stub — runs as the first workflow step before App-token mint to bound the misconfig blast radius (RFC §8 T-03-04-01).

## What Was Built

### `rocm_mq/preflight.py` (new module, ~215 lines)

Implements the comprehensive pre-flight per plan 03-04 Task 1 option-a:

- **Check 1 (WF-10):** `default_branch == "develop"` via `client.rest.repos.get(owner, repo)`.
- **Check 3:** `.github/merge-queue/path_to_queues.yml` loadable from `develop` ref via `client.rest.repos.get_content(..., ref="develop")`.
- **Check 2 (App-identity slug-match):** DEFERRED to processor startup per RESEARCH.md Area #23 nuance — `apps.get_authenticated` requires an App-token, but preflight runs BEFORE App-token mint. The wrong-App misconfig surfaces passively as the first processor failure via the existing `resolve_app_identity` call inside `process_cycle`.

Exit codes match `cmd_process.py` discipline:
- 0 — all checks pass
- 1 — Check 1 or Check 3 failed (structured `preflight FAILED:` stderr + `## Pre-flight FAILED` section appended to `$GITHUB_STEP_SUMMARY`)
- 2 — usage error (`--repo` missing/malformed or `GITHUB_TOKEN` unset)

`$GITHUB_STEP_SUMMARY` writes use `"a"` (append) mode and never truncate prior step content.

### `cmd_process.run_preflight` rewired (Task 3)

Replaces the plan 03-01 stub body:

```python
raise NotImplementedError("rocm_mq preflight: plan 03-04 wires preflight.main ...")
```

with a deferred-import dispatch:

```python
from rocm_mq.preflight import main as _preflight_main
return _preflight_main([f"--repo={args.repo}"])
```

The re-serialization (`--repo=...`) lets the preflight CLI re-parse argv so the two entrypoints (`python -m rocm_mq preflight` via subcommand routing and a direct invocation of `preflight.main`) stay independently usable with the same flag surface.

## Tests

10 new tests in `tests/test_preflight.py`:

- `test_preflight_module_importable` — smoke
- `test_main_happy_path_returns_zero` — both checks pass; happy stderr line emitted
- `test_main_wrong_default_branch_returns_one` — Check 1 fails; short-circuits before Check 3
- `test_main_path_to_queues_unloadable_returns_one` — Check 3 fails; exception repr embedded
- `test_main_no_repo_returns_two` — usage error: no `--repo`/`$GITHUB_REPOSITORY`
- `test_main_malformed_repo_returns_two` — usage error: `--repo` without slash
- `test_main_missing_github_token_returns_two` — usage error: no `GITHUB_TOKEN`
- `test_failure_appends_to_step_summary_in_append_mode` — **sentinel-survival**: pre-populated `$GITHUB_STEP_SUMMARY` content survives the append
- `test_step_summary_not_required_when_env_unset` — local-dev parity (unset env var is non-fatal)
- `test_main_repo_defaults_to_github_repository_env` — `$GITHUB_REPOSITORY` fallback

Plus 1 test renamed (Task 3):
- `test_main_preflight_stub_raises_not_implemented` → `test_main_preflight_dispatches_to_module` — now asserts successful dispatch into `preflight.main` against a FakeGitHub-backed happy path; verifies the absence of `NotImplementedError` in stderr and the presence of preflight's `"preflight passed"` log line.

**Test patching pattern:** the base `FakeGitHub._ReposNS` does not model `repos.get` (metadata) or `repos.get_content`. Tests extend the fake with per-test helpers (`_patch_repos_get_default_branch`, `_patch_repos_get_content_ok`, `_patch_repos_get_content_raises`) — same pattern used in `tests/test_config.py::_seed_contents` for plan 03-02's loader tests.

**Total test count:** 355 passed (up from 345 before this plan).

## Verification

All `must_haves.truths` from the plan frontmatter satisfied:

| Truth | Verification |
|---|---|
| `python -m rocm_mq preflight --repo OWNER/REPO` exits 0 when default_branch == 'develop' AND path_to_queues.yml loadable | `test_main_happy_path_returns_zero` (also covered by `test_main_preflight_dispatches_to_module` end-to-end through `cmd_process.main`) |
| preflight exits non-zero with structured stderr `preflight FAILED: default_branch=<x>, expected develop` when default branch differs | `test_main_wrong_default_branch_returns_one` asserts the literal substring |
| preflight exits non-zero with structured stderr `preflight FAILED: PATH_TO_QUEUES not loadable from develop: <repr>` when contents API errors | `test_main_path_to_queues_unloadable_returns_one` asserts `"PATH_TO_QUEUES not loadable"` AND the exception `repr()` substring |
| preflight writes a `## Pre-flight FAILED` section to $GITHUB_STEP_SUMMARY on every failure path (append mode, never truncate) | `test_failure_appends_to_step_summary_in_append_mode` — sentinel pre-populated + asserted to survive after append |
| PRE-CONFIRM: scope is 3-check comprehensive default per CONTEXT.md (Check 2 deferred per RESEARCH amendment) | Recorded in Decisions; reflected in `preflight.py` module docstring |

Smoke-test (manual): `GITHUB_TOKEN= python -m rocm_mq preflight --repo owner/repo` → exits 2 with the expected `GITHUB_TOKEN` usage-error stderr. No `NotImplementedError` trace — wire-through confirmed working.

## Threat Model Coverage (plan frontmatter)

| Threat | Disposition | Coverage |
|---|---|---|
| T-03-04-01 (EoP — preflight runs before App-token mint) | mitigate | By design: `preflight.main` invoked via workflow `run:` step BEFORE `actions/create-github-app-token@v3` step (plan 03-07/03-08 will declare this ordering). Module docstring records the discipline. |
| T-03-04-02 (Tampering — $GITHUB_STEP_SUMMARY write race) | accept | Append-mode used; per-job step serialization is GHA-enforced. |
| T-03-04-03 (DoS — API throttling) | accept | `_RetryProxy` from `gh.py` wraps every `client.rest.*` call automatically (Phase 2 WR-01 fix). |
| T-03-04-04 (Info disclosure — default branch name leak) | accept | Default branch is public information. |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking lint] Ruff `BLE001` noqa suppression**
- **Found during:** Task 2 GREEN initial ruff pass
- **Issue:** I added `# noqa: BLE001 — see docstring above` on the `except Exception as exc:` line at preflight.py Check 3. Ruff config (`pyproject.toml`) does NOT enable `BLE` rules in `select`, so the suppression was unused → ruff reported `RUF100` "Remove unused noqa directive".
- **Fix:** Removed the unused noqa directive; kept the docstring rationale explaining why broad `except Exception` is intentional (Contents API + httpx + base64 all collapse to "file not loadable" for preflight purposes).
- **Files modified:** `.github/merge-queue/src/rocm_mq/preflight.py`
- **Commit:** `6ba08e3edc5` (rolled into GREEN commit alongside ruff format pass)

**2. [Rule 3 — Blocking lint] Ruff `I001` import order in test_cmd_process.py**
- **Found during:** Task 3 full-suite lint pass
- **Issue:** My added test had `from types import SimpleNamespace` before `import base64 as _b64` inside the function body — ruff prefers stdlib `import base64` before the deeper `from types import` per its sorting policy.
- **Fix:** Swapped the two import lines.
- **Files modified:** `.github/merge-queue/tests/test_cmd_process.py`
- **Commit:** `e2323803ece` (rolled into GREEN commit)

### Out-of-Scope Discoveries (not fixed)

**Pre-existing ruff format drift in `cmd_process.py` and many other files.** Running `ruff format --check src/ tests/` after my edits reported 29 unrelated files needing reformat — long-line splits, dict-string-quoting choices, etc. None of the drift hunks overlap my added/modified lines. Per executor scope-boundary, these are out of scope for plan 03-04. Not logged to `deferred-items.md` (these are cosmetic, would be picked up by any future ruff-format-on-CI hook).

## Known Stubs

None. The two-check preflight is complete per option-a scope. The deferred Check 2 (App-identity) is intentional design (see Decisions), not a stub — it is covered passively by `resolve_app_identity` inside the processor's `process_cycle` (Phase 2, already shipping).

## Self-Check: PASSED

**Files exist:**
- `[FOUND]` `.github/merge-queue/src/rocm_mq/preflight.py`
- `[FOUND]` `.github/merge-queue/tests/test_preflight.py`
- `[FOUND]` `.github/merge-queue/src/rocm_mq/cmd_process.py` (modified — `run_preflight` body replaced)
- `[FOUND]` `.github/merge-queue/tests/test_cmd_process.py` (modified — preflight stub test renamed)

**Commits exist:**
- `[FOUND]` `ddf406cac58` — `test(03-04): add failing tests for preflight.main (RED)`
- `[FOUND]` `6ba08e3edc5` — `feat(03-04): implement rocm_mq.preflight comprehensive 2-check (GREEN)`
- `[FOUND]` `f5c46c79ee9` — `test(03-04): assert preflight subcommand dispatches to preflight.main (RED)`
- `[FOUND]` `e2323803ece` — `feat(03-04): wire cmd_process.run_preflight to preflight.main (GREEN)`

**Tests pass:**
- `pytest tests/test_preflight.py -q` → 10 passed
- `pytest tests/test_cmd_process.py -q` → 23 passed
- Full suite: `pytest -q` → 355 passed (was 345 before this plan)

**Lint/type checks:**
- `ruff check` on touched files → clean
- `mypy src/rocm_mq/preflight.py src/rocm_mq/cmd_process.py` → clean

**TDD Gate Compliance:**
- Task 2: RED commit (`ddf406c`) → GREEN commit (`6ba08e3`) → no refactor needed (linter auto-fixes rolled into GREEN per scope-boundary).
- Task 3: RED commit (`f5c46c7`) → GREEN commit (`e232380`) → no refactor needed.
- Both gates emitted in correct sequence.
