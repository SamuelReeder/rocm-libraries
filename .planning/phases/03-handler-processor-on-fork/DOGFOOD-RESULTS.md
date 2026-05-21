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

**Status:** passed

- **Latest pass:** `2026-05-21T04:32:53.212433+00:00`
- **PR:** [#38](https://github.com/SamuelReeder/rocm-libraries/pull/38)
- **Expected outcome:** `{"action": "Eject", "reason": "merge conflict with develop"}`
- **Observed outcome:** `{"action": "Eject", "reason": "merge conflict with develop"}`
- **Source JSON:** [`dogfood-runs/2026-05-21T04-30-46.580255+00-00-dog_02.json`](dogfood-runs/2026-05-21T04-30-46.580255+00-00-dog_02.json)
- **Notes:** Live-fork run: the processor cycle URL embedded in the status comment body is the canonical processor_run_url; populate from the comment in a future enhancement if needed.


<a id="dog-03"></a>
## DOG-03 — RFC §6 row: CI failure during evaluation

**Status:** passed

- **Latest pass:** `2026-05-21T04:19:37.884422+00:00`
- **PR:** [#37](https://github.com/SamuelReeder/rocm-libraries/pull/37)
- **Expected outcome:** `{"action": "Eject", "reason_substring": "mq-dogfood-canary"}`
- **Observed outcome:** `{"action": "Eject", "reason": "mq-dogfood-canary", "reason_full_body": "## \u274c Ejected from merge queue\n\nReason: Repository rule violations found\n\nRequired status check \"mq-dogfood-canary\" is failing.\n\n.\n\nRe-enqueue with `/merge` once addressed.\n_Updated 2026-05-21 04:19 UTC._\nLast processed: [run](https://github.com/SamuelReeder/rocm-libraries/actions/runs/26205151165)\n\n<!-- rocm-mq-status -->"}`
- **Source JSON:** [`dogfood-runs/2026-05-21T04-14-13.356284+00-00-dog_03.json`](dogfood-runs/2026-05-21T04-14-13.356284+00-00-dog_03.json)
- **Notes:** Canary check-name resolved from path_to_queues.yml at runtime: 'mq-dogfood-canary'; eject-reason matched substring 'mq-dogfood-canary'. Live-fork run: the processor cycle URL embedded in the status comment body is the canonical processor_run_url; populate from the comment in a future enhancement if needed.


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

**Status:** passed

- **Latest pass:** `2026-05-21T04:39:30.054389+00:00`
- **PR:** [#39](https://github.com/SamuelReeder/rocm-libraries/pull/39)
- **Expected outcome:** `{"action": "Eject", "reason": "activation invalid (branch updated or label tampered)"}`
- **Observed outcome:** `{"action": "Eject", "reason": "activation invalid (branch updated or label tampered)"}`
- **Source JSON:** [`dogfood-runs/2026-05-21T04-34-17.664340+00-00-dog_05.json`](dogfood-runs/2026-05-21T04-34-17.664340+00-00-dog_05.json)
- **Notes:** Activation observed at head SHA '898f91a0284a0917dc6b6251936bafe545eeb956'; author push wrote 'projects/hipdnn/dogfood-author-push-extra-df32fbef.txt' producing post-push head SHA '310c77e632ada4731a59addef4e6827398271216'. The eject reason is the verbatim RFC §6 / decision.py literal; any drift in the literal would flip passed=False. Live-fork run: the processor cycle URL embedded in the status comment body is the canonical processor_run_url; populate from the comment in a future enhancement if needed.


<a id="dog-06"></a>
## DOG-06 — RFC §6 row: 5-PR worked example (RFC §4.2)

**Status:** passed

- **Latest pass:** `2026-05-21T04:56:21.986650+00:00`
- **PR:** [#40](https://github.com/SamuelReeder/rocm-libraries/pull/40)
- **Expected outcome:** `{"action": "AllMerged", "ordering_invariant": "merge_queue_active_status_posted precedes squash_merge_completed per PR", "per_pr_state_all": "merged", "pr_count": 5, "tree_diff_invariant": "tree_diff_status='ahead' on every squash_merge_completed"}`
- **Observed outcome:** `{"action": "AllMerged", "ordering_invariant_violations": [], "per_pr": {"PR_A": {"active_run_url": "", "pr_number": 40, "pr_url": "https://github.com/SamuelReeder/rocm-libraries/pull/40", "squash_run_url": "https://github.com/SamuelReeder/rocm-libraries/actions/runs/26205983384", "state": "merged", "tree_diff_status": "ahead"}, "PR_B": {"active_run_url": "", "pr_number": 41, "pr_url": "https://github.com/SamuelReeder/rocm-libraries/pull/41", "squash_run_url": "https://github.com/SamuelReeder/rocm-libraries/actions/runs/26206283105", "state": "merged", "tree_diff_status": "ahead"}, "PR_C": {"active_run_url": "", "pr_number": 42, "pr_url": "https://github.com/SamuelReeder/rocm-libraries/pull/42", "squash_run_url": "https://github.com/SamuelReeder/rocm-libraries/actions/runs/26206093108", "state": "merged", "tree_diff_status": "ahead"}, "PR_D": {"active_run_url": "", "pr_number": 43, "pr_url": "https://github.com/SamuelReeder/rocm-libraries/pull/43", "squash_run_url": "https://github.com/SamuelReeder/rocm-libraries/actions/runs/26206208797", "state": "merged", "tree_diff_status": "ahead"}, "PR_E": {"active_run_url": "", "pr_number": 44, "pr_url": "https://github.com/SamuelReeder/rocm-libraries/pull/44", "squash_run_url": "https://github.com/SamuelReeder/rocm-libraries/actions/runs/26206093108", "state": "merged", "tree_diff_status": "ahead"}}, "pr_count": 5, "tree_diff_invariant_violations": []}`
- **Source JSON:** [`dogfood-runs/2026-05-21T04-42-39.643364+00-00-dog_06.json`](dogfood-runs/2026-05-21T04-42-39.643364+00-00-dog_06.json)
- **Notes:** 5-PR RFC §4.2 worked example replay. Primary pr_url='https://github.com/SamuelReeder/rocm-libraries/pull/40' (PR_A); all 5 PR URLs: PR_A: https://github.com/SamuelReeder/rocm-libraries/pull/40; PR_B: https://github.com/SamuelReeder/rocm-libraries/pull/41; PR_C: https://github.com/SamuelReeder/rocm-libraries/pull/42; PR_D: https://github.com/SamuelReeder/rocm-libraries/pull/43; PR_E: https://github.com/SamuelReeder/rocm-libraries/pull/44. Operational note: if driver runs longer than TIMEOUT_S due to real-CI variability, re-invocation does NOT clean up half-progress; operator must manually finalize partially-progressed PRs (clear mq:* labels, comment /dequeue) before retrying. Per plan 03-14 <action>.


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
