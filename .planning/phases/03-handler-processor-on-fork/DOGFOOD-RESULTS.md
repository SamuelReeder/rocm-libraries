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

**Status:** not yet run

Driver module: `rocm_mq.dogfood.dog_04`. Invoke per CONTEXT.md D-04, then re-run the aggregator.


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

**Status:** not yet run

Driver module: `rocm_mq.dogfood.dog_08`. Invoke per CONTEXT.md D-04, then re-run the aggregator.
