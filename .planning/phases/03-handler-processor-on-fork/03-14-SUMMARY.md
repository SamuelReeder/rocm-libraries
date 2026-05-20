---
phase: 03-handler-processor-on-fork
plan: 14
subsystem: testing
tags: [dogfood, dog_06, rfc-4.2, worked-example, tree-diff-status, silent-corruption, ordering-invariant, marquee]

# Dependency graph
requires:
  - phase: 02-i-o-layer-executor
    provides: _verify_squash Phase B (compare_commits tree-diff sanity) — the closure DOG-06 verifies non-regression of (Phase 2 SC#3, commits 28b4024..ca8720e + WR-hardening 52618a8..350715b)
  - phase: 03-handler-processor-on-fork
    provides: dogfood _base.py scaffolding (plan 03-10) — DogfoodResult / create_dogfood_pr / poll_pr_state / emit_result / download_cycle_summary_artifact
  - phase: 03-handler-processor-on-fork
    provides: dog_02..dog_05, dog_08 driver shape established (plans 03-11..03-13) — dog_06 follows the same two-mode pattern with multi-PR aggregation extensions
provides:
  - DOG-06 marquee 5-PR RFC §4.2 worked-example driver (rocm_mq.dogfood.dog_06)
  - PR_DESCRIPTORS frozen tuple pinning per-PR path → queue-membership mapping for the RFC §4.2 narrative
  - Two post-hoc timeline invariant assertions — RFC §4.3 binding (active precedes squash per PR) and Phase 2 SC#3 closure (tree_diff_status='ahead' per squash)
  - Single-JSON output shape carrying per-PR sub-outcomes nested under observed_outcome.per_pr — primary pr_url is PR_A's; pr_url_list_str in notes catalogues all 5
  - Phase 5 porting-prep replay reference artifact (the dog_06.json that the upstream reviewer reads)
affects:
  - 03-16 (dogfood aggregator) — must read dog_06 JSON's observed_outcome.per_pr nested shape (different from other DOG-* scenarios' flat observed_outcome) and the tree_diff_invariant_violations field
  - Phase 5 porting-prep (PORT-02 ops runbook + PORT-03 evidence pack) — dog_06 JSON IS the canonical replay reference; pr_url + processor_run_urls list MUST stay stable across any later refactor

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Multi-PR dogfood orchestration with global polling predicate (fires when ALL N PRs satisfy a per-PR predicate)"
    - "Two-tier tree_diff_status extraction (squash status comment body first, cycle-summary artifact fallback) — robust to comment-renderer drift"
    - "Post-hoc timeline invariant assertions over an ordered tuple of (ts, event_type, observed_state) events — RFC §4.3 binding + Phase 2 SC#3 tree-diff sanity"
    - "Per-PR descriptor as frozen-slots dataclass — paths derived from path_to_queues.yml so RFC §4.2 queue-membership narrative is materialized declaratively, not hardcoded"
    - "Empty-string sentinel for tree_diff_status absence — flips invariant assertion to False so silent-corruption (Apr-2026 D-05 pattern) surfaces in JSON evidence"

key-files:
  created:
    - .github/merge-queue/src/rocm_mq/dogfood/dog_06.py
    - .github/merge-queue/tests/test_dogfood_dog_06.py
  modified: []

key-decisions:
  - "Each PR descriptor carries a tuple of paths but run_scenario opens the PR at the FIRST path only — single-file PR shape matches the other DOG-* drivers and avoids requiring a multi-file commit helper. The RFC §4.2 queue-membership story holds as long as the PR's changed-file set intersects at least the documented opted-in path."
  - "PR_E uses dnn-providers/miopen-provider/ (same queue as PR_B) — selected over a fresh provider (e.g., fusilli) because the RFC §4.2 narrative beat is 'E touches a provider in the same queue as something earlier' (queue reuse), not 'E touches a different provider'. fusilli-provider was deliberately reserved as unused so a future DOG-* scenario can engage it without conflicting with dog_06's worked-example shape."
  - "Two-tier tree_diff_status extraction (squash status comment body PRIMARY, cycle-summary artifact FALLBACK) — the comment body is the lowest-latency source and matches the renderer-emitted substring directly; the artifact is the structured-data fallback per RESEARCH.md Area #11. Either source returning 'ahead' is sufficient; both absent flips the invariant to False (silent-corruption signal)."
  - "Per-PR sub-outcomes nested under observed_outcome.per_pr (rather than five top-level DogfoodResult instances) — keeps the single-JSON shape per CONTEXT.md D-04 (one file per scenario) while still surfacing per-PR diagnostic detail. Top-level pr_number/pr_url is PR_A's; all five URLs are catalogued in result.notes for human reading."
  - "Invariant assertion violations do NOT raise — they record into observed_outcome.{ordering,tree_diff}_invariant_violations and flip passed=False. Rationale: the per-run JSON IS the evidence pack; a raise would lose the diagnostic context the reviewer needs to see ('which PR's tree_diff was missing?')."
  - "TIMEOUT_S=3600 (60 min) hard-pinned per plan 03-14 must_haves — does NOT auto-scale with PR count. Matches RESEARCH.md Area #10's '4 serial squashes through real CI' budget; the parallel B+C beat shaves wall time but PR_A through E + processor cycle latency still consume the full hour in the worst case."

patterns-established:
  - "Multi-PR dogfood global polling: poll_all_prs_to_squash() fires only when EVERY PR in pr_records has a squash-section status comment; per-PR predicate is encapsulated in _observe_pr() returning a _PRObservation frozen-slots snapshot"
  - "Comment-section detection by literal section-marker substring ('Squashed', 'Active') — robust to surrounding markdown/wording drift; same shape the other DOG-* drivers use for eject reasons"
  - "Run URL extraction by 'actions/runs/' needle + whitespace-boundary walk — works against the GHA run URL shape regardless of surrounding text (markdown links, parentheses, bold)"
  - "Timeline assembly with two-phase ordering — Phase 1 emits all enqueue events for all PRs in A..E order; Phase 2 emits per-PR activation+squash events in A..E narrative order so the timeline reads like the RFC §4.2 story top-to-bottom"

requirements-completed: [DOG-06]

# Metrics
duration: 40min
completed: 2026-05-20
---

# Phase 3 Plan 14: DOG-06 5-PR RFC §4.2 Worked-Example Driver Summary

**Marquee 5-PR dogfood driver that replays RFC §4.2 (A→B+C parallel→D→E) end-to-end, asserts the RFC §4.3 active-before-squash binding invariant + Phase 2 SC#3 tree_diff_status='ahead' invariant from a single ordered timeline, and surfaces Apr-2026 silent-corruption regressions even without a deliberate scenario.**

## Performance

- **Duration:** ~40 min (TDD: tests first → implementation → ruff/mypy/full-suite verification)
- **Started:** 2026-05-20 (work session)
- **Completed:** 2026-05-20
- **Tasks:** 1 of 2 complete (Task 1 unit-test mode; Task 2 is the operator-initiated 60-min live-fork checkpoint — deferred per execution objective "Do NOT live-run. Unit tests only.")
- **Files modified:** 2 (1 created src, 1 created tests)

## Accomplishments

- `dog_06.py` module implementing the 5-PR worked-example driver with PR_DESCRIPTORS frozen-slots tuple pinning per-PR paths to RFC §4.2 queue-membership semantics
- Global polling loop `_poll_all_prs_to_squash` that fires only when every PR carries a squash status comment — required for the parallel B+C beat
- Two-tier tree_diff_status extractor (`_extract_tree_diff_status`) with comment-body primary source + cycle-summary artifact fallback
- Two post-hoc invariant assertions (`_assert_ordering_invariant` + `_assert_tree_diff_invariant`) walking the timeline once each
- Single DogfoodResult emission per CONTEXT.md D-04 schema; per-PR detail nested under `observed_outcome.per_pr`
- 15 unit tests covering: scenario identity, timeout pin, PR descriptor shape & path routing, happy-path pass, D-04 schema field coverage, per-PR squash event presence, tree_diff='ahead' invariant, ordering invariant, processor_run_urls dedup, **silent-corruption detection** (PR_A's tree_diff missing → passed=False), timeout on PR-never-activates, B+C parallel-merge ordering
- Full test suite remains green: **497 passed** (15 new + 482 prior); ruff clean; mypy clean on dog_06.py

## Task Commits

1. **Task 1 (TDD RED):** test failures pinning the contract — `3a621d3b312` (test)
2. **Task 1 (TDD GREEN):** dog_06.py implementation — `c78a0d46f3f` (feat)

Task 2 (live-fork manual verification, ~60 min wall) is a `checkpoint:human-verify` gate — explicitly deferred per the execution objective's "Do NOT live-run. Unit tests only" directive. The checkpoint becomes runnable once the Phase 3 handler+processor workflows are deployed to the fork's `develop` and the App installation is live (plans 03-05..03-09 prerequisite chain).

## Files Created/Modified

- `.github/merge-queue/src/rocm_mq/dogfood/dog_06.py` (created, ~570 lines) — 5-PR orchestration driver
- `.github/merge-queue/tests/test_dogfood_dog_06.py` (created, ~615 lines) — 15 unit tests against a FakeGitHub extension that simulates the 5-PR lifecycle

## JSON timeline narrative

The single per-run JSON's `timeline` field reads top-to-bottom as the RFC §4.2 worked example. Side-by-side excerpts:

| RFC §4.2 narrative beat | dog_06.json timeline excerpt (event_type, observed_state.letter) |
|---|---|
| "PR A is opted into queues for hipDNN core and integration-tests…" | `pr_opened` (A), `merge_command_posted` (A), `mq_queued_label_applied` (A) — followed by the same triple for B, C, D, E in order |
| "PR A is at the head of every queue it belongs to; the processor activates A." | `activation_began` (A), `merge_queue_active_status_posted` (A), `mq_active_label_applied` (A) |
| "Required checks pass; A squash-merges into develop." | `squash_merge_completed` (A) with `observed_state.tree_diff_status: 'ahead'`, `mq_merged_label_applied` (A) |
| "PR B and PR C are both at the head of their (disjoint) queues; both can proceed." | `activation_began` (B), `merge_queue_active_status_posted` (B), `mq_active_label_applied` (B), `squash_merge_completed` (B), `mq_merged_label_applied` (B) — then the same five-event block for C |
| "After B and C merge, PR D (integration-tests) becomes head of all its queues." | `activation_began` (D), `merge_queue_active_status_posted` (D), `mq_active_label_applied` (D), `squash_merge_completed` (D), `mq_merged_label_applied` (D) |
| "Finally, PR E (same queue as B) becomes eligible and merges." | `activation_began` (E), `merge_queue_active_status_posted` (E), `mq_active_label_applied` (E), `squash_merge_completed` (E), `mq_merged_label_applied` (E) |

Note that the timeline emits the parallel B+C beat as two sequential five-event blocks rather than interleaved events. The decision is deliberate: the timeline's purpose is narrative-readability (top-to-bottom RFC §4.2 trace), not microsecond chronology. The genuine parallelism is captured in the `processor_run_urls` field where B and C share the same `actions/runs/...` URL (the cycle that activated and squashed both). Interleaving the events would obscure the narrative without adding diagnostic value.

## Decisions Made

See the `key-decisions:` frontmatter list above for the canonical record. The five most-load-bearing decisions:

1. **Single-file PRs per descriptor** (paths tuple kept for future extension; current impl uses paths[0]). Matches DOG-02..DOG-05 shape.
2. **PR_E uses miopen-provider** (same queue as PR_B) per the "E touches a previously-used queue" RFC §4.2 beat — fusilli-provider deliberately left untouched for future drivers.
3. **Two-tier tree_diff_status extraction** (comment body primary, artifact fallback) — comment is lowest-latency; artifact is the structured fallback per RESEARCH.md Area #11.
4. **Per-PR sub-outcomes nested under observed_outcome.per_pr** — single JSON per CONTEXT.md D-04 + per-PR detail surfaces in `result.notes`.
5. **Invariant violations record-and-continue** (do not raise) so the JSON evidence carries the diagnostic context the reviewer needs.

## Deviations from Plan

None - plan executed exactly as written.

The plan's `<verify><automated>` line was satisfied directly (`SCENARIO_ID == 'dog_06'`, `TIMEOUT_S == 3600`). The plan's "extract tree_diff_status field from each per-PR step summary into the squash_merge_completed event's observed_state" instruction was implemented as a two-tier extractor (comment body → artifact fallback) which matches the spirit of the must_have ("captured from processor `$GITHUB_STEP_SUMMARY`") without restricting the source — the comment body IS a copy of the relevant step-summary line per the renderer convention, and the artifact remains the authoritative fallback.

## Issues Encountered

- **Initial ruff E501 + F841 violations** in first-pass implementation. Resolved by splitting a long string across multiple lines and removing two dead-code lines from the silent-corruption test fixture. Sub-minute resolution; no behavior change.
- **mypy clean** on first pass (the frozen-slots dataclasses + explicit `dict[str, Any]` types matched mypy --strict expectations without adjustment).

## User Setup Required

None — DOG-06 is a fork-runnable driver. Operator-initiated live-fork verification (the deferred Task 2 checkpoint) requires only `GITHUB_TOKEN` exported and the Phase 3 workflows deployed to `develop` on the fork. See the dog_06 module docstring's "Operational note" for the manual-replay recovery procedure if the driver exceeds TIMEOUT_S due to real-CI variability.

## Next Phase Readiness

- Plan 03-15 (DOG-07 approval-revoked driver) can proceed — DOG-06's per-PR lifecycle pattern + the global-polling-predicate primitive translate directly.
- Plan 03-16 (dogfood aggregator + DOGFOOD-RESULTS.md) must be aware that dog_06's JSON shape differs from DOG-02..05/DOG-08 — `observed_outcome` is a nested dict with `per_pr` sub-outcomes rather than a flat `{action, reason}` shape. The aggregator's per-scenario section renderer needs a small branch for dog_06.
- Phase 5 porting-prep (PORT-02 / PORT-03) can cite `dog_06.json` as the marquee evidence artifact; the schema is stable as long as the D-04 12-field shape + the `observed_outcome.per_pr` nested keys (`pr_number`, `pr_url`, `state`, `active_run_url`, `squash_run_url`, `tree_diff_status`) remain unchanged.

## Self-Check: PASSED

- `dog_06.py` exists at `.github/merge-queue/src/rocm_mq/dogfood/dog_06.py` — FOUND
- `test_dogfood_dog_06.py` exists at `.github/merge-queue/tests/test_dogfood_dog_06.py` — FOUND
- Commit `3a621d3b312` (RED) — FOUND in git log
- Commit `c78a0d46f3f` (GREEN) — FOUND in git log
- 15 dog_06 tests pass; full 497-test suite green; ruff + mypy clean

---
*Phase: 03-handler-processor-on-fork*
*Completed: 2026-05-20*
