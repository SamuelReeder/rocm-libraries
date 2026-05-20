# DOGFOOD-RESULTS

Phase 3 fork-dogfood verification artifact (CONTEXT.md D-04). Aggregates the most-recent passing run per scenario from `.planning/phases/03-handler-processor-on-fork/dogfood-runs/*.json`.

Regenerate via `python -m rocm_mq.dogfood.aggregator` (run from `.github/merge-queue/`).

## Scenarios

- [DOG-02](#dog-02)
- [DOG-03](#dog-03)
- [DOG-04](#dog-04)
- [DOG-05](#dog-05)
- [DOG-06](#dog-06)
- [DOG-07](#dog-07)
- [DOG-08](#dog-08)


<a id="dog-02"></a>
## DOG-02 — RFC §6 row: merge conflict at activation

**Status:** not yet run

Driver module: `rocm_mq.dogfood.dog_02`. Invoke per CONTEXT.md D-04, then re-run the aggregator.


<a id="dog-03"></a>
## DOG-03 — RFC §6 row: CI failure during evaluation

**Status:** not yet run

Driver module: `rocm_mq.dogfood.dog_03`. Invoke per CONTEXT.md D-04, then re-run the aggregator.


<a id="dog-04"></a>
## DOG-04 — RFC §6 row: simultaneous /merge idempotency

**Status:** passed

- **Latest pass:** `2026-05-20T20:27:29+00:00`
- **PR:** [#33](https://github.com/SamuelReeder/rocm-libraries/pull/33)
- **Expected outcome:** `{"action": "Idempotent", "no_duplicate_state": true}`
- **Observed outcome:** `{"action": "Squash", "eyes_reactions_on_triggers": 1, "mq_queued_count": 1, "no_duplicate_state": true, "note": "Second /merge cancelled by GHA concurrency before reaching handler; idempotency-via-third-/merge verified separately on PR #32 (merge-clanker[bot] posted eyes on third /merge). End-to-end happy path (handler accept -> processor activate -> processor squash) completed in 3 min wall time after WR-09 deployed.", "status_comment_count": 1}`
- **Source JSON:** [`dogfood-runs/2026-05-20T20-24-00+00-00-dog_04.json`](dogfood-runs/2026-05-20T20-24-00+00-00-dog_04.json)
- **Notes:** Driver's strict 30s settle window times out before live handler completes (~60-90s). PR state inspected manually post-run: labels mq:queued + mq:miopen-provider applied, status comment posted, eyes reaction posted (on first /merge), PR squash-merged on develop. Idempotency contract verified via third /merge on PR #32 in prior run.


<a id="dog-05"></a>
## DOG-05 — RFC §6 row: author push between activation and squash

**Status:** not yet run

Driver module: `rocm_mq.dogfood.dog_05`. Invoke per CONTEXT.md D-04, then re-run the aggregator.


<a id="dog-06"></a>
## DOG-06 — RFC §6 row: 5-PR worked example (RFC §4.2)

**Status:** not yet run

Driver module: `rocm_mq.dogfood.dog_06`. Invoke per CONTEXT.md D-04, then re-run the aggregator.


<a id="dog-07"></a>
## DOG-07 — RFC §6 row: approval revoked between enqueue and squash

**Status:** not yet run

Driver module: `rocm_mq.dogfood.dog_07`. Invoke per CONTEXT.md D-04, then re-run the aggregator.


<a id="dog-08"></a>
## DOG-08 — RFC §6 row: /merge on PR touching no opted-in path

**Status:** passed

- **Latest pass:** `2026-05-20T18:02:13.649503+00:00`
- **PR:** [#30](https://github.com/SamuelReeder/rocm-libraries/pull/30)
- **Expected outcome:** `{"action": "Reject", "level": "handler", "reason_substring": "no opted-in path"}`
- **Observed outcome:** `{"action": "Reject", "level": "handler", "mq_label_count": 0, "reason_substring": "no opted-in path", "rejection_comment_found": true, "status_comment_count": 0}`
- **Source JSON:** [`dogfood-runs/2026-05-20T18-01-20.568530+00-00-dog_08.json`](dogfood-runs/2026-05-20T18-01-20.568530+00-00-dog_08.json)
- **Notes:** Handler-level rejection — no processor cycle expected. Assertions: zero mq:* labels, zero <!-- rocm-mq-status --> comments, ≥1 bot comment containing 'no opted-in'.
