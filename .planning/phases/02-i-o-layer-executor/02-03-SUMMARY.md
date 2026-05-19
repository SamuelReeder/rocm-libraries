---
phase: 02-i-o-layer-executor
plan: 03
subsystem: infra
tags: [executor, dispatch, activation-state-machine, post-squash-verify, idempotency, pure-09]

# Dependency graph
requires:
  - phase: 02-i-o-layer-executor
    plan: 01
    provides: GitHubClient, CorruptSquashError, AppIdentity, PURE-09 positive lint scaffold
  - phase: 02-i-o-layer-executor
    plan: 02
    provides: FakeGitHub / FakeRepoState (status overwrite, label dedup, merge 204/201, label remove 404), build_snapshot
provides:
  - dispatch(action, *, client, config, owner, repo) -> ActionOutcome — exhaustive match over the five Action variants with assert_never
  - _handle_activate — RFC §4.9 state machine (merge develop → 204/201/409 → pre-stamp race check → stamp activation status → flip labels)
  - _handle_squash + _verify_squash — Pitfall 8 / IO-05 (April 2026 silent-corruption defence)
  - _handle_eject — overwrite activation status to failure + safely remove all mq:* labels
  - _handle_update_comment + _find_status_comment_id — lazy marker discovery for status-comment upsert
  - _safe_remove_label — swallows RequestFailed(404) per UK-3
affects: [02-04 cmd_process, 03-handler-audit, 04-cmd-process]

# Tech tracking
tech-stack:
  added: []  # no new runtime deps; reuses githubkit pinned in 02-01
  patterns:
    - "config: MergeQueueConfig threaded through dispatch() as keyword arg — label names and activation_status_context come from config, not hardcoded"
    - "pre-stamp race check — re-read pulls.get().head.sha after repos.merge and abort the stamp if it differs from the SHA we are about to stamp (T-02-03-02 mitigation)"
    - "PRState left UNCHANGED — branch name read from pulls.get().head.ref at handler runtime instead of widening PRState with a branch field (avoids touching 5 conftest fixtures + every PRState construction site in Phase 1)"
    - "pre_squash_develop_sha sourced via repos.get_branch(_TRUNK_BRANCH).commit.sha read BEFORE pulls.merge — guarantees the value matches what GitHub will compute as parents[0].sha on a correct squash"
    - "_VERIFY_SQUASH_RETRIES = 3 with (1s, 2s) backoffs on get_commit 404 (read-replication lag, UK-4); non-404 RequestFailed propagates immediately"
    - "_TRUNK_BRANCH = 'develop' as a module-level constant — pinned by RFC §4.9 contract; not promoted to MergeQueueConfig"
    - "_STATUS_MARKER literal byte-identical to comment._STATUS_MARKER (T-02-03-06 cross-file marker contract)"
    - "Test-only fake patches in test_executor.py — _patch_pulls_get_to_return_branch / _patch_repos_merge_to_advance_pr_head / _patch_repos_get_branch — model post-merge head advancement + branch-tip reads that the shipped FakeGitHub does not natively provide; kept in the test file (not gh_fake.py) to preserve the contract-test fixture's stability"

key-files:
  created:
    - ".github/merge-queue/src/rocm_mq/executor.py"
    - ".github/merge-queue/tests/test_executor.py"
    - ".planning/phases/02-i-o-layer-executor/02-03-SUMMARY.md"
  modified: []

key-decisions:
  - "PRState NOT extended with branch: str — handler reads pulls.get().head.ref at runtime to avoid touching every Phase 1 PRState construction site (5+ fixtures + Hypothesis strategies)"
  - "pre_squash_develop_sha sourced from repos.get_branch('develop').commit.sha read just before pulls.merge — single source of truth that matches what GitHub will set as parents[0] on a non-corrupted squash"
  - "dispatch() takes config: MergeQueueConfig as a keyword argument — executor must not hardcode label names or activation_status_context; threading config through is cleaner than duplicating defaults"
  - "204 from repos.merge handled via .status_code == 204 OR .parsed_data is None — either signal proves the call was a no-op; the executor falls back to the PR's current head SHA for the stamp"
  - "Pre-stamp race check skipped on the 204 path — the head SHA used for the stamp IS the current head (we just read it); race window only exists between repos.merge 201 and create_commit_status"
  - "_safe_remove_label catches only 404 — any other RequestFailed propagates so the caller cycle aborts cleanly (RFC §4.6 stateless-processor pattern: failure here means the next 3-min cron tick retries)"

requirements-completed: [IO-03, IO-04, IO-05]

# Metrics
duration: ~35min
completed: 2026-05-19
---

# Phase 02 Plan 03: Executor — Dispatch + Handlers + Post-Squash Verification Summary

**Implements `rocm_mq.executor`: the I/O dispatcher that turns inert `Action` data from `decide_cycle` into GitHub mutations. Includes the RFC §4.9 activation state machine (merge develop → pre-stamp race check → stamp → label flip), the Pitfall 8 post-squash readback (`_verify_squash` asserting `parents[0].sha == pre_squash_develop_sha` with 3-retry on 404), the eject handler (status overwrite to failure + mq:* label cleanup), `UpdateComment` lazy comment-ID discovery via the `<!-- rocm-mq-status -->` marker, and per-handler idempotency.**

## Performance

- **Duration:** ~35 min
- **Completed:** 2026-05-19
- **Tasks:** 2 (RED + GREEN)
- **Files created:** 3 (1 src, 1 test, 1 SUMMARY)
- **Files modified:** 0 (no edits to conftest, gh_fake, state, etc.)
- **Commits:** 2 (1 test [RED] + 1 feat [GREEN])

## Accomplishments

- **IO-03 landed.** `dispatch(action, *, client, config, owner, repo) -> ActionOutcome` exhaustively matches the five `Action` variants. The `case Defer(): return ActionOutcome(success=True)` branch makes deferred PRs cycle-cheap (no API call); `case _: assert_never(action)` keeps the exhaustiveness contract enforced at lint time by `mypy --strict` (the typing match is already exhaustive over `Activate | Squash | Eject | UpdateComment | Defer`).

- **IO-04 landed.** Every handler is idempotent on re-run:
  - `repos.merge` 204 → fall back to current PR head SHA + continue (already-up-to-date is a success, not a failure).
  - `repos.create_commit_status` overwrites the same `(sha, context)` pair — second post is a no-op data-wise (and `next_status_seq` increments harmlessly).
  - `issues.add_labels` is set-semantics; the fake confirms no duplicate; the executor relies on real GitHub honouring the same.
  - `issues.remove_label` 404 wrapped in `_safe_remove_label` → swallowed (label already absent matches the desired post-state).
  - `pulls.merge` 405 (already merged) → `ActionOutcome(success=True)` with the "already merged" reason in `error_message`.

- **IO-05 landed.** `_verify_squash` defends against the April 2026 GitHub silent-corruption incident (Pitfall 8). After `pulls.merge(squash)` succeeds, the handler reads back the squash SHA via `repos.get_commit` (with up-to-3 retries at 1s/2s backoffs to absorb read-replication lag, UK-4) and asserts `parents[0].sha == pre_squash_develop_sha`. A mismatch raises `CorruptSquashError` (RuntimeError subclass) carrying the PR number, the squash SHA, the expected parent, and the observed parent. `_handle_squash` catches `CorruptSquashError` and returns `ActionOutcome(success=False, error_message=str(exc))` — the cycle records the failure and the next 3-min cron tick handles the eject decision.

- **Activation state machine wired exactly as specified.** Per RFC §4.9: read PR branch ref → `repos.merge(base=<pr-branch>, head="develop")` → handle 201/204/409 → re-read PR head for the pre-stamp race check → `create_commit_status(state="success")` → `add_labels([mq:active])` → `_safe_remove_label(mq:queued)`. The race check is the load-bearing T-02-03-02 mitigation: if the author pushes between merge and stamp, the stamp is aborted with `success=False` so a stale SHA is never marked active.

- **PURE-09 positive lint extended.** `test_io_modules_do_import_githubkit[executor]` PASSES — was previously skipped because `executor.py` did not exist. All three I/O modules (gh, snapshot, executor) now satisfy the symmetric guard.

- **PRState NOT touched.** The plan allowed adding `branch: str` to `PRState`, but reading the branch ref via `pulls.get().head.ref` at handler runtime is the lower-blast-radius choice — `PRState` is constructed in 5+ test fixtures, Hypothesis strategies, and the canonical worked-example. Adding a required field would have rippled into all of them.

- **`gh_fake.py` NOT modified.** The plan allowed extending the fake, but the gaps (no `head.ref` on `pulls.get`, no `get_branch`, no post-merge `head_sha` advancement) are handled via test-local `_patch_*` helpers in `test_executor.py`. This keeps `gh_fake.py` stable for the contract tests in `test_io_contract.py` and the snapshot tests in `test_snapshot.py`.

## OQ Resolutions / Design Choices

### branch field on PRState — NOT added

`PRState` stayed at 7 fields. The executor reads the PR's head ref via `pulls.get().head.ref` at the start of `_handle_activate`. This is one extra API call per activate, but the activate-handler is already low-frequency (≤ 1 per PR per cycle, and only when the cycle decides to activate). The alternative (widening `PRState`) would have required updating:
- `tests/conftest.py` — 6+ PRState fixtures (lines 327–512)
- `_strategies.py` — Hypothesis builders
- `_worked_example_constants.py` — Plan 01-05 frozen snapshots
- Every test that constructs `PRState` directly
- Plan 02-04's `cmd_process.py` (still to come) would need to plumb the branch through `build_snapshot` / `derive_pr`

The runtime-read approach localizes the change to one file.

### pre_squash_develop_sha source — repos.get_branch("develop")

Recorded just BEFORE `pulls.merge(squash)`. This is the value GitHub will set as `parents[0].sha` on a correct squash (the squash commit is created on top of develop's current tip). Reading the PR's `base.sha` from `pulls.get` would NOT be correct in general — `base.sha` is the SHA of the base branch at the time the PR was opened/last updated, not the current develop tip. The `get_branch` call gives the live value.

### dispatch() signature — added config: MergeQueueConfig keyword

Final signature:

```python
def dispatch(
    action: Action,
    *,
    client: GitHubClient,
    config: MergeQueueConfig,
    owner: str,
    repo: str,
) -> ActionOutcome:
```

All keyword-only after the action arg — matches the snapshot.py convention (`build_snapshot(client, config, *, owner, repo)`) and prevents positional-arg drift as Plan 02-04 wires `cmd_process.py`. Config provides `activation_status_context`, `active_label`, `queued_label`, and `label_prefix` — no hardcoded RFC §4.3 defaults in executor.

### Test-only fake patches — kept in test_executor.py

The shipped `FakeGitHub` (Plan 02-02) does not model:
1. **`head.ref` on `pulls.get`** — the fake returns `head=SimpleNamespace(sha=...)` only. Patched via `_patch_pulls_get_to_return_branch`.
2. **`repos.get_branch`** — not present at all. Patched via `_patch_repos_get_branch` (returns `commit.sha`).
3. **PR head_sha advancement after `repos.merge` 201** — the fake updates `develop_tip` but not the PR's `head_sha`. In real GitHub, a `repos.merge(base=feature, head=develop)` creates a merge commit on `feature` and advances the PR head. Patched via `_patch_repos_merge_to_advance_pr_head`.
4. **204 modelling against branch-name `head=`** — the fake's literal check `head == self._state.develop_tip` only returns 204 when `head` is a SHA equal to develop's tip; with `head="develop"` (a branch name), it falls into the 201 path. Tests that want to exercise the 204 path force the response with `fake.rest.repos.merge = lambda ...: SimpleNamespace(status_code=204, parsed_data=None)`.

These patches stayed in `test_executor.py` rather than being merged into `gh_fake.py` so the contract tests (`test_io_contract.py`) keep observing the unmodified fake — preserving their parity guarantee with the real GitHub API. The patches are documented inline with the rationale.

## Activation State Machine Details

`_handle_activate` implements RFC §4.9 step-by-step. Recipe in code order:

```
1. pr_obj  = pulls.get(pr.number).parsed_data
   pr_branch = pr_obj.head.ref
2. try:
       merge_resp = repos.merge(base=pr_branch, head="develop")
   except RequestFailed as exc:
       if status_code(exc) == 409:
           return ActionOutcome(success=False, "merge conflict with develop")
       raise
3. if merge_resp.status_code == 204 or parsed_data is None:
       new_sha = pulls.get(pr.number).parsed_data.head.sha   # 204: use current head
   else:
       new_sha = merge_resp.parsed_data.sha                  # 201: use merge SHA
4. # Pre-stamp race check — 201 path only
   if status_code != 204:
       current_head = pulls.get(pr.number).parsed_data.head.sha
       if current_head != new_sha:
           return ActionOutcome(success=False, f"pre-stamp race: {new_sha} -> {current_head}")
5. repos.create_commit_status(new_sha, state="success", context=activation_status_context)
6. issues.add_labels(pr.number, data=[active_label])
7. _safe_remove_label(pr.number, queued_label)
8. return ActionOutcome(success=True)
```

Edge cases covered by tests:
- 201 with consistent head → success path, status on merge SHA, labels flipped.
- 204 already-up-to-date → status on PR's current head SHA, labels flipped.
- 409 conflict → success=False with "merge conflict" message; no status, no label change.
- Pre-stamp race (201, then author pushes) → success=False with "pre-stamp race" message; no status stamped.
- Second call on already-active PR (forced 204) → success; labels unchanged; status overwritten on same SHA.

## Test Counts

`tests/test_executor.py`: 25 tests

| Section | Count |
|---------|-------|
| Dispatch table rows (Activate, Squash, Eject, UpdateComment, Defer) | 5 |
| Activation state machine (201, 204, 409, pre-stamp race, second-call no-op) | 5 |
| Per-handler idempotency (remove_label 404, squash 405, status overwrite, add_labels dedup, repos.merge 204) | 5 |
| Post-squash verification (`_verify_squash` happy path, mismatch raises, 404 retry success, 404 retries exhausted, dispatch returns failure on CorruptSquashError) | 5 |
| `_find_status_comment_id` (marker present / absent) | 2 |
| UpdateComment routing (create vs update) | 2 |
| Eject handler (status to failure + mq:* label cleanup) | 1 |
| **Total** | **25** |

Full suite: 282 tests pass (was 257 — 25 new from this plan).

## Task Commits

1. **Task 1 (RED): Failing tests for executor dispatch + idempotency + post-squash verify** — `de5d18bab55` (test)
2. **Task 2 (GREEN): Implement executor.py dispatch + handlers + post-squash verification** — `ea2e67457e1` (feat)

## Files Created/Modified

### Created

- `.github/merge-queue/src/rocm_mq/executor.py` — `dispatch`, 4 `_handle_*` functions, `_verify_squash`, `_find_status_comment_id`, `_safe_remove_label`, `_status_code`, `_read_pr`, `_read_branch_tip`. ~430 LOC including module docstring + per-function docstrings. PURE-09 compliance marker: `from githubkit.exception import RequestFailed` at module level.
- `.github/merge-queue/tests/test_executor.py` — 25 unit tests. Uses `FakeGitHub` for idempotency / state-machine tests and `MagicMock` for dispatch-routing tests. Test-only `_patch_*` helpers (3) handle fake gaps documented above.

### Modified

None. `state.py`, `conftest.py`, `gh_fake.py`, and every Phase 1 file untouched.

## Decisions Made

See "OQ Resolutions / Design Choices" above. Summary of the four load-bearing choices:

1. **PRState unchanged** — branch ref read at runtime instead of widening the dataclass.
2. **pre_squash_develop_sha via `repos.get_branch`** — live develop tip; not PR `base.sha`.
3. **`dispatch()` keyword-only `config`** — matches `build_snapshot`'s signature shape.
4. **Test patches stay in `test_executor.py`** — preserves `gh_fake.py` stability for `test_io_contract.py`.

## Deviations from Plan

None requiring rule-tracking. Two plan-allowed deviations exercised, both documented:

- **Allowed deviation:** "MAY add `branch: str` to PRState" → **chose not to** (runtime read instead). Documented above.
- **Allowed deviation:** "MAY choose pre_squash_develop_sha source" → **chose `repos.get_branch('develop').commit.sha`** read just before squash. Documented above.

No Rule 1 / 2 / 3 fixes were needed — the GREEN gate revealed only test-level fake-modelling gaps (handled via the documented test-local patches), not executor bugs.

## Issues Encountered

- **FakeGitHub's `repos.merge` 204 modelling** — the fake checks `head == develop_tip` literally, which only works when `head` is a SHA equal to develop. The executor (correctly per real GitHub) calls `repos.merge(base=pr_branch, head="develop")` — passing a branch name. Worked around by force-stubbing `fake.rest.repos.merge` to return 204 unconditionally in the 4 tests that exercise the 204 path. Documented inline.
- **FakeGitHub does not advance PR `head_sha` after `repos.merge` 201** — in real GitHub, the merge commit IS the new PR head. Worked around via `_patch_repos_merge_to_advance_pr_head` in tests that exercise the 201→stamp path. Documented inline.

Both gaps stayed out of `gh_fake.py` to preserve contract-test stability; instead documented as known fake limitations in `test_executor.py`'s helper docstrings.

## User Setup Required

None.

## Next Phase Readiness

- **Ready for Plan 02-04 (cmd_process).** `dispatch()` is the single entry point for executing Actions; `cmd_process.py` will iterate over `decide_cycle`'s output and call `dispatch()` per action.
- **Ready for Phase 3 (handler / audit workflows).** `_handle_eject` provides the eject path the audit job will invoke; `_handle_update_comment` + `_find_status_comment_id` provide the comment-upsert path the `/merge` handler will invoke.
- **No blockers.** Full suite (282 tests) green; ruff clean on touched files (1 pre-existing I001 in `tests/test_gh_client.py` from Plan 02-01 left out of scope per executor scope-boundary); mypy clean on `executor.py`.

## Self-Check: PASSED

- File `.github/merge-queue/src/rocm_mq/executor.py` — FOUND
- File `.github/merge-queue/tests/test_executor.py` — FOUND
- File `.planning/phases/02-i-o-layer-executor/02-03-SUMMARY.md` — FOUND
- Commit `de5d18bab55` — FOUND (test Task 1 RED)
- Commit `ea2e67457e1` — FOUND (feat Task 2 GREEN)
- PURE-09 positive lint for executor — PASSES (was previously skipped)
- Full suite — 282 passed (was 257)

---
*Phase: 02-i-o-layer-executor*
*Completed: 2026-05-19*
