---
phase: 03-handler-processor-on-fork
plan: 16
subsystem: dogfood
tags: [dogfood, aggregator, verification-artifact, phase-3, rfc-§6]
dependency_graph:
  requires:
    - 03-10 (DogfoodResult + emit_result schema in _base.py)
    - 03-11..03-15 (driver subpackage scenarios DOG-02..DOG-08)
  provides:
    - rocm_mq.dogfood.aggregator (collect_runs / latest_passing / render_markdown / main)
    - .planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md (Phase 3 verification artifact)
  affects:
    - Phase 3 success criterion 4 (every RFC §6 scenario ejects with documented reason — DOGFOOD-RESULTS.md aggregates the evidence)
    - Phase 5 PORT-02 (porting-prep replay kit reads same per-run JSONs)
tech-stack:
  added: []
  patterns:
    - stdlib-only (json + argparse + pathlib + sys) — no new deps
    - "renderer mirrors summary.py shape: module-level constants + per-section private helpers + public render function"
    - "graceful-skip JSON tolerance per T-03-16-01 (logged-to-stderr, not raised)"
key-files:
  created:
    - .github/merge-queue/src/rocm_mq/dogfood/aggregator.py
    - .github/merge-queue/tests/test_dogfood_aggregator.py
    - .planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md
  modified: []
decisions:
  - "Aggregator returns 0 + stderr warning (not non-zero) when all scenarios are 'not yet run' — PRE-CONFIRM from 03-16-PLAN.md Task 1: this state is legitimate before Wave 4 drivers exercised; failing exit would block CI on the empty initial state."
  - "Per-run JSON tolerance: malformed JSON, non-object top-level, or missing scenario_id are logged + skipped (not raised). This keeps a single corrupt artifact from blocking the rest of the aggregation."
  - "Output is overwrite-on-each-run, idempotent. Operators re-run after each driver verification; the renderer reads disk, doesn't accumulate state."
  - "Source JSON paths are stamped onto the run dict via a synthetic '_source_path' key so the renderer can produce 'dogfood-runs/<file>' markdown links without re-walking the directory."
  - "Cleanup driver (rocm_mq.dogfood.cleanup) is DEFERRED to Phase 5 per CONTEXT.md deferred list (re-confirmed in earlier plans)."
metrics:
  duration_min: 12
  tasks_completed: 3
  files_changed: 3
  lines_added: 828
  completed_date: 2026-05-20
---

# Phase 03 Plan 16: Dogfood Aggregator Summary

**One-liner:** Stdlib-only aggregator (`collect_runs` → `latest_passing` → `render_markdown` → `main`) that turns per-scenario dogfood JSONs into the Phase 3 verification artifact `DOGFOOD-RESULTS.md`, plus an initial all-pending render of the file itself.

## What was built

### Aggregator module — `.github/merge-queue/src/rocm_mq/dogfood/aggregator.py` (420 lines)

Four public surfaces:

1. **`collect_runs(input_dir: Path) → dict[scenario_id, list[run_dict]]`** —
   globs `input_dir/*.json`, json.loads each, groups by `scenario_id`. Tolerates
   nonexistent input dir (returns `{}`), malformed JSON (stderr warning, skip),
   non-object top-level (skip), and missing `scenario_id` (skip). Stamps a
   synthetic `_source_path` onto each parsed run so the renderer can link back
   to the source file.

2. **`latest_passing(by_scenario) → dict[scenario_id, run_dict | None]`** —
   for each scenario picks `max(run_ended_at)` where `passed=True`; scenarios
   with zero passing runs map to `None`. ISO-8601 lexicographic sort is safe
   because every driver emits `+00:00` per CONTEXT.md D-04.

3. **`render_markdown(latest, *, scenarios=DEFAULT_SCENARIOS) → str`** —
   produces the full markdown body: top-level `# DOGFOOD-RESULTS` header,
   one-line synopsis, table of contents linking each scenario, one `## DOG-XX
   — {RFC §6 row}` section per scenario in DOG-02..DOG-08 order. Missing
   scenarios render as **Status: not yet run** with a pointer to the driver
   module. Passing scenarios show latest pass timestamp, PR URL, expected vs
   observed outcome, link to source JSON, and optional notes. Uses HTML
   anchor tags so TOC links are stable regardless of GitHub's heading
   slugification of em dashes / parentheses.

4. **`main(argv=None) → int`** — argparse for `--input` and `--output`,
   falls back to module-level `DEFAULT_INPUT_DIR` /
   `DEFAULT_OUTPUT_MD` (monkeypatch-friendly per `test_main_default_paths_smoke`).
   Wraps the pipeline in a `try/except` mirroring `cmd_process.main`. Always
   ensures all seven `DEFAULT_SCENARIOS` appear in the rendered output even
   if no JSONs exist on disk. Logs `passing_count/total` to stderr (or a
   warning when count is zero). Exit 0 on success; 1 on uncaught exception.

Module-level constants:
- `DEFAULT_INPUT_DIR = .planning/phases/03-handler-processor-on-fork/dogfood-runs`
- `DEFAULT_OUTPUT_MD = .planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md`
- `DEFAULT_SCENARIOS = (dog_02, …, dog_08)` — RFC §6 row order
- `SCENARIO_RFC_MAP` — per-scenario RFC §6 row label rendered in each section
  heading; gives the artifact reader the RFC anchor without lookup.

PURE-09: I/O layer; not added to `PURE_LAYER_MODULES`. Imports only stdlib
(`json`, `argparse`, `pathlib`, `sys`).

### Tests — `.github/merge-queue/tests/test_dogfood_aggregator.py` (337 lines, 18 tests)

Coverage:
- `collect_runs`: empty dir, nonexistent dir, group-by-scenario_id across
  multiple JSONs, malformed-JSON skip, missing-`scenario_id` skip.
- `latest_passing`: picks max `run_ended_at` among passing, ignores newer
  failed runs in favor of older passing ones, returns `None` when only
  failed runs exist, empty-input → empty dict.
- `render_markdown`: one section per requested scenario, 'not yet run' for
  missing, every scenario heading carries its `SCENARIO_RFC_MAP` label,
  passing section contains timestamp / PR URL / outcomes / JSON link,
  top-level `# ` header.
- `main`: `--input`/`--output` writes to chosen path, default-paths smoke
  via monkeypatch on `DEFAULT_INPUT_DIR` / `DEFAULT_OUTPUT_MD`, exit 0
  when no runs yet (PRE-CONFIRM rationale documented in the test
  docstring).

Fixture pattern: `_make_result()` builds a minimal `DogfoodResult` with
defaults; `_write_run()` mirrors `_base.emit_result` filename shape
(`{safe_ts}-{scenario_id}.json` with colons → dashes) for D-04 schema
parity. Uses `tmp_path` for filesystem isolation; uses `monkeypatch.setattr`
on the module-level path constants for the default-paths smoke test.

### Verification artifact — `.planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md` (71 lines)

Generated by running `python -m rocm_mq.dogfood.aggregator` against the
empty `dogfood-runs/` directory. All seven DOG-02..DOG-08 sections render
as **Status: not yet run** with pointers to the driver modules. The file
is the Phase 3 verification artifact per CONTEXT.md D-04 and gets
regenerated idempotently on every driver verification.

Force-added with `git add -f` because `.planning/` is gitignored.

## Verification

- `pytest tests/test_dogfood_aggregator.py -v` → **18/18 passed** in 0.22s.
- `pytest tests/test_pure_layer_imports.py -q` → **16/16 passed** (aggregator
  correctly excluded from PURE_LAYER_MODULES).
- `ruff check src/rocm_mq/dogfood/aggregator.py tests/test_dogfood_aggregator.py`
  → **All checks passed.**
- `python -m rocm_mq.dogfood.aggregator …` → **exit 0** with expected
  stderr warning; output file written; `head -40 | grep '^# \\|^## '` shows
  the top-level `# DOGFOOD-RESULTS` header followed by `## Scenarios` and
  each `## DOG-XX —` section.
- All three must-haves artifacts on disk:
  - `.github/merge-queue/src/rocm_mq/dogfood/aggregator.py` ≥ 80 lines: ✅ 420 lines
  - `.github/merge-queue/tests/test_dogfood_aggregator.py` contains
    `test_aggregator_groups_by_scenario_id` analog: ✅
    `test_collect_runs_groups_by_scenario_id`
  - `.planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md`
    contains `DOG-06`: ✅

## Commits

| Phase | Commit | Description |
| ----- | ------ | ----------- |
| RED   | `25eac0cabbd` | `test(03-16): add failing tests for dogfood aggregator (RED)` |
| GREEN | `6f4736af743` | `feat(03-16): implement dogfood aggregator (GREEN)` |
| DOCS  | `0ec9df941af` | `docs(03-16): seed DOGFOOD-RESULTS.md with initial all-pending state` |

RED→GREEN gate sequence verified in git log (TDD compliance).

## Deviations from Plan

None — plan executed exactly as written. The plan's "PRE-CONFIRMED" decision
(exit 0 + warning rather than fail when all scenarios are 'not yet run') was
implemented as specified and is enforced by `test_main_exits_zero_when_no_runs_yet`.

The plan called for separate Task 1 (aggregator) and Task 2 (tests) commits;
the TDD flow naturally produced RED (tests) → GREEN (impl) commits in the
opposite order, which is the canonical TDD shape per `<tdd_execution>` in
the executor reference. Both commits land per the plan's intent; the file
set on disk matches exactly.

## Threat Flags

None — no new network endpoints, auth paths, file access patterns at trust
boundaries, or schema changes beyond what plans 03-10..03-15 already declared.
The aggregator consumes JSONs the dogfood drivers (trusted-by-construction
producers) wrote under `.planning/`; it does not call any GitHub APIs and
emits dev-only markdown.

## Known Stubs

None. Every code path in the aggregator is wired to real data; the
'not yet run' rendering is the documented graceful behavior for the
empty-state case, not a stub. Operators will see real timestamps, PR
URLs, and outcomes once Wave 4 drivers run on the fork.

## Operator Usage

From `.github/merge-queue/`:

```bash
# Default paths (writes to .planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md)
python -m rocm_mq.dogfood.aggregator

# Explicit paths (useful from a different cwd or for ad-hoc inspection)
python -m rocm_mq.dogfood.aggregator \
    --input  ../../.planning/phases/03-handler-processor-on-fork/dogfood-runs \
    --output ../../.planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md
```

Re-run after every driver verification — the renderer is idempotent.

## Self-Check: PASSED

Files verified to exist:
- FOUND: `.github/merge-queue/src/rocm_mq/dogfood/aggregator.py`
- FOUND: `.github/merge-queue/tests/test_dogfood_aggregator.py`
- FOUND: `.planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md`

Commits verified to exist:
- FOUND: `25eac0cabbd` (RED)
- FOUND: `6f4736af743` (GREEN)
- FOUND: `0ec9df941af` (DOCS — DOGFOOD-RESULTS.md seed)
