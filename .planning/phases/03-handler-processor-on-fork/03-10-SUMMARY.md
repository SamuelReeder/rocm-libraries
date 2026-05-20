---
phase: 03-handler-processor-on-fork
plan: 10
subsystem: testing
tags: [dogfood, scaffolding, dataclass, frozen, slots, github-actions-artifacts]

# Dependency graph
requires:
  - phase: 02-i-o-layer-executor
    provides: GitHubClient, _RetryProxy wrapping, FakeGitHub fixture vocabulary, RequestFailed shim pattern
  - phase: 03-handler-processor-on-fork (plan 02)
    provides: CONTEXT.md D-04 JSON schema lock + dogfood subpackage placement (D-02)
provides:
  - DogfoodResult frozen-slots dataclass (12-field D-04 JSON contract)
  - create_dogfood_pr (branch-off-develop + commit + open PR)
  - post_command (issue-comment poster, returns comment id)
  - poll_pr_state (bounded polling with required timeout_s, T-03-10-02 mitigation)
  - emit_result (JSON writer to .planning/.../dogfood-runs/ with Windows-safe filename)
  - download_cycle_summary_artifact (zip-extracting artifact fetcher with graceful "" fallback)
affects:
  - 03-11 (dog_02 driver — merge conflict scenario)
  - 03-12 (dog_03 driver — canary CI failure)
  - 03-13 (dog_04 driver — simultaneous /merge idempotency)
  - 03-14 (dog_05 / dog_07 — push between activation/squash, approval revoked)
  - 03-15 (dog_06 — 5-PR worked example)
  - 03-16 (aggregator — reads emit_result JSONs into DOGFOOD-RESULTS.md)
  - phase 5 porting-prep (replay kit JSON schema stability)

# Tech tracking
tech-stack:
  added: [stdlib-only — base64, dataclasses, io, json, secrets, time, zipfile, pathlib]
  patterns:
    - "Frozen @dataclass(frozen=True, slots=True) for on-disk JSON contract (CLAUDE.md mandate; NOT Pydantic)"
    - "tuple-typed timeline + processor_run_urls (Pitfall 11: hashable + immutable)"
    - "Required-keyword-only timeout_s parameter on polling helper (T-03-10-02: no infinite-loop default)"
    - "Graceful '' return on artifact fetch failure (driver does not crash on missing cycle-summary)"
    - "Local FakeGitHub extension (subclass + monkey-attached sub-namespaces) for one-consumer surfaces"

key-files:
  created:
    - .github/merge-queue/src/rocm_mq/dogfood/__init__.py
    - .github/merge-queue/src/rocm_mq/dogfood/_base.py
    - .github/merge-queue/tests/test_dogfood_base.py
  modified: []

key-decisions:
  - "DogfoodResult is frozen + slots per CLAUDE.md frozen-dataclass discipline; timeline/processor_run_urls are tuples (not lists) so the whole instance stays hashable and the JSON serialization sort order is stable"
  - "emit_result sanitizes ':' from ISO timestamps in the filename so the per-run JSON path is Windows-safe (NTFS rejects ':' in filenames) without losing the timestamp's information"
  - "poll_pr_state's timeout_s is REQUIRED keyword (not optional with a default sentinel); explicit opt-in per threat T-03-10-02 — drivers cannot accidentally inherit an infinite loop"
  - "download_cycle_summary_artifact swallows ALL exceptions and returns ''; a missing/failed artifact upload from the processor should not cascade into a driver crash — the JSON simply records an empty excerpt and the operator inspects the GHA run UI"
  - "_DogfoodFake (FakeGitHub extension) lives in tests/test_dogfood_base.py rather than being promoted to tests/gh_fake.py — dogfood is the only consumer today; promotion can happen in a future plan if another module needs the same git/actions surface"
  - "Default output dir for emit_result is .planning/phases/03-handler-processor-on-fork/dogfood-runs/ per CONTEXT.md D-04; tests pass tmp_path to redirect"

patterns-established:
  - "Subpackage I/O-layer scaffolding: __init__.py is a one-line docstring marker; _base.py owns the shared dataclass + helpers; consumers are dog_*.py driver modules"
  - "JSON schema enforcement via dataclass.fields() set comparison in tests — any field-set drift fails the test immediately"
  - "Polling helper monkeypatch idiom: tests setattr both rocm_mq.dogfood._base.time.sleep (no-op) and time.monotonic (fake counter) so timeout tests run in microseconds"

requirements-completed: []

# Metrics
duration: ~25min
completed: 2026-05-20
---

# Phase 3 Plan 10: Dogfood Scaffolding Subpackage Summary

**Frozen-slots DogfoodResult (D-04 schema) + create_dogfood_pr/post_command/poll_pr_state/emit_result/download_cycle_summary_artifact helpers that every DOG-02..DOG-08 driver and the aggregator import from**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-05-20T (plan execution start)
- **Completed:** 2026-05-20
- **Tasks:** 2 (Task 1: dataclass + helpers; Task 2: tests — executed as RED/GREEN gates)
- **Files created:** 3

## Accomplishments

- `rocm_mq.dogfood` subpackage importable; `_base.py` exports `DogfoodResult`, `create_dogfood_pr`, `post_command`, `poll_pr_state`, `emit_result`, `download_cycle_summary_artifact`
- `DogfoodResult` is `@dataclass(frozen=True, slots=True)` with the 12-field D-04 schema (tuple-typed `timeline` + `processor_run_urls` per Pitfall 11)
- `emit_result` writes deterministic JSON to `dogfood-runs/{safe-ts}-{scenario_id}.json` with colon-sanitized filenames (Windows-safe)
- `poll_pr_state` requires `timeout_s` keyword (T-03-10-02 mitigation: no infinite-loop default); uses `time.monotonic` elapsed clock
- `download_cycle_summary_artifact` fetches the `cycle-summary-*` artifact from `mq-processor.yml` uploads (RESEARCH.md Area #11), unzips in-memory, returns first file's UTF-8 content; returns `""` on any failure (no artifact / no match / fetch error / unzip error)
- 21 new tests in `test_dogfood_base.py` cover: frozen-slots invariants, D-04 schema (key-set equality), branch-name regex, end-to-end PR creation against extended FakeGitHub, comment-body roundtrip, polling success / eventual-success / timeout, all four artifact-download branches
- Full test suite still green: **437 / 437 passed**; `test_pure_layer_imports.py` still passes (dogfood subpackage is correctly outside `PURE_LAYER_MODULES`)
- Ruff lint + format clean on new files; mypy clean across all 17 `rocm_mq` source files

## Task Commits

Each task was committed atomically following TDD per the plan's per-task `tdd="true"` discipline:

1. **RED gate (Task 1 + Task 2 tests):** `c7e6d4e` — `test(03-10): add failing tests for dogfood scaffolding`
2. **GREEN gate (Task 1 + Task 2 implementation):** `c5dbbe7` — `feat(03-10): add rocm_mq.dogfood scaffolding subpackage`

**Plan metadata (this SUMMARY):** added next as part of the close-out commit (not committed here — per `git add -f` orchestrator instructions, SUMMARY is force-added separately).

_Note: Tasks 1 and 2 are tightly coupled (Task 1's source is what Task 2's tests validate), so the TDD cycle was executed once across the pair — RED commit contains the full test file; GREEN commit contains both the implementation source and the format-only ruff adjustments to the test file._

## Files Created/Modified

- `.github/merge-queue/src/rocm_mq/dogfood/__init__.py` — subpackage marker; one-line docstring acknowledging I/O-layer status (CONTEXT.md D-02)
- `.github/merge-queue/src/rocm_mq/dogfood/_base.py` — 345 lines: `DogfoodResult` dataclass + 5 helper functions + module docstring documenting the D-04 contract, T-03-10-02 mitigation, and graceful-fallback discipline
- `.github/merge-queue/tests/test_dogfood_base.py` — 21 tests covering all 12 behavior bullets from the plan; local `_DogfoodFake` extends `FakeGitHub` with `git.get_ref` / `git.create_ref` / `repos.create_or_update_file_contents` / `pulls.create` / `actions.list_workflow_run_artifacts` / `actions.download_artifact`

## Decisions Made

- **`DogfoodResult` field shape locked exactly to CONTEXT.md D-04** — no extra fields, no missing fields. The test `test_dogfood_result_is_dataclass_with_d04_fields` asserts the field set equals the frozen `_D04_KEYS` literal; any future drift fails immediately. The same key set is asserted on the emitted JSON in `test_emitted_json_schema_matches_d04_exactly`.
- **`title_prefix` parameter on `create_dogfood_pr`** — added as `title_prefix: str | None = None` so future scenarios (e.g., DOG-03's `[dogfood-ci-fail]` canary marker) can override the default `[dogfood {scenario_id}]` prefix without modifying the helper. Default behavior preserves the existing pattern; test `test_create_dogfood_pr_title_prefix_override` exercises the override path.
- **`_DogfoodFake` stays local to the test file** — promoting to `gh_fake.py` would expand the shared fixture's surface for one consumer. When DOG-* drivers in later plans need additional methods, they can extend the local fake without touching the shared one.
- **`time.monotonic` over `time.time`** — wall-clock jumps (NTP sync, DST) cannot cause `poll_pr_state` to spuriously expire early or hang past the budget. Tests monkeypatch both `time.sleep` and `time.monotonic` so timeout coverage runs in microseconds.

## Deviations from Plan

None — plan executed exactly as written.

The plan's verification block specified:
- `pytest tests/test_dogfood_base.py tests/test_pure_layer_imports.py` → exits 0 (37 passed)
- `python -c "from rocm_mq.dogfood._base import …"` → exits 0 (confirmed)
- `python -c "… dataclass fields == D-04 set"` → exits 0 (confirmed)

All three pass. The plan's `min_lines: 150` artifact constraint on `_base.py` is satisfied (345 lines, well above floor).

Minor formatter-driven adjustments to the test file (line wrapping by `ruff format`) and four lint cleanups (one unused `noqa: BLE001` directive removed since the rule was not enabled in `pyproject.toml`, one `SIM118` `dict.keys()` simplification, one `RUF059` `_pr_url` unused-tuple-unpack rename, one matching `noqa` removal) were applied during the GREEN cycle. These are mechanical / style-only and not deviations from plan content.

## Issues Encountered

- **Local pytest env required `pip install -e ".[dev]" --break-system-packages --user`** — the editable install had been registered against a sibling worktree's path, so the new worktree saw `ModuleNotFoundError: rocm_mq` until the package was re-installed against this worktree's filesystem. Resolved with a single install command. Not a code issue; environmental only.

## User Setup Required

None.

## Next Phase Readiness

Wave 4 plans (`03-11` through `03-16`) can now import the scaffolding:

```python
from rocm_mq.dogfood._base import (
    DogfoodResult,
    create_dogfood_pr,
    post_command,
    poll_pr_state,
    emit_result,
    download_cycle_summary_artifact,
)
```

Each `dog_<scenario>.py` driver becomes a thin scenario-specific module per the per-driver shape in 03-PATTERNS.md `### .github/merge-queue/src/rocm_mq/dogfood/dog_02.py … dog_08.py (test drivers)`. The aggregator in plan 03-16 globs `.planning/phases/03-handler-processor-on-fork/dogfood-runs/*.json` (the path `emit_result` writes to by default) and renders `DOGFOOD-RESULTS.md`.

The D-04 JSON contract is locked from this commit forward — Phase 5 porting-prep can ship the per-run JSONs as the upstream-reviewer replay kit without further schema work.

## Self-Check: PASSED

- `.github/merge-queue/src/rocm_mq/dogfood/__init__.py` — FOUND
- `.github/merge-queue/src/rocm_mq/dogfood/_base.py` — FOUND (345 lines, >= 150 floor)
- `.github/merge-queue/tests/test_dogfood_base.py` — FOUND
- Commit `c7e6d4e111f` (RED gate) — FOUND in `git log`
- Commit `c5dbbe7bf95` (GREEN gate) — FOUND in `git log`
- `pytest tests/test_dogfood_base.py tests/test_pure_layer_imports.py -q` → 37 passed
- `pytest -q` (full suite) → 437 passed
- `ruff check src/rocm_mq/dogfood/ tests/test_dogfood_base.py` → All checks passed
- `mypy src/rocm_mq/` → no issues (17 source files)
- `python3 -c "from rocm_mq.dogfood._base import …"` → 6-symbol import succeeds
- `python3 -c "… dataclass fields == D-04 set"` → schema lock confirmed

---
*Phase: 03-handler-processor-on-fork*
*Completed: 2026-05-20*
